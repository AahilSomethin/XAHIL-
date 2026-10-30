"""Command-line entry point: `python -m xahil <command>` (`python3` in the cloud).

This is the ONLY path that can submit orders, and every order passes the
deterministic risk engine first. Order execution is OFF unless BOTH
config/execution.json has order_execution_enabled=true (with approved_by)
AND the environment has XAHIL_ORDER_EXECUTION=enabled. Otherwise every
order command is a dry run that records a proposal.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

from xahil import guard, metrics, notify, reconcile, research
from xahil.alpaca import AlpacaClient, AlpacaError
from xahil.config import (
    MEMORY_DIR,
    ROOT,
    STATE_DIR,
    ConfigError,
    env_halt,
    execution_env_enabled,
    is_cloud_run,
    load_dotenv,
    load_execution_settings,
    load_integrations,
    load_risk_limits,
    _read_json,
    CONFIG_DIR,
)
from xahil.risk import (
    OrderRequest,
    RiskContext,
    Verdict,
    _f,
    check_stop_not_lowered,
    count_week_buys,
    evaluate_order,
    is_whole,
    position_for,
)

SNAPSHOT_FILE = STATE_DIR / "broker_snapshot.json"
HISTORY_FILE = STATE_DIR / "equity_history.csv"
PROPOSALS_FILE = STATE_DIR / "proposed_orders.jsonl"
EXECUTIONS_FILE = STATE_DIR / "executed_orders.jsonl"
RECON_LOG = MEMORY_DIR / "RECONCILIATION-LOG.md"
RESEARCH_LOG = MEMORY_DIR / "RESEARCH-LOG.md"

EXIT_OK, EXIT_ERROR, EXIT_HALT, EXIT_UNAVAILABLE, EXIT_REJECTED = 0, 1, 2, 3, 4


def out(obj) -> None:
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, indent=2, default=str))


# --- time helpers (ET via Alpaca's clock, no tz database needed) --------------

def parse_ts(ts: str) -> dt.datetime:
    ts = ts.replace("Z", "+00:00")
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
    return dt.datetime.fromisoformat(ts)


def week_start_iso(clock_ts: str) -> str:
    now = parse_ts(clock_ts)
    monday = (now - dt.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday.isoformat()


# --- execution gate ------------------------------------------------------------

def execution_mode(env=None) -> tuple:
    env = os.environ if env is None else env
    s = load_execution_settings()
    reasons = []
    if s.halt:
        reasons.append(f"halt=true in config/execution.json ({s.halt_reason or 'no reason given'})")
    if env_halt(env):
        reasons.append("XAHIL_HALT is set")
    if not s.order_execution_enabled:
        reasons.append("order_execution_enabled=false in config/execution.json (awaiting owner approval)")
    elif not s.approved_by.strip():
        reasons.append("order_execution_enabled=true but approved_by is empty")
    if not execution_env_enabled(env):
        reasons.append("XAHIL_ORDER_EXECUTION=enabled not set in this environment")
    return ("execute" if not reasons else "dry-run"), reasons


# --- research log check -----------------------------------------------------------

def research_section(date: str, path: Path = RESEARCH_LOG) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(date)}\b.*?(?=^## \d{{4}}-\d{{2}}-\d{{2}}|\Z)", text, re.M | re.S)
    return m.group(0) if m else ""


def research_mentions(date: str, symbol: str, path: Path = RESEARCH_LOG) -> bool:
    return bool(re.search(rf"\b{re.escape(symbol)}\b", research_section(date, path)))


# --- shared state gathering ----------------------------------------------------------

def gather(client: AlpacaClient, symbol: str = None, with_market: bool = False) -> dict:
    clock = client.clock()
    state = {
        "clock": clock,
        "today": parse_ts(clock["timestamp"]).date().isoformat(),
        "account": client.account(),
        "positions": client.positions(),
        "open_orders": client.orders("open"),
    }
    state["week_orders"] = client.orders("all", after=week_start_iso(clock["timestamp"]), side="buy")
    if symbol and with_market:
        state["asset"] = client.asset(symbol)
        state["quote"] = client.latest_quote(symbol)
    return state


def recon_halts(state: dict) -> list:
    snap = reconcile.load_snapshot(SNAPSHOT_FILE)
    if snap.get("account_number") and snap["account_number"] != state["account"].get("account_number"):
        return [f"account number changed since last snapshot ({snap['account_number']})"]
    return []


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def manual_confirmation(symbol: str, body: dict) -> bool:
    if is_cloud_run() or not sys.stdin.isatty():
        return False
    print(json.dumps(body, indent=2))
    try:
        typed = input(f"PAPER order. Type {symbol} to submit, anything else to cancel: ").strip().upper()
    except (EOFError, KeyboardInterrupt):
        return False
    return typed == symbol


def submit_or_propose(client, order: OrderRequest, verdict: Verdict, workflow: str, today: str,
                      reason: str, manual: bool, cancel_first: list = None) -> int:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%H%M%S")
    coid = f"xahil-{today}-{workflow}-{order.symbol}-{order.side}-{order.type}"
    if order.side == "sell":
        coid += f"-{stamp}"
    body = order.to_alpaca(coid[:128])
    record = {"date": today, "workflow": workflow, "order": body, "reason": reason,
              "verdict": verdict.summary(), "checks": verdict.checks, "estimated_cost": verdict.estimated_cost,
              "cancel_first": cancel_first or []}
    if not verdict.approved:
        record["status"] = "rejected_by_risk_engine"
        append_jsonl(PROPOSALS_FILE, record)
        out(record)
        return EXIT_REJECTED

    mode, why = execution_mode()
    if mode != "execute" and manual:
        halts = [w for w in why if "halt" in w.lower()]
        if not halts and manual_confirmation(order.symbol, body):
            mode = "execute"
    if mode != "execute":
        record["status"] = "dry_run_proposal"
        record["not_executed_because"] = why
        append_jsonl(PROPOSALS_FILE, record)
        out(record)
        return EXIT_OK

    if not client.clock().get("is_open"):
        record["status"] = "not_submitted_market_closed"
        append_jsonl(PROPOSALS_FILE, record)
        out(record)
        return EXIT_REJECTED
    for oid in cancel_first or []:
        client.cancel_order(oid)
    resp = client.submit_order(body)
    record["status"] = "submitted"
    record["alpaca_order_id"] = resp.get("id")
    record["alpaca_status"] = resp.get("status")
    append_jsonl(EXECUTIONS_FILE, record)
    out(record)
    return EXIT_OK


# --- commands ------------------------------------------------------------------------

def cmd_preflight(args, client_factory) -> int:
    report = {"paper_endpoint": None, "auth_mode": None, "account": None}
    limits = load_risk_limits()
    report["risk_limits"] = {
        "starting_capital": limits.starting_capital,
        "max_position_pct": limits.max_position_pct,
        "max_total_exposure_pct": limits.max_total_exposure_pct,
        "max_new_trades_per_week": limits.max_new_trades_per_week,
        "max_open_positions": limits.max_open_positions,
    }
    mode, why = execution_mode()
    report["execution_mode"] = mode
    report["execution_blockers"] = why
    client = client_factory()
    report["paper_endpoint"] = client.trading_base
    report["auth_mode"] = client.credentials.mode
    acct = client.account()
    report["account"] = {"number_prefix": str(acct.get("account_number", ""))[:2], "status": acct.get("status"),
                         "equity": acct.get("equity"), "cash": acct.get("cash")}
    env = os.environ
    report["telegram"] = "configured" if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID") else "fallback-outbox"
    try:
        research.check_available(load_integrations(), env, dt.date.today())
        report["perplexity"] = "enabled"
    except research.ResearchUnavailable as exc:
        report["perplexity"] = f"off ({exc}) - use WebSearch"
    report["dotenv_present_in_cloud"] = is_cloud_run() and (ROOT / ".env").exists()
    out(report)
    if report["dotenv_present_in_cloud"]:
        print("ERROR: a .env file exists in a cloud run. Delete it; never commit it.", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def cmd_market_status(args, client_factory) -> int:
    c = client_factory()
    clock = c.clock()
    today = parse_ts(clock["timestamp"]).date().isoformat()
    cal = c.calendar(today, today)
    session = cal[0] if cal and cal[0].get("date") == today else None
    out({
        "today_et": today,
        "trading_day": session is not None,
        "is_open": clock.get("is_open"),
        "session_open": session.get("open") if session else None,
        "session_close": session.get("close") if session else None,
        "early_close": bool(session and session.get("close") not in (None, "16:00")),
        "next_open": clock.get("next_open"),
        "next_close": clock.get("next_close"),
    })
    return EXIT_OK


def cmd_read(args, client_factory) -> int:
    c = client_factory()
    if args.command == "account":
        out(c.account())
    elif args.command == "positions":
        out(c.positions())
    elif args.command == "orders":
        out(c.orders(args.status))
    elif args.command == "quote":
        out(c.latest_quote(args.symbol.upper()))
    return EXIT_OK


def cmd_reconcile(args, client_factory) -> int:
    c = client_factory()
    limits = load_risk_limits()
    state = gather(c)
    prev = reconcile.load_snapshot(SNAPSHOT_FILE)
    result = reconcile.reconcile(prev, state["clock"]["timestamp"][:16], args.workflow, state["account"],
                                 state["positions"], state["open_orders"], limits, bool(state["clock"].get("is_open")))
    result["halt_reasons"] = recon_halts(state) + result["halt_reasons"]
    result["ok"] = not result["halt_reasons"]
    reconcile.save_snapshot(SNAPSHOT_FILE, result["snapshot"])
    if not RECON_LOG.exists():
        RECON_LOG.write_text("# Reconciliation Log\n\nBroker (Alpaca paper) state at the start of every run. "
                             "Alpaca is the source of truth.\n", encoding="utf-8")
    with RECON_LOG.open("a", encoding="utf-8") as fh:
        fh.write(reconcile.format_log_entry(result))
    summary = {k: result[k] for k in ("ok", "halt_reasons", "limit_breaches", "unprotected_positions", "differences")}
    summary["equity"] = result["snapshot"]["equity"]
    summary["cash"] = result["snapshot"]["cash"]
    summary["positions"] = result["snapshot"]["positions"]
    summary["execution_mode"] = execution_mode()[0]
    out(summary)
    return EXIT_OK if result["ok"] else EXIT_HALT


def _buy_request(args) -> OrderRequest:
    return OrderRequest(
        side="buy", symbol=args.symbol.upper(), type="limit" if args.limit_price else "market",
        time_in_force="day", qty=args.qty, notional=args.notional, limit_price=args.limit_price,
    )


def _buy_verdict(client, order: OrderRequest) -> tuple:
    state = gather(client, order.symbol, with_market=True)
    ctx = RiskContext(
        account=state["account"], positions=state["positions"], open_orders=state["open_orders"],
        week_orders=state["week_orders"], limits=load_risk_limits(), asset=state["asset"], quote=state["quote"],
        research_documented=research_mentions(state["today"], order.symbol), halt_reasons=recon_halts(state),
    )
    return evaluate_order(order, ctx), state


def cmd_check_buy(args, client_factory) -> int:
    verdict, _ = _buy_verdict(client_factory(), _buy_request(args))
    out({"verdict": verdict.summary(), "checks": verdict.checks, "estimated_cost": verdict.estimated_cost})
    return EXIT_OK if verdict.approved else EXIT_REJECTED


def cmd_buy(args, client_factory) -> int:
    if not args.reason.strip():
        raise ConfigError("--reason (the catalyst) is required")
    c = client_factory()
    order = _buy_request(args)
    verdict, state = _buy_verdict(c, order)
    return submit_or_propose(c, order, verdict, args.workflow, state["today"], args.reason, args.manual)


def _sell_context(state, limits, cancel_ids):
    remaining = [o for o in state["open_orders"] if o.get("id") not in cancel_ids]
    return RiskContext(account=state["account"], positions=state["positions"], open_orders=remaining,
                       week_orders=[], limits=limits)


def cmd_sell(args, client_factory) -> int:
    if not args.reason.strip():
        raise ConfigError("--reason is required")
    c = client_factory()
    limits = load_risk_limits()
    sym = args.symbol.upper()
    state = gather(c)
    pos = position_for(state["positions"], sym)
    held = _f(pos.get("qty")) if pos else 0.0
    qty = held if args.all else args.qty
    # Alpaca reserves shares for open sell orders (stops): cancel them first, then sell.
    cancel_ids = [o["id"] for o in state["open_orders"] if o.get("symbol") == sym and o.get("side") == "sell"]
    order = OrderRequest(side="sell", symbol=sym, type="limit" if args.limit_price else "market",
                         time_in_force="day", qty=qty, limit_price=args.limit_price)
    verdict = evaluate_order(order, _sell_context(state, limits, cancel_ids))
    return submit_or_propose(c, order, verdict, args.workflow, state["today"], args.reason, args.manual, cancel_ids)


def _existing_stop_level(orders: list, sym: str):
    levels = [_f(o.get("stop_price")) for o in orders
              if o.get("symbol") == sym and o.get("side") == "sell" and o.get("type") in ("stop", "trailing_stop")
              and _f(o.get("stop_price")) > 0]
    return max(levels) if levels else None


def cmd_protect(args, client_factory) -> int:
    c = client_factory()
    limits = load_risk_limits()
    params = _read_json(CONFIG_DIR / "strategy_params.json")
    sym = args.symbol.upper()
    state = gather(c)
    pos = position_for(state["positions"], sym)
    if not pos:
        out({"error": f"no position in {sym}"})
        return EXIT_REJECTED
    qty, price = _f(pos.get("qty")), _f(pos.get("current_price"))
    cancel_ids = [o["id"] for o in state["open_orders"] if o.get("symbol") == sym and o.get("side") == "sell"]
    old_level = _existing_stop_level(state["open_orders"], sym)
    trail = args.trail_pct
    if trail is not None and is_whole(qty):
        order = OrderRequest(side="sell", symbol=sym, type="trailing_stop", time_in_force="gtc",
                             qty=qty, trail_percent=trail)
        new_level = price * (1 - trail / 100)
    else:
        pct = trail if trail is not None else args.stop_pct
        stop_price = args.stop_price or round(price * (1 - pct / 100), 2)
        order = OrderRequest(side="sell", symbol=sym, type="stop",
                             time_in_force="day" if not is_whole(qty) else "gtc", qty=qty, stop_price=stop_price)
        new_level = stop_price
    verdict = evaluate_order(order, _sell_context(state, limits, cancel_ids))
    extra = []
    lowered = check_stop_not_lowered(old_level, new_level)
    if lowered:
        extra.append(lowered)
    min_gap = float(params.get("min_stop_distance_pct", 3.0))
    if price and new_level > price * (1 - min_gap / 100) + 1e-9:
        extra.append(f"stop {new_level:.2f} is within {min_gap}% of price {price:.2f}")
    if extra:
        verdict = Verdict(False, verdict.reasons + extra, {**verdict.checks, "strategy_stop_rules": False})
    return submit_or_propose(c, order, verdict, args.workflow, state["today"], args.reason or "protective stop",
                             args.manual, cancel_ids)


def midday_plan(positions: list, open_orders: list, params: dict) -> list:
    actions = []
    cut = float(params["cut_loss_pct"])
    tiers = sorted(params["trail_tightening"], key=lambda t: -t["gain_pct"])
    for p in positions:
        sym, plpc = p["symbol"], _f(p.get("unrealized_plpc")) * 100
        if plpc <= cut:
            actions.append({"symbol": sym, "action": "SELL_ALL", "why": f"{plpc:.2f}% <= {cut}% cut rule",
                            "command": f"sell {sym} --all --reason \"cut at {cut}% per rule\""})
            continue
        for tier in tiers:
            if plpc >= tier["gain_pct"]:
                actions.append({"symbol": sym, "action": "TIGHTEN_STOP", "why": f"+{plpc:.2f}% >= +{tier['gain_pct']}%",
                                "command": f"protect {sym} --trail-pct {tier['trail_pct']} --reason \"tighten at +{tier['gain_pct']}%\""})
                break
        has_stop = any(o.get("symbol") == sym and o.get("side") == "sell" and o.get("type") in ("stop", "trailing_stop")
                       for o in open_orders)
        if not has_stop:
            actions.append({"symbol": sym, "action": "ADD_STOP", "why": "no protective stop on file",
                            "command": f"protect {sym} --stop-pct {params['initial_stop_pct']} --reason \"missing stop\""})
    return actions


def cmd_midday_scan(args, client_factory) -> int:
    c = client_factory()
    params = _read_json(CONFIG_DIR / "strategy_params.json")
    state = gather(c)
    plan = midday_plan(state["positions"], state["open_orders"], params)
    out({"execution_mode": execution_mode()[0], "actions": plan,
         "note": "Deterministic rule hits only. Thesis-break exits are Claude's judgment and still pass the risk engine."})
    return EXIT_OK


def _spy_close(c, day: str):
    try:
        bars = c.daily_bars("SPY", start=day)
        for b in bars:
            if str(b.get("t", ""))[:10] == day:
                return _f(b.get("c")) or None
    except AlpacaError:
        return None
    return None


def cmd_snapshot(args, client_factory) -> int:
    c = client_factory()
    state = gather(c)
    acct, positions, today = state["account"], state["positions"], state["today"]
    rows = metrics.load_history(HISTORY_FILE)
    long_value = sum(abs(_f(p.get("market_value"))) for p in positions)
    row = {"date": today, "equity": _f(acct.get("equity")), "cash": _f(acct.get("cash")),
           "long_value": long_value, "spy_close": _spy_close(c, today)}
    rows = metrics.upsert_row(rows, row)
    metrics.save_history(HISTORY_FILE, rows)

    prev = [r for r in rows if r["date"] < today]
    prev_eq = prev[-1]["equity"] if prev else _f(acct.get("last_equity"))
    day_pnl = row["equity"] - prev_eq
    inception = metrics.inception_stats(rows)
    d = dt.date.fromisoformat(today)
    wtd = metrics.period_stats(rows, metrics.week_start(d), d)
    week_buys = count_week_buys(state["week_orders"])

    lines = [f"### {today} - EOD Snapshot (PAPER)",
             f"**Portfolio:** ${row['equity']:,.2f} | **Cash:** ${row['cash']:,.2f} "
             f"({row['cash'] / row['equity'] * 100 if row['equity'] else 0:.1f}%) | "
             f"**Day P&L:** {metrics.fmt_money(day_pnl)} ({metrics.fmt_pct(metrics.pct(row['equity'], prev_eq))}) | "
             f"**Since start:** {metrics.fmt_money(inception.get('pnl'))} ({metrics.fmt_pct(inception.get('return_pct'))})",
             f"**Week to date:** {metrics.fmt_pct(wtd.get('return_pct'))} vs SPY {metrics.fmt_pct(wtd.get('spy_return_pct'))} | "
             f"**Since start vs SPY:** {metrics.fmt_pct(inception.get('spy_return_pct'))} | "
             f"**Max drawdown:** {metrics.fmt_pct(inception.get('max_drawdown_pct'))} | "
             f"**Buys this week:** {week_buys}/{load_risk_limits().max_new_trades_per_week}",
             "", "| Ticker | Shares | Entry | Close | Day Chg | Unrealized P&L | Stop |",
             "|---|---|---|---|---|---|---|"]
    for p in sorted(positions, key=lambda x: x["symbol"]):
        stop = _existing_stop_level(state["open_orders"], p["symbol"])
        lines.append(f"| {p['symbol']} | {_f(p.get('qty')):g} | ${_f(p.get('avg_entry_price')):.2f} | "
                     f"${_f(p.get('current_price')):.2f} | {_f(p.get('change_today')) * 100:+.2f}% | "
                     f"{metrics.fmt_money(_f(p.get('unrealized_pl')))} ({_f(p.get('unrealized_plpc')) * 100:+.2f}%) | "
                     f"{'$%.2f' % stop if stop else 'NONE'} |")
    if not positions:
        lines.append("| (no positions) | | | | | | |")
    out("\n".join(lines))
    return EXIT_OK


def _all_fills(c) -> list:
    fills, after = [], None
    for _ in range(20):
        page = c.fills(after=None) if after is None else c._get(
            "/account/activities", {"activity_types": "FILL", "direction": "asc", "page_size": 100, "page_token": after})
        page = page or []
        fills.extend(page)
        if len(page) < 100:
            break
        after = page[-1].get("id")
    return fills


def cmd_weekly_stats(args, client_factory) -> int:
    c = client_factory()
    rows = metrics.load_history(HISTORY_FILE)
    if args.week_ending:
        end = dt.date.fromisoformat(args.week_ending)
    else:
        today = parse_ts(c.clock()["timestamp"]).date()
        end = today - dt.timedelta(days=(today.weekday() - 4) % 7)  # most recent Friday (today if Friday)
    start = metrics.week_start(end)
    week = metrics.period_stats(rows, start, end)
    inception = metrics.inception_stats(rows)
    trades = metrics.closed_trades_from_fills(_all_fills(c))
    week_trades = [t for t in trades if start.isoformat() <= (t.exit_time or "")[:10] <= end.isoformat()]
    ws, alls = metrics.trade_stats(week_trades), metrics.trade_stats(trades)

    def best(s, key):
        t = s[key]
        return f"{t.symbol} {t.return_pct:+.2f}%" if t else "n/a"

    md = [f"## Week ending {end.isoformat()} (PAPER)", "", "### Stats (computed by `xahil weekly-stats`)",
          "| Metric | Value |", "|--------|-------|",
          f"| Starting portfolio | ${week.get('start_equity', 0):,.2f} |" if week.get("available") else "| Starting portfolio | n/a |",
          f"| Ending portfolio | ${week.get('end_equity', 0):,.2f} |" if week.get("available") else "| Ending portfolio | n/a |",
          f"| Week return | {metrics.fmt_money(week.get('pnl'))} ({metrics.fmt_pct(week.get('return_pct'))}) |",
          f"| S&P 500 (SPY) week | {metrics.fmt_pct(week.get('spy_return_pct'))} |",
          f"| Bot vs SPY (week) | {metrics.fmt_pct(week.get('vs_spy_pct'))} |",
          f"| Week max drawdown | {metrics.fmt_pct(week.get('max_drawdown_pct'))} |",
          f"| Since start | {metrics.fmt_money(inception.get('pnl'))} ({metrics.fmt_pct(inception.get('return_pct'))}) |",
          f"| SPY since start | {metrics.fmt_pct(inception.get('spy_return_pct'))} |",
          f"| Max drawdown since start | {metrics.fmt_pct(inception.get('max_drawdown_pct'))} |",
          f"| Closed trades this week | {ws['closed_trades']} (W:{ws['wins']} / L:{ws['losses']}) |",
          f"| Win rate this week | {'n/a' if ws['win_rate_pct'] is None else str(ws['win_rate_pct']) + '%'} |",
          f"| Win rate since start | {'n/a' if alls['win_rate_pct'] is None else str(alls['win_rate_pct']) + '%'} ({alls['closed_trades']} trades) |",
          f"| Best / worst this week | {best(ws, 'best')} / {best(ws, 'worst')} |",
          f"| Profit factor (week) | {ws['profit_factor'] if ws['profit_factor'] is not None else 'n/a'} |",
          "", "### Closed Trades", "| Ticker | Qty | Entry | Exit | P&L | Return |", "|---|---|---|---|---|---|"]
    for t in week_trades:
        md.append(f"| {t.symbol} | {t.qty:g} | ${t.entry_price:.2f} | ${t.exit_price:.2f} | "
                  f"{metrics.fmt_money(t.pnl)} | {t.return_pct:+.2f}% |")
    if not week_trades:
        md.append("| (none) | | | | | |")
    out("\n".join(md))
    return EXIT_OK


def cmd_notify(args, client_factory) -> int:
    msg = args.message if args.message else sys.stdin.read()
    out(notify.send(msg))
    return EXIT_OK


def cmd_research(args, client_factory) -> int:
    try:
        out(research.to_markdown(research.ask(args.query)))
        return EXIT_OK
    except research.ResearchUnavailable as exc:
        print(f"RESEARCH UNAVAILABLE: {exc}. Fall back to WebSearch and note it in the log.", file=sys.stderr)
        return EXIT_UNAVAILABLE


def cmd_guard(args, client_factory) -> int:
    bad = guard.classify(guard.changed_paths())
    if bad:
        out({"ok": False, "blocked_paths": bad,
             "fix": "Agent runs may only change memory/ (not TRADING-STRATEGY.md or PROJECT-CONTEXT.md). "
                    "Revert these with `git checkout -- <path>` (or delete untracked files) and propose changes "
                    "in memory/STRATEGY-PROPOSALS.md instead."})
        return EXIT_ERROR
    out({"ok": True})
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xahil", description="Paper-only trading agent tools")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="verify paper endpoint, credentials, config and integrations")
    sub.add_parser("market-status", help="is today a trading day / is the market open")
    sub.add_parser("account")
    sub.add_parser("positions")
    o = sub.add_parser("orders")
    o.add_argument("status", nargs="?", default="open", choices=["open", "closed", "all"])
    q = sub.add_parser("quote")
    q.add_argument("symbol")
    r = sub.add_parser("reconcile", help="diff broker state vs last snapshot; exit 2 on halt")
    r.add_argument("--workflow", required=True)

    def size_args(sp):
        g = sp.add_mutually_exclusive_group(required=True)
        g.add_argument("--qty", type=float)
        g.add_argument("--notional", type=float)
        sp.add_argument("--limit-price", type=float)

    cb = sub.add_parser("check-buy", help="risk verdict only, never submits")
    cb.add_argument("symbol")
    size_args(cb)
    b = sub.add_parser("buy")
    b.add_argument("symbol")
    size_args(b)
    b.add_argument("--reason", required=True)
    b.add_argument("--workflow", default="manual")
    b.add_argument("--manual", action="store_true", help="local terminal only: type-to-confirm one paper order")
    s = sub.add_parser("sell")
    s.add_argument("symbol")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--qty", type=float)
    g.add_argument("--all", action="store_true")
    s.add_argument("--limit-price", type=float)
    s.add_argument("--reason", required=True)
    s.add_argument("--workflow", default="manual")
    s.add_argument("--manual", action="store_true")
    pr = sub.add_parser("protect", help="place/replace a protective stop (never lowers it)")
    pr.add_argument("symbol")
    g2 = pr.add_mutually_exclusive_group(required=True)
    g2.add_argument("--trail-pct", type=float)
    g2.add_argument("--stop-pct", type=float)
    g2.add_argument("--stop-price", type=float)
    pr.add_argument("--reason", default="")
    pr.add_argument("--workflow", default="manual")
    pr.add_argument("--manual", action="store_true")
    sub.add_parser("midday-scan", help="deterministic cut/tighten/missing-stop plan")
    sub.add_parser("snapshot", help="record EOD equity + SPY, print EOD markdown")
    w = sub.add_parser("weekly-stats")
    w.add_argument("--week-ending")
    n = sub.add_parser("notify")
    n.add_argument("message", nargs="?")
    rs = sub.add_parser("research")
    rs.add_argument("query")
    sub.add_parser("guard", help="block commits outside memory/")
    return p


HANDLERS = {
    "preflight": cmd_preflight, "market-status": cmd_market_status, "account": cmd_read,
    "positions": cmd_read, "orders": cmd_read, "quote": cmd_read, "reconcile": cmd_reconcile,
    "check-buy": cmd_check_buy, "buy": cmd_buy, "sell": cmd_sell, "protect": cmd_protect,
    "midday-scan": cmd_midday_scan, "snapshot": cmd_snapshot, "weekly-stats": cmd_weekly_stats,
    "notify": cmd_notify, "research": cmd_research, "guard": cmd_guard,
}


def main(argv=None, client_factory=None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.command in ("protect",) and args.trail_pct is None and args.stop_pct is None and args.stop_price is None:
        args.stop_pct = 10.0
    factory = client_factory or (lambda: AlpacaClient.from_env())
    try:
        return HANDLERS[args.command](args, factory)
    except ConfigError as exc:
        print(f"CONFIG/SAFETY ERROR (fail closed): {exc}", file=sys.stderr)
        return EXIT_ERROR
    except AlpacaError as exc:
        print(f"ALPACA ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
