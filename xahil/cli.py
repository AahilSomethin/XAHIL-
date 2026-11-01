"""Command-line entry point: `python -m xahil <command>` (`python3` in the cloud).

This is the ONLY path that can submit orders. Every order passes the
deterministic risk engine, and is re-checked on freshly fetched broker state
immediately before submission. Order execution is OFF unless BOTH
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

from xahil import guard, journal, metrics, notify, reconcile, research
from xahil.alpaca import AlpacaClient, AlpacaError
from xahil.config import (
    CONFIG_DIR,
    MEMORY_DIR,
    ROOT,
    STATE_DIR,
    ConfigError,
    _read_json,
    env_halt,
    execution_env_enabled,
    is_cloud_run,
    load_dotenv,
    load_execution_settings,
    load_integrations,
    load_risk_limits,
)
from xahil.risk import (
    OrderRequest,
    RiskContext,
    Verdict,
    _f,
    account_halt_reasons,
    check_stop_not_lowered,
    count_day_trades,
    count_week_buys,
    evaluate_order,
    is_whole,
    parse_ts,
    overnight_protection_gaps,
    position_for,
    protection_gaps,
    symbols_bought_on,
)

SNAPSHOT_FILE = STATE_DIR / "broker_snapshot.json"
HISTORY_FILE = STATE_DIR / "equity_history.csv"
PROPOSALS_FILE = STATE_DIR / "proposed_orders.jsonl"
EXECUTIONS_FILE = STATE_DIR / "executed_orders.jsonl"
HALTS_FILE = STATE_DIR / "halts.json"
RECON_LOG = MEMORY_DIR / "RECONCILIATION-LOG.md"
RESEARCH_LOG = MEMORY_DIR / "RESEARCH-LOG.md"

EXIT_OK, EXIT_ERROR, EXIT_HALT, EXIT_UNAVAILABLE, EXIT_REJECTED = 0, 1, 2, 3, 4
DAYTRADE_LOOKBACK_DAYS = 10  # calendar days; always covers 5 business days (conservative)


def out(obj) -> None:
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, indent=2, default=str))


def alert(message: str) -> dict:
    """Telegram alert that never raises (falls back to the outbox)."""
    try:
        return notify.send(f"XAHIL ALERT (PAPER): {message}")
    except Exception as exc:  # an alert failure must not mask the original problem
        return {"sent": False, "error": type(exc).__name__}


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

def research_section(date: str, path: Path = None) -> str:
    path = RESEARCH_LOG if path is None else path
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(date)}\b.*?(?=^## \d{{4}}-\d{{2}}-\d{{2}}|\Z)", text, re.M | re.S)
    return m.group(0) if m else ""


def research_mentions(date: str, symbol: str, path: Path = None) -> bool:
    return bool(re.search(rf"\b{re.escape(symbol)}\b", research_section(date, path)))


# --- shared state gathering ----------------------------------------------------------

def gather(client: AlpacaClient, symbol: str = None, with_market: bool = False) -> dict:
    clock = client.clock()
    now = parse_ts(clock["timestamp"])
    today = now.date().isoformat()
    state = {
        "clock": clock,
        "now": now,
        "today": today,
        "market_open": clock.get("is_open") is True,
        "account": client.account(),
        "positions": client.positions(),
        "open_orders": client.orders("open"),
    }
    state["week_orders"] = client.orders("all", after=week_start_iso(clock["timestamp"]), side="buy")
    try:
        since = (now - dt.timedelta(days=DAYTRADE_LOOKBACK_DAYS)).isoformat()
        state["recent_fills"] = client.fills(after=since)
    except AlpacaError:
        state["recent_fills"] = None
    state["daytrade_count"], state["daytrade_source"] = resolve_daytrades(state["account"], state["recent_fills"])
    state["same_day_buys"] = (symbols_bought_on(state["recent_fills"], today)
                              if state["recent_fills"] is not None else None)
    if symbol and with_market:
        state["asset"] = client.asset(symbol)
        state["quote"] = client.latest_quote(symbol)
    return state


def resolve_daytrades(account: dict, fills) -> tuple:
    """Broker field when present; otherwise computed from broker fills; the larger if both; None if neither."""
    broker = account.get("daytrade_count")
    broker = int(broker) if isinstance(broker, (int, float)) or (isinstance(broker, str) and broker.isdigit()) else None
    computed = count_day_trades(fills) if fills is not None else None
    if broker is None and computed is None:
        return None, "unavailable"
    if broker is None:
        return computed, "computed_from_fills"
    if computed is None:
        return broker, "broker"
    return max(broker, computed), "broker+fills"


def recon_halts(state: dict = None) -> list:
    """Blocking conditions persisted between runs."""
    reasons = [f"{h['id']}: {h['reason']}" for h in journal.load_halts(HALTS_FILE)]
    reasons += [f"unresolved order {c} (run reconcile)" for c in journal.unresolved(EXECUTIONS_FILE)]
    if journal.corrupt_lines(EXECUTIONS_FILE):
        reasons.append("order journal has corrupt lines; human review needed")
    snap = reconcile.load_snapshot(SNAPSHOT_FILE)
    if snap.get("corrupt"):
        reasons.append("broker snapshot file is corrupt; human review needed")
    elif state and snap.get("account_fingerprint") and             snap["account_fingerprint"] != reconcile.account_fingerprint(state["account"]):
        reasons.append("account number differs from the last snapshot (account reset or swap?)")
    return reasons


def build_context(state: dict, open_orders: list = None, research_ok: bool = False, symbol: str = None) -> RiskContext:
    params = _read_json(CONFIG_DIR / "strategy_params.json")
    evidence = None
    if symbol:
        from xahil import learning
        evidence = learning.evidence_sufficiency(symbol, state["today"], research.requirements(load_integrations()),
                                                 path=learning.EVIDENCE)
    orders = state["open_orders"] if open_orders is None else open_orders
    return RiskContext(
        account=state["account"], positions=state["positions"], open_orders=orders,
        week_orders=state["week_orders"], limits=load_risk_limits(),
        asset=state.get("asset"), quote=state.get("quote"), research_documented=research_ok,
        halt_reasons=recon_halts(state), now=state["now"], market_open=state["market_open"],
        daytrade_count=state["daytrade_count"], same_day_buy_symbols=state["same_day_buys"],
        # Entry gate: every held position must have protection that survives the close (GTC stop, whole shares).
        protection_gaps=overnight_protection_gaps(state["positions"], state["open_orders"]),
        cut_loss_pct=float(params["cut_loss_pct"]),
        require_whole_shares=True,
        research_evidence=evidence,
    )


def manual_confirmation(prompt: str, expected: str, body: dict = None) -> bool:
    """Human-only gate: local interactive terminal, never the cloud, never piped input."""
    if is_cloud_run() or not sys.stdin.isatty():
        return False
    if body is not None:
        print(json.dumps(body, indent=2))
    try:
        typed = input(prompt).strip().upper()
    except (EOFError, KeyboardInterrupt):
        return False
    return typed == expected.upper()


# --- submission ------------------------------------------------------------------------

def _record(path: Path, record: dict) -> None:
    journal.append(path, record)


def _coid(order: OrderRequest, workflow: str, today: str) -> str:
    coid = f"xahil-{today}-{workflow}-{order.symbol}-{order.side}-{order.type}"
    if order.side == "sell":  # sells/stops may legitimately repeat in a day
        coid += "-" + dt.datetime.now(dt.timezone.utc).strftime("%H%M%S%f")[:10]
    return coid[:128]


def _resolve_uncertain(client, coid: str):
    """After an ambiguous response, ask Alpaca whether the order exists. Returns (status, order|None)."""
    try:
        found = client.order_by_client_id(coid)
    except AlpacaError:
        return "uncertain", None
    if found is None:
        return "uncertain", None  # may still be in flight; reconcile resolves it later
    return "recovered_submitted", found


def submit_or_propose(client, order: OrderRequest, evaluate, workflow: str, reason: str, manual: bool) -> int:
    """`evaluate()` -> (Verdict, state, cancel_ids). Called once now and again right before submission."""
    verdict, state, cancel_ids = evaluate()
    coid = _coid(order, workflow, state["today"])
    body = order.to_alpaca(coid)
    record = {"date": state["today"], "workflow": workflow, "client_order_id": coid, "order": body,
              "purpose": order.purpose, "reason": reason, "verdict": verdict.summary(), "checks": verdict.checks,
              "estimated_cost": verdict.estimated_cost, "cancel_first": cancel_ids}
    protective = order.purpose in ("protective", "risk_exit")

    if not verdict.approved:
        record["status"] = "rejected_by_risk_engine"
        _record(PROPOSALS_FILE, record)
        if protective and verdict.checks.get("position_exists", True):
            record["alert"] = alert(f"{order.purpose} order for {order.symbol} rejected by risk engine: "
                                    f"{verdict.summary()[:300]}. New entries stay blocked while any position lacks a stop.")
        elif "positions_protected" in verdict.checks and not verdict.checks["positions_protected"]:
            record["alert"] = alert(f"entry {order.symbol} blocked: a position has no protective stop.")
        out(record)
        return EXIT_REJECTED

    mode, why = execution_mode()
    if mode != "execute" and manual:
        if not [w for w in why if "halt" in w.lower()] and manual_confirmation(
                f"PAPER order. Type {order.symbol} to submit, anything else to cancel: ", order.symbol, body):
            mode = "execute"
    if mode != "execute":
        record["status"] = "dry_run_proposal"
        record["not_executed_because"] = why
        _record(PROPOSALS_FILE, record)
        out(record)
        return EXIT_OK

    # Re-verify on fresh broker state immediately before submitting (no stale approvals).
    verdict2, state2, cancel_ids = evaluate()
    record.update(reverify=verdict2.summary(), cancel_first=cancel_ids)
    if not verdict2.approved:
        record["status"] = "rejected_on_reverify"
        _record(PROPOSALS_FILE, record)
        if protective:
            record["alert"] = alert(f"{order.purpose} order for {order.symbol} failed re-check: {verdict2.summary()[:300]}")
        out(record)
        return EXIT_REJECTED
    if not state2["market_open"]:
        record["status"] = "not_submitted_market_closed"
        _record(PROPOSALS_FILE, record)
        out(record)
        return EXIT_REJECTED

    for oid in cancel_ids:
        try:
            client.cancel_order(oid)
        except AlpacaError as exc:
            record.update(status="cancel_failed", error=str(exc)[:300])
            _record(EXECUTIONS_FILE, record)
            record["alert"] = alert(f"could not cancel existing order {oid[:8]} for {order.symbol}; "
                                    f"{order.purpose} order not sent. Check the position's protection.")
            out(record)
            return EXIT_ERROR

    _record(EXECUTIONS_FILE, {**record, "status": "submitting"})
    try:
        resp = client.submit_order(body)
        status = "rejected_by_broker" if resp.get("status") == "rejected" else "submitted"
    except AlpacaError as exc:
        resp = None
        ambiguous = exc.status == 0 or exc.status >= 500 or exc.status == 429 or (
            exc.status == 422 and "client_order_id" in (exc.body or "").lower())
        if ambiguous:
            status, resp = _resolve_uncertain(client, coid)
        else:
            status = "rejected_by_broker"
        record["broker_error"] = str(exc)[:300]
    record["status"] = status
    if resp:
        record.update(alpaca_order_id=resp.get("id"), alpaca_status=resp.get("status"))
    _record(EXECUTIONS_FILE, record)

    if status == "uncertain":
        record["alert"] = alert(f"order {coid} outcome unknown after a broker/network error. New entries are blocked "
                                f"until reconcile resolves it.")
        out(record)
        return EXIT_HALT
    if status == "rejected_by_broker":
        if protective or cancel_ids:
            record["alert"] = alert(f"broker rejected {order.purpose} order for {order.symbol}. "
                                    f"The position may be unprotected; new entries are blocked until a stop is in place.")
        out(record)
        return EXIT_REJECTED
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
        "max_quote_age_seconds": limits.max_quote_age_seconds,
    }
    mode, why = execution_mode()
    report["execution_mode"] = mode
    report["execution_blockers"] = why
    client = client_factory()
    report["paper_endpoint"] = client.trading_base
    report["auth_mode"] = client.credentials.mode
    acct = client.account()
    report["account"] = {"number_prefix": str(acct.get("account_number", ""))[:2], "status": acct.get("status"),
                         "equity": acct.get("equity"), "cash": acct.get("cash"),
                         "margin_multiplier": acct.get("multiplier"), "shorting_enabled": acct.get("shorting_enabled")}
    report["persistent_halts"] = recon_halts()
    env = os.environ
    tg_problem = notify.config_problem(env)
    report["telegram"] = "configured" if not tg_problem else f"NOT DELIVERABLE ({tg_problem}) - messages go to the outbox"
    from xahil import tavily
    settings = load_integrations()
    tav = {"key": "set" if (env.get("TAVILY_API_KEY") or env.get("XAHIL_TAVILY_AUTH", "").lower() == "proxy")
           else "TAVILY_API_KEY not set"}
    try:
        b = tavily.check_budget(settings, settings.get("tavily", {}).get("search_depth", "basic"),
                                dt.datetime.now(dt.timezone.utc))
        tav.update(status="available" if tav["key"] == "set" else "no key - research falls back to WebSearch",
                   credits_this_month=b["month_credits"], credits_today=b["day_credits"],
                   monthly_cap=settings["tavily"]["monthly_credit_cap"], daily_cap=settings["tavily"]["daily_credit_cap"])
    except tavily.TavilyUnavailable as exc:
        tav["status"] = f"unavailable ({exc}) - use WebSearch"
    report["research"] = {"primary": "tavily", "tavily": tav}
    try:
        research.check_available(settings, env, dt.date.today())
        report["perplexity"] = "enabled"
    except research.ResearchUnavailable as exc:
        report["perplexity"] = f"off ({exc})"
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
        "is_open": clock.get("is_open") is True,
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


def _resolve_journal(c) -> list:
    """Resolve submitting/uncertain journal entries against Alpaca by client_order_id."""
    notes = []
    for coid in journal.unresolved(EXECUTIONS_FILE):
        try:
            found = c.order_by_client_id(coid)
        except AlpacaError as exc:
            notes.append(f"{coid}: still unresolved ({str(exc)[:80]})")
            continue
        if found is None:
            journal.append(EXECUTIONS_FILE, {"client_order_id": coid, "status": "not_placed",
                                             "note": "not found at Alpaca during reconcile"})
            notes.append(f"{coid}: not placed (Alpaca has no such order)")
        else:
            journal.append(EXECUTIONS_FILE, {"client_order_id": coid, "status": "recovered_submitted",
                                             "alpaca_order_id": found.get("id"), "alpaca_status": found.get("status")})
            notes.append(f"{coid}: exists at Alpaca (status {found.get('status')}); journal recovered")
    return notes


def cmd_reconcile(args, client_factory) -> int:
    c = client_factory()
    limits = load_risk_limits()
    state = gather(c)
    prev = reconcile.load_snapshot(SNAPSHOT_FILE)
    if prev.get("corrupt"):
        prev = {}
    as_of = state["clock"]["timestamp"]
    since = prev.get("as_of") if prev.get("as_of") and len(prev.get("as_of", "")) > 16 else None
    fills_since = c.fills(after=since) if since else []
    result = reconcile.reconcile(prev, as_of, args.workflow, state["account"], state["positions"],
                                 state["open_orders"], limits, state["market_open"], fills_since)

    new_halts = []
    if result["account_changed"]:
        if journal.add_halt(HALTS_FILE, "account-changed", "Alpaca account number differs from the last snapshot. "
                            "If you reset or replaced the paper account, run `python -m xahil reset-baseline`."):
            new_halts.append("account changed")
    for item in result["unexplained"]:
        hid = "unexplained-" + re.sub(r"[^a-z0-9]+", "-", item.lower())[:40] + "-" + as_of[:10]
        if journal.add_halt(HALTS_FILE, hid, item + " (acknowledge with `python -m xahil ack-halt` or reset-baseline)"):
            new_halts.append(item)

    result["journal"] = _resolve_journal(c)
    known = journal.known_coids(EXECUTIONS_FILE)
    recent_orders = c.orders("all", after=since or (state["now"] - dt.timedelta(days=7)).isoformat())
    unlogged, foreign = reconcile.foreign_or_unlogged_orders(recent_orders, known)
    for o in unlogged:
        journal.append(EXECUTIONS_FILE, {"client_order_id": o.get("client_order_id"), "status": "recovered_from_broker",
                                         "alpaca_order_id": o.get("id"), "alpaca_status": o.get("status"),
                                         "order": {k: o.get(k) for k in ("symbol", "side", "type", "qty", "notional")}})
        result["journal"].append(f"{o.get('client_order_id')}: found at Alpaca but missing from journal; recovered")
    for o in foreign:
        result["differences"].append(f"order {str(o.get('id'))[:8]} ({o.get('side')} {o.get('symbol')}) "
                                     f"was not placed by XAHIL")

    result["halt_reasons"] = recon_halts(state) + result["halt_reasons"]
    result["ok"] = not result["halt_reasons"]
    if not result["account_changed"]:
        reconcile.save_snapshot(SNAPSHOT_FILE, result["snapshot"])  # keep the old account's snapshot on a change
    if not RECON_LOG.exists():
        RECON_LOG.write_text("# Reconciliation Log\n\nBroker (Alpaca paper) state at the start of every run. "
                             "Alpaca is the source of truth.\n", encoding="utf-8")
    with RECON_LOG.open("a", encoding="utf-8") as fh:
        fh.write(reconcile.format_log_entry(result))

    alerts = []
    if new_halts:
        alerts.append(alert("new halt: " + "; ".join(new_halts)[:500]))
    if result["unprotected_positions"] and args.workflow != "market-open":
        alerts.append(alert(f"positions without a protective stop: {', '.join(result['unprotected_positions'])}. "
                            f"New entries are blocked."))
    elif result["overnight_unprotected"] and args.workflow in ("daily-summary", "pre-market", "weekly-review"):
        alerts.append(alert(f"positions with no overnight (GTC) protection: {', '.join(result['overnight_unprotected'])}. "
                            f"Fractional or DAY-stop positions are unprotected after the close. New entries are blocked; "
                            f"unattended trading must not be activated while this persists."))
    summary = {k: result[k] for k in ("ok", "halt_reasons", "unexplained", "limit_breaches",
                                      "unprotected_positions", "overnight_unprotected", "differences", "journal")}
    summary.update(equity=result["snapshot"]["equity"], cash=result["snapshot"]["cash"],
                   positions=result["snapshot"]["positions"], execution_mode=execution_mode()[0],
                   daytrade_count=state["daytrade_count"], daytrade_source=state["daytrade_source"],
                   alerts_sent=len(alerts))
    out(summary)
    return EXIT_OK if result["ok"] else EXIT_HALT


def _buy_request(args) -> OrderRequest:
    return OrderRequest(side="buy", symbol=args.symbol.upper(), type="limit" if args.limit_price else "market",
                        time_in_force="day", qty=args.qty, notional=args.notional, limit_price=args.limit_price,
                        purpose="entry")


def _buy_evaluator(client, order: OrderRequest):
    def evaluate():
        state = gather(client, order.symbol, with_market=True)
        ctx = build_context(state, research_ok=research_mentions(state["today"], order.symbol), symbol=order.symbol)
        return evaluate_order(order, ctx), state, []
    return evaluate


def cmd_check_buy(args, client_factory) -> int:
    verdict, state, _ = _buy_evaluator(client_factory(), _buy_request(args))()
    out({"verdict": verdict.summary(), "checks": verdict.checks, "estimated_cost": verdict.estimated_cost,
         "daytrade_source": state["daytrade_source"]})
    return EXIT_OK if verdict.approved else EXIT_REJECTED


def cmd_buy(args, client_factory) -> int:
    if not args.reason.strip():
        raise ConfigError("--reason (the catalyst) is required")
    c = client_factory()
    order = _buy_request(args)
    return submit_or_propose(c, order, _buy_evaluator(c, order), args.workflow, args.reason, args.manual)


def _sell_cancel_ids(state, sym):
    # Alpaca reserves shares for open sell orders (stops): those are cancelled first, then the new order sent.
    return [o["id"] for o in state["open_orders"] if o.get("symbol") == sym and o.get("side") == "sell"]


def cmd_sell(args, client_factory) -> int:
    if not args.reason.strip():
        raise ConfigError("--reason is required")
    c = client_factory()
    sym = args.symbol.upper()
    purpose = "risk_exit" if args.risk_exit else "discretionary"
    holder = {}

    def evaluate():
        state = gather(c)
        pos = position_for(state["positions"], sym)
        qty = (_f(pos.get("qty")) if pos else 0.0) if args.all else args.qty
        order = OrderRequest(side="sell", symbol=sym, type="limit" if args.limit_price else "market",
                             time_in_force="day", qty=qty, limit_price=args.limit_price, purpose=purpose)
        holder["order"] = order
        cancel_ids = _sell_cancel_ids(state, sym)
        remaining = [o for o in state["open_orders"] if o.get("id") not in cancel_ids]
        return evaluate_order(order, build_context(state, open_orders=remaining)), state, cancel_ids

    first = evaluate()
    order = holder["order"]
    return submit_or_propose(c, order, _once_then(first, evaluate), args.workflow, args.reason, args.manual)


def _once_then(first, evaluate):
    """Return a callable that yields `first` on the first call and fresh results afterwards."""
    calls = {"n": 0}

    def wrapped():
        calls["n"] += 1
        return first if calls["n"] == 1 else evaluate()
    return wrapped


def _existing_stop_level(orders: list, sym: str):
    levels = [_f(o.get("stop_price")) for o in orders
              if o.get("symbol") == sym and o.get("side") == "sell" and o.get("type") in ("stop", "trailing_stop")
              and _f(o.get("stop_price")) > 0]
    return max(levels) if levels else None


def protective_order(pos: dict, trail_pct, stop_pct, stop_price) -> tuple:
    """Pick an order Alpaca actually supports for this position. Returns (order, new_level, note)."""
    sym, qty, price = pos["symbol"], _f(pos.get("qty")), _f(pos.get("current_price"))
    if is_whole(qty) and trail_pct is not None:
        return (OrderRequest("sell", sym, type="trailing_stop", time_in_force="gtc", qty=qty,
                             trail_percent=trail_pct, purpose="protective"),
                price * (1 - trail_pct / 100), "whole shares: GTC trailing stop")
    pct = trail_pct if trail_pct is not None else stop_pct
    level = stop_price or round(price * (1 - pct / 100), 2)
    if is_whole(qty):
        return (OrderRequest("sell", sym, type="stop", time_in_force="gtc", qty=qty, stop_price=level,
                             purpose="protective"), level, "whole shares: GTC stop")
    note = ("fractional shares: Alpaca allows DAY stops only (no GTC, no trailing). This stop expires at the close; "
            "it must be re-placed every session and the position is unprotected overnight")
    return (OrderRequest("sell", sym, type="stop", time_in_force="day", qty=qty, stop_price=level,
                         purpose="protective"), level, note)


def cmd_protect(args, client_factory) -> int:
    c = client_factory()
    params = _read_json(CONFIG_DIR / "strategy_params.json")
    sym = args.symbol.upper()
    holder = {}

    def evaluate():
        state = gather(c)
        pos = position_for(state["positions"], sym)
        if not pos:
            holder["order"] = holder.get("order") or OrderRequest("sell", sym, type="stop", qty=0, purpose="protective")
            return Verdict(False, [f"no position in {sym} (if a buy was just sent, wait for the fill and retry)"],
                           {"position_exists": False}), state, []
        order, new_level, note = protective_order(pos, args.trail_pct, args.stop_pct, args.stop_price)
        holder.update(order=order, note=note)
        cancel_ids = _sell_cancel_ids(state, sym)
        remaining = [o for o in state["open_orders"] if o.get("id") not in cancel_ids]
        verdict = evaluate_order(order, build_context(state, open_orders=remaining))
        extra = []
        lowered = check_stop_not_lowered(_existing_stop_level(state["open_orders"], sym), new_level)
        if lowered:
            extra.append(lowered)
        price = _f(pos.get("current_price"))
        min_gap = float(params.get("min_stop_distance_pct", 3.0))
        if price and new_level > price * (1 - min_gap / 100) + 1e-9:
            extra.append(f"stop {new_level:.2f} is within {min_gap}% of price {price:.2f}")
        if extra:
            verdict = Verdict(False, verdict.reasons + extra, {**verdict.checks, "strategy_stop_rules": False})
        return verdict, state, cancel_ids

    first = evaluate()
    if "note" in holder:
        print(f"NOTE: {holder['note']}", file=sys.stderr)
    return submit_or_propose(c, holder["order"], _once_then(first, evaluate), args.workflow,
                             args.reason or "protective stop", args.manual)


def midday_plan(positions: list, open_orders: list, params: dict) -> list:
    actions = []
    cut = float(params["cut_loss_pct"])
    tiers = sorted(params["trail_tightening"], key=lambda t: -t["gain_pct"])
    for p in positions:
        sym, plpc = p["symbol"], _f(p.get("unrealized_plpc")) * 100
        if plpc <= cut:
            actions.append({"symbol": sym, "action": "SELL_ALL", "why": f"{plpc:.2f}% <= {cut}% cut rule",
                            "command": f"sell {sym} --all --risk-exit --reason \"cut at {cut}% per rule\""})
            continue
        for tier in tiers:
            if plpc >= tier["gain_pct"]:
                actions.append({"symbol": sym, "action": "TIGHTEN_STOP", "why": f"+{plpc:.2f}% >= +{tier['gain_pct']}%",
                                "command": f"protect {sym} --trail-pct {tier['trail_pct']} --reason \"tighten at +{tier['gain_pct']}%\""})
                break
        qty = _f(p.get("qty"))
        if not is_whole(qty):
            actions.append({"symbol": sym, "action": "FRACTIONAL_NO_OVERNIGHT_PROTECTION",
                            "why": "fractional position: only DAY stops are possible, so it is unprotected every night "
                                   "and blocks all new entries",
                            "command": f"sell {sym} --all --reason \"fractional position cannot hold a GTC stop\"",
                            "note": "discretionary exit: refused if bought today; otherwise Claude decides and logs why"})
            continue
        has_gtc = any(o.get("symbol") == sym and o.get("side") == "sell" and o.get("type") in ("stop", "trailing_stop")
                      and o.get("time_in_force") == "gtc" for o in open_orders)
        if not has_gtc:
            actions.append({"symbol": sym, "action": "ADD_STOP", "why": "no GTC protective stop on file",
                            "command": f"protect {sym} --trail-pct {params['initial_stop_pct']} --reason \"missing stop\""})
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
    halts = account_halt_reasons(acct, positions, load_risk_limits()) + recon_halts(state)
    if halts:
        # Never record a bad baseline (e.g. Alpaca's default $100k, or a just-reset account) into the history.
        out({"recorded": False, "halt_reasons": halts,
             "note": "Equity history not updated while the account is in a halt state."})
        return EXIT_HALT
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
                     f"{'$%.2f' % stop if stop else 'NONE (fractional DAY stop expired at close)' if not is_whole(_f(p.get('qty'))) else 'NONE'} |")
    if not positions:
        lines.append("| (no positions) | | | | | | |")
    out("\n".join(lines))
    return EXIT_OK


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
    trades = metrics.closed_trades_from_fills(c.fills())
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


def cmd_telegram_setup(args, client_factory) -> int:
    out({"config_problem": notify.config_problem() or None, **notify.discover_chats()})
    return EXIT_OK


def cmd_notify(args, client_factory) -> int:
    msg = args.message if args.message else sys.stdin.read()
    out(notify.send(msg))
    return EXIT_OK


EXIT_INSUFFICIENT = 6


def cmd_research(args, client_factory) -> int:
    try:
        result = research.ask(args.query, provider=args.provider, topic=args.topic, time_range=args.time_range,
                              max_results=args.max_results)
    except research.ResearchUnavailable as exc:
        print(f"RESEARCH UNAVAILABLE: {exc}. Fall back to WebSearch, record each source with "
              f"`xahil evidence ... --published YYYY-MM-DD`, and note the fallback in the log. "
              f"Missing research is not a signal: default HOLD.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    out(research.to_markdown(result))
    logged = []
    if args.log:
        from xahil import learning
        kind = {"finance": "data"}.get(result.get("topic"), "news") if result["provider"] == "tavily" else "perplexity"
        for s in result["sources"]:
            claim = s.get("title") or (result.get("answer") or "")[:400] or args.query
            rec = learning.record_evidence(args.symbol or "MARKET", claim, s["url"], kind, args.workflow,
                                           path=learning.EVIDENCE, published_date=s.get("published_date"),
                                           retrieved_at=s.get("retrieved_at"), provider=result["provider"],
                                           query=args.query, title=s.get("title"), snippet=s.get("snippet"))
            logged.append(rec["id"])
        print(f"\nEvidence logged: {', '.join(logged) or 'none'}")
    if not result["sufficiency"]["sufficient"]:
        print("INSUFFICIENT RESEARCH: " + "; ".join(result["sufficiency"]["reasons"])
              + ". Treat as no information (not bullish). Default HOLD.", file=sys.stderr)
        return EXIT_INSUFFICIENT
    return EXIT_OK


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


def cmd_halts(args, client_factory) -> int:
    out({"persistent_halts": journal.load_halts(HALTS_FILE),
         "unresolved_orders": journal.unresolved(EXECUTIONS_FILE)})
    return EXIT_OK


def cmd_ack_halt(args, client_factory) -> int:
    halts = journal.load_halts(HALTS_FILE)
    ids = None if args.all else [args.id]
    targets = [h for h in halts if ids is None or h["id"] in ids]
    if not targets:
        out({"cleared": [], "note": "no matching halt"})
        return EXIT_OK
    for h in targets:
        print(f"- {h['id']}: {h['reason']}")
    if not manual_confirmation("Human acknowledgement required. Type ACK to clear: ", "ACK"):
        out({"cleared": [], "note": "not confirmed (needs a local interactive terminal; never in the cloud)"})
        return EXIT_REJECTED
    removed = journal.clear_halts(HALTS_FILE, [h["id"] for h in targets])
    _log_human_action(f"halts acknowledged: {', '.join(h['id'] for h in removed)}")
    out({"cleared": [h["id"] for h in removed]})
    return EXIT_OK


def cmd_reset_baseline(args, client_factory) -> int:
    """After a paper-account reset: archive history, re-baseline from the broker. Human-only."""
    c = client_factory()
    acct = c.account()
    positions, open_orders = c.positions(), c.orders("open")
    print(f"Broker now shows equity ${_f(acct.get('equity')):,.2f}, cash ${_f(acct.get('cash')):,.2f}, "
          f"{len(positions)} positions, {len(open_orders)} open orders.")
    pending = journal.unresolved(EXECUTIONS_FILE)
    if pending:
        out({"reset": False, "note": f"unresolved orders {pending}; run reconcile first"})
        return EXIT_REJECTED
    if not manual_confirmation("This archives equity history and clears halts. Type RESET to continue: ", "RESET"):
        out({"reset": False, "note": "not confirmed (needs a local interactive terminal; never in the cloud)"})
        return EXIT_REJECTED
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = []
    for path in (HISTORY_FILE, SNAPSHOT_FILE):
        if path.exists():
            dest = path.with_name(f"{path.stem}.archived-{stamp}{path.suffix}")
            path.replace(dest)
            archived.append(dest.name)
    cleared = journal.clear_halts(HALTS_FILE, None)
    clock = c.clock()
    snap = reconcile.build_snapshot(clock["timestamp"], "reset-baseline", acct, positions, open_orders)
    reconcile.save_snapshot(SNAPSHOT_FILE, snap)
    _log_human_action(f"reset-baseline: archived {archived}; cleared {len(cleared)} halts; "
                      f"new baseline equity ${snap['equity']:,.2f}")
    out({"reset": True, "archived": archived, "cleared_halts": [h["id"] for h in cleared],
         "baseline_equity": snap["equity"]})
    return EXIT_OK


def _log_human_action(text: str) -> None:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not RECON_LOG.exists():
        RECON_LOG.write_text("# Reconciliation Log\n", encoding="utf-8")
    with RECON_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {stamp} - HUMAN ACTION\n- {text}\n")


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
    s.add_argument("--risk-exit", action="store_true",
                   help="cut-loss exit; verified by the engine (unrealized P&L at or below the cut level)")
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
    sub.add_parser("telegram-setup", help="read-only: find your Telegram chat id after messaging the bot")
    n = sub.add_parser("notify")
    n.add_argument("message", nargs="?")
    rs = sub.add_parser("research")
    rs.add_argument("query")
    rs.add_argument("--log", action="store_true", help="record each source in the evidence log")
    rs.add_argument("--topic", choices=["news", "finance", "general"], help="Tavily topic (default news)")
    rs.add_argument("--time-range", choices=["day", "week", "month", "year"], help="Tavily window (default week)")
    rs.add_argument("--max-results", type=int)
    rs.add_argument("--provider", choices=["tavily", "perplexity"], help="force one provider")
    rs.add_argument("--symbol")
    rs.add_argument("--workflow", default="manual")
    sub.add_parser("guard", help="block commits outside memory/")
    sub.add_parser("halts", help="show persistent halts and unresolved orders")
    ah = sub.add_parser("ack-halt", help="HUMAN ONLY: acknowledge persistent halts in a local terminal")
    g3 = ah.add_mutually_exclusive_group(required=True)
    g3.add_argument("--id")
    g3.add_argument("--all", action="store_true")
    sub.add_parser("reset-baseline", help="HUMAN ONLY: re-baseline after a paper account reset")
    from xahil import cli_intel  # late import: cli_intel imports this module
    cli_intel.add_parsers(sub)
    return p


HANDLERS = {
    "preflight": cmd_preflight, "market-status": cmd_market_status, "account": cmd_read,
    "positions": cmd_read, "orders": cmd_read, "quote": cmd_read, "reconcile": cmd_reconcile,
    "check-buy": cmd_check_buy, "buy": cmd_buy, "sell": cmd_sell, "protect": cmd_protect,
    "midday-scan": cmd_midday_scan, "snapshot": cmd_snapshot, "weekly-stats": cmd_weekly_stats,
    "notify": cmd_notify, "telegram-setup": cmd_telegram_setup, "research": cmd_research, "guard": cmd_guard, "halts": cmd_halts,
    "ack-halt": cmd_ack_halt, "reset-baseline": cmd_reset_baseline,
}


def main(argv=None, client_factory=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252; research text is UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    load_dotenv()
    args = build_parser().parse_args(argv)
    factory = client_factory or (lambda: AlpacaClient.from_env())
    from xahil import cli_intel
    handlers = {**HANDLERS, **cli_intel.HANDLERS}
    try:
        return handlers[args.command](args, factory)
    except ConfigError as exc:
        print(f"CONFIG/SAFETY ERROR (fail closed): {exc}", file=sys.stderr)
        return EXIT_ERROR
    except AlpacaError as exc:
        print(f"ALPACA ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:  # invalid user/agent input to a memory command (bad URL, date, action)
        print(f"INVALID INPUT: {exc}", file=sys.stderr)
        return EXIT_ERROR
