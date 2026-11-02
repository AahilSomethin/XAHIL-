"""Trading-intelligence and run-control commands (registered into `python -m xahil`).

Analysis: regime, signals, backtest, size. Memory: decide, evidence, mistake, learn.
Run control: run-claim, memory-sync, run-start, run-finish, persist, state-audit, state-recover,
activation-check. None of these place orders.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from xahil import backtest as bt
from xahil import cli, data, journal, learning, metrics, persist, regime, runs, signals
from xahil.config import CONFIG_DIR, MEMORY_DIR, ROOT, STATE_DIR, _read_json, is_cloud_run, load_risk_limits
from xahil.risk import _f, overnight_protection_gaps, parse_ts
from xahil.ta import Indicators

BACKTEST_DIR = MEMORY_DIR / "backtests"
BACKTEST_INDEX = STATE_DIR / "backtests.jsonl"
HISTORY_DAYS = 450  # calendar days of bars for indicators (SMA200 needs ~290)


def _params():
    return _read_json(CONFIG_DIR / "signal_params.json"), _read_json(CONFIG_DIR / "strategy_params.json")


def _universe(symbols=None) -> list:
    if symbols:
        return [s.upper() for s in symbols]
    return [s.upper() for s in _read_json(CONFIG_DIR / "universe.json")["symbols"]]


def _now_et(c) -> dt.datetime:
    return parse_ts(c.clock()["timestamp"])


def _sizing_equity(acct: dict, limits) -> tuple:
    equity = _f(acct.get("equity"))
    cap = limits.starting_capital * limits.max_equity_multiple_of_start
    if equity <= 0 or equity > cap:
        return limits.starting_capital, f"broker equity {equity:,.2f} outside the valid range; sizing at the configured {limits.starting_capital:,.0f}"
    return equity, None


# --- analysis ----------------------------------------------------------------------

def cmd_regime(args, client_factory) -> int:
    c = client_factory()
    sig, _ = _params()
    now = _now_et(c)
    end = now.date().isoformat()
    start = (now.date() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    bars = c.daily_bars(sig["regime"]["benchmark"], start=start, end=end)
    r = regime.classify(bars, sig).to_dict()
    r["benchmark"] = sig["regime"]["benchmark"]
    r["note"] = "advisory: informs research; not an entry gate unless the owner approves one"
    if args.record:
        learning.record_regime(r, args.workflow, path=learning.REGIMES)
    cli.out(r)
    return cli.EXIT_OK


def cmd_signals(args, client_factory) -> int:
    c = client_factory()
    sig, _ = _params()
    limits = load_risk_limits()
    now = _now_et(c)
    end, start = now.date().isoformat(), (now.date() - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    universe = [args.explain.upper()] if args.explain else _universe(args.symbols)
    bench_sym = sig["regime"]["benchmark"]
    bars, errors = data.get_many(c, universe + [bench_sym], start, end)
    bench_bars = bars.get(bench_sym) or []
    errors.update({s: "no bars returned (delisted or no IEX data)" for s in universe if s not in errors and not bars.get(s)})
    reg = regime.classify(bench_bars, sig).regime if bench_bars else regime.UNKNOWN
    equity, note = _sizing_equity(c.account(), limits)
    budget = equity * limits.max_position_pct / 100
    results = [signals.evaluate(s, bars[s], bench_bars, sig, position_budget=budget,
                                price_buffer_pct=limits.price_buffer_pct, regime=reg)
               for s in universe if bars.get(s)]
    results.sort(key=lambda s: (not s.qualified, -s.score, s.symbol))
    if args.record:
        learning.record_signals(results, args.workflow, path=learning.SIGNALS)
    if args.explain:
        target = next((s for s in results if s.symbol == args.explain.upper()), None)
        cli.out(target.to_dict() if target else {"error": f"{args.explain} not evaluated"})
        return cli.EXIT_OK
    cli.out({"as_of": end, "regime": reg, "position_budget": round(budget, 2), "sizing_note": note,
             "advisory": "signals inform research only; every buy still needs a dated catalyst and the risk engine",
             "qualified": [s.summary() for s in results if s.qualified][:args.top],
             "near_misses": [s.summary() for s in results if not s.qualified and s.score >= 0.7][:args.top],
             "evaluated": len(results), "data_errors": errors, "recorded": bool(args.record)})
    return cli.EXIT_OK


def cmd_backtest(args, client_factory) -> int:
    c = client_factory()
    sig, strat = _params()
    limits = load_risk_limits()
    end = args.end or (_now_et(c).date() - dt.timedelta(days=1)).isoformat()
    start = args.start
    warm = (dt.date.fromisoformat(start) - dt.timedelta(days=HISTORY_DAYS)).isoformat()
    universe = _universe(args.symbols)
    bench_sym = sig["regime"]["benchmark"]
    bars, errors = data.get_many(c, universe + [bench_sym], warm, end)
    bench = bars.pop(bench_sym, None)
    if not bench:
        cli.out({"error": "no benchmark bars", "data_errors": errors})
        return cli.EXIT_ERROR
    res = bt.out_of_sample(bars, bench, sig, strat, limits, start, end, limits.starting_capital, args.regime_filter)
    if "error" in res:
        cli.out(res)
        return cli.EXIT_ERROR
    summary = bt.summary_dict(res)
    summary.update(universe=universe, regime_filter=args.regime_filter, data_errors=errors,
                   generated=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    if args.save:
        BACKTEST_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H%MZ")
        path = BACKTEST_DIR / f"{stamp}{'-regime' if args.regime_filter else ''}.md"
        path.write_text(bt.report_markdown(res, universe, args.regime_filter, stamp), encoding="utf-8")
        try:
            rel = str(path.relative_to(MEMORY_DIR.parent)).replace("\\", "/")
        except ValueError:
            rel = str(path)
        journal.append(BACKTEST_INDEX, {**summary, "report": rel})
        summary["report"] = str(path)
    cli.out(summary)
    return cli.EXIT_OK


def cmd_size(args, client_factory) -> int:
    c = client_factory()
    limits = load_risk_limits()
    sym = args.symbol.upper()
    q = c.latest_quote(sym)
    ask = _f(q.get("ap"))
    equity, note = _sizing_equity(c.account(), limits)
    cap = equity * limits.max_position_pct / 100
    unit = ask * (1 + limits.price_buffer_pct / 100)
    shares = int(cap // unit) if unit > 0 else 0
    cli.out({"symbol": sym, "ask": ask, "position_cap": round(cap, 2), "max_whole_shares": shares,
             "est_cost": round(shares * unit, 2), "sizing_note": note,
             "note": "whole shares only: fractional positions cannot carry a GTC stop overnight"
             if shares else f"no whole share of {sym} fits the {limits.max_position_pct}% cap; skip it"})
    return cli.EXIT_OK if shares else cli.EXIT_REJECTED


# --- memory --------------------------------------------------------------------

def cmd_decide(args, client_factory) -> int:
    rec = learning.record_decision(args.symbol, args.action, args.reason, args.workflow, args.evidence or [],
                                   path=learning.DECISIONS, evidence_path=learning.EVIDENCE)
    cli.out(rec)
    return cli.EXIT_OK if not rec["unknown_evidence_ids"] else cli.EXIT_REJECTED


def cmd_evidence(args, client_factory) -> int:
    cli.out(learning.record_evidence(args.symbol, args.claim, args.source, args.kind, args.workflow,
                                     path=learning.EVIDENCE, published_date=args.published,
                                     provider=args.provider, query=args.query))
    return cli.EXIT_OK


def cmd_research_check(args, client_factory) -> int:
    from xahil import research
    from xahil.config import load_integrations
    date = args.date or learning.et_today()
    res = learning.evidence_sufficiency(args.symbol, date, research.requirements(load_integrations()),
                                        path=learning.EVIDENCE)
    cli.out({"symbol": args.symbol.upper(), "date": date, **res})
    return cli.EXIT_OK if res["sufficient"] else 6


def cmd_mistake(args, client_factory) -> int:
    cli.out(learning.record_mistake(args.title, args.what, args.lesson, args.symbol or "",
                                    path=learning.MISTAKES, md_path=learning.MISTAKES_MD))
    return cli.EXIT_OK


def cmd_learn(args, client_factory) -> int:
    c = client_factory()
    sig, strat = _params()
    if args.week_ending:
        end = dt.date.fromisoformat(args.week_ending)
    else:
        today = _now_et(c).date()
        end = today - dt.timedelta(days=(today.weekday() - 4) % 7)
    start = end - dt.timedelta(days=4)
    lookback = end - dt.timedelta(days=60)
    recs = {name: journal.read(path) for name, path in (
        ("signals", learning.SIGNALS), ("decisions", learning.DECISIONS), ("evidence", learning.EVIDENCE),
        ("mistakes", learning.MISTAKES), ("regimes", learning.REGIMES), ("proposals", cli.PROPOSALS_FILE))}
    syms = learning.symbols_needing_bars(recs["signals"], recs["proposals"], lookback.isoformat())
    bars, errors = data.get_many(c, sorted(syms), lookback.isoformat(),
                                 (_now_et(c).date()).isoformat()) if syms else ({}, {})
    trades = [{"symbol": t.symbol, "entry_time": t.entry_time or "", "exit_time": t.exit_time or "",
               "return_pct": t.return_pct} for t in metrics.closed_trades_from_fills(c.fills())]
    a = learning.weekly_analysis(start.isoformat(), end.isoformat(), closed_trades=trades, bars=bars,
                                 default_stop_pct=float(strat["initial_stop_pct"]), **recs)
    text = learning.analysis_markdown(a)
    if errors:
        text += f"\n_Data errors: {errors}_\n"
    cli.out(text)
    return cli.EXIT_OK


# --- run control ----------------------------------------------------------------

def _session(c, date: str):
    cal = c.calendar(date, date)
    return cal[0] if cal and cal[0].get("date") == date else None


REPO = ROOT  # the Git checkout persistence works on (tests point this at a temporary repo)


def _persist_failure(exc: persist.PersistError, what: str, extra: dict = None) -> int:
    """Report a typed persistence failure. Every kind except a lost claim also alerts the owner."""
    report = {"ok": False, "step": what, "kind": exc.kind, "error": str(exc), **(extra or {})}
    if exc.kind == persist.CLAIM_LOST:
        report["decision"] = "skip_claim_lost"
    else:
        note = (extra or {}).get("recovery") or {}
        where = f" Recovery copy on {note['branch']} ({note['commit'][:12]})." if note.get("recovery_pushed") else ""
        cli.alert(f"{what} failed ({exc.kind}): {str(exc)[:250]}.{where} Run stopped; nothing was retried.")
    cli.out(report)
    return exc.exit_code()


def cmd_memory_sync(args, client_factory) -> int:
    try:
        res = persist.memory_sync(REPO)
    except persist.PersistError as exc:
        return _persist_failure(exc, "memory-sync")
    cli.out(res)
    return cli.EXIT_OK


def cmd_run_claim(args, client_factory) -> int:
    """memory-sync, then the run-start decision, then push the claim marker. PROCEED only once verified."""
    try:
        sync = persist.memory_sync(REPO)
    except persist.PersistError as exc:
        return _persist_failure(exc, "run-claim (memory-sync)")
    c = client_factory()
    now = _now_et(c)
    res = runs.start(args.workflow, now, _session(c, now.date().isoformat()), runs_dir=runs.RUNS_DIR,
                     force_recover=args.force_recover)
    res["state_commit"] = sync["state_commit"]
    if res["decision"] != runs.PROCEED:
        cli.out(res)
        return 5
    marker = res["marker"]
    rel = Path(res["path"]).resolve().relative_to(Path(REPO).resolve()).as_posix()
    try:
        pushed = persist.persist(f"run-claim {args.workflow} {marker['date']} {marker['run_id']}", repo=REPO,
                                 claim={"path": rel, "run_id": marker["run_id"]})
    except persist.PersistError as exc:
        persist.restore_path(REPO, rel)  # an unverified claim must not stay behind locally
        return _persist_failure(exc, "run-claim", {"workflow": args.workflow, "run_id": marker["run_id"]})
    res.update(claim_commit=pushed["commit"], claim_verified=True)
    cli.out(res)
    return cli.EXIT_OK


def cmd_run_start(args, client_factory) -> int:
    c = client_factory()
    now = _now_et(c)
    res = runs.start(args.workflow, now, _session(c, now.date().isoformat()), runs_dir=runs.RUNS_DIR,
                     force_recover=args.force_recover)
    res["note"] = "local marker only (not pushed); cloud runs use run-claim"
    cli.out(res)
    return cli.EXIT_OK if res["decision"] == runs.PROCEED else 5


def cmd_run_finish(args, client_factory) -> int:
    c = client_factory()
    res = runs.finish(args.workflow, _now_et(c), args.status, args.summary or "", runs_dir=runs.RUNS_DIR)
    cli.out(res)
    return cli.EXIT_OK if res["ok"] else cli.EXIT_ERROR


def cmd_persist(args, client_factory) -> int:
    try:
        res = persist.persist(args.message, repo=REPO, dry_run=args.dry_run)
    except persist.PersistError as exc:
        recovery = None
        if exc.kind != persist.UNSAFE and not args.dry_run:
            try:
                recovery = persist.save_recovery(REPO, args.message, exc)
            except persist.PersistError as rexc:
                recovery = {"recovery_pushed": False, "reason": f"{rexc.kind}: {str(rexc)[:200]}"}
        return _persist_failure(exc, "persist", {"persisted": False, "recovery": recovery})
    cli.out(res)
    return cli.EXIT_OK


def cmd_state_audit(args, client_factory) -> int:
    try:
        res = persist.audit(REPO)
    except persist.PersistError as exc:
        cli.out({"ok": False, "kind": exc.kind, "error": str(exc)})
        return exc.exit_code()
    cli.out(res)
    return cli.EXIT_OK if res["ok"] else cli.EXIT_ERROR


def cmd_state_recover(args, client_factory) -> int:
    """Owner only: merge a recovery copy from a session branch into the state branch."""
    if is_cloud_run() or not cli.manual_confirmation(
            f"Merge the recovery copy on {args.branch} into the state branch? Type RECOVER: ", "RECOVER"):
        cli.out({"recovered": False, "note": "not confirmed (owner only, local interactive terminal, never the cloud)"})
        return cli.EXIT_REJECTED
    try:
        res = persist.recover(REPO, args.branch)
    except persist.PersistError as exc:
        cli.out({"recovered": False, "kind": exc.kind, "error": str(exc)})
        return exc.exit_code()
    cli.out({"recovered": True, **res})
    return cli.EXIT_OK


def activation_report(acct: dict, positions: list, open_orders: list, limits, env, halts: list,
                      completed_summaries: int, completed_reviews: int, latest_backtest: dict = None,
                      snapshot_exists: bool = False) -> dict:
    items = []

    def item(name, ok, detail, blocking=True):
        items.append({"check": name, "ok": bool(ok), "blocking": blocking, "detail": detail})

    equity = _f(acct.get("equity"))
    item("paper_account", str(acct.get("account_number", "")).startswith("PA"), "Alpaca paper account (PA...)")
    item("baseline_equity", 0.5 * limits.starting_capital <= equity <= limits.max_equity_multiple_of_start * limits.starting_capital,
         f"equity {equity:,.2f} vs configured start {limits.starting_capital:,.0f}")
    item("reconciled_baseline", snapshot_exists, "a broker snapshot exists in memory/state")
    item("no_halts", not halts, "; ".join(halts) or "none")
    gaps = overnight_protection_gaps(positions, open_orders)
    item("overnight_protection", not gaps,
         "every position is whole shares with an open GTC stop" if not gaps else f"UNPROTECTED overnight: {gaps}")
    from xahil import notify
    tg = notify.config_problem(env)
    item("telegram", not tg, "alerts can reach you (config check; send a real test with `notify`)" if not tg else tg)
    item("dry_run_track_record", completed_summaries >= 5 and completed_reviews >= 1,
         f"{completed_summaries} completed daily summaries (need 5), {completed_reviews} weekly reviews (need 1)")
    if latest_backtest:
        v = latest_backtest.get("assessment", {}).get("verdict", "unknown")
        item("backtest_evidence", not v.startswith("fails"), f"latest saved backtest: {v}", blocking=False)
    else:
        item("backtest_evidence", False, "no saved backtest (run `backtest --save`)", blocking=False)
    item("owner_approval", False, "execution switches stay OFF until the owner sets both "
         "(config/execution.json + XAHIL_ORDER_EXECUTION). This tool never sets them.", blocking=False)
    ready = all(i["ok"] for i in items if i["blocking"])
    return {"ready_for_owner_activation_decision": ready, "items": items,
            "note": "READY means the preconditions hold; activation itself is a separate owner decision."}


def cmd_activation_check(args, client_factory) -> int:
    c = client_factory()
    limits = load_risk_limits()
    latest = journal.read(BACKTEST_INDEX)
    rep = activation_report(c.account(), c.positions(), c.orders("open"), limits, os.environ,
                            cli.recon_halts(), runs.completed_count("daily-summary", runs.RUNS_DIR),
                            runs.completed_count("weekly-review", runs.RUNS_DIR),
                            latest[-1] if latest else None, cli.SNAPSHOT_FILE.exists())
    cli.out(rep)
    return cli.EXIT_OK if rep["ready_for_owner_activation_decision"] else cli.EXIT_REJECTED


# --- registration -----------------------------------------------------------------

def add_parsers(sub) -> None:
    r = sub.add_parser("regime", help="advisory market regime from SPY")
    r.add_argument("--record", action="store_true")
    r.add_argument("--workflow", default="manual")
    s = sub.add_parser("signals", help="explainable advisory signals for the universe")
    s.add_argument("--symbols", nargs="*")
    s.add_argument("--record", action="store_true", help="log every evaluation (qualified and rejected)")
    s.add_argument("--workflow", default="manual")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--explain", type=cli.filled, help="print every check for one symbol")
    b = sub.add_parser("backtest", help="historical backtest with an out-of-sample split (read-only data)")
    b.add_argument("--start", default="2018-01-02")
    b.add_argument("--end")
    b.add_argument("--symbols", nargs="*")
    b.add_argument("--regime-filter", action="store_true")
    b.add_argument("--save", action="store_true", help="write a report to memory/backtests/")
    z = sub.add_parser("size", help="max whole shares within the position cap")
    z.add_argument("symbol", type=cli.filled)
    d = sub.add_parser("decide", help="record a decision in the Git-backed decision log")
    d.add_argument("symbol", type=cli.filled)
    d.add_argument("--action", required=True, choices=learning.ACTIONS)
    d.add_argument("--reason", required=True, type=cli.filled)
    d.add_argument("--evidence", nargs="+", type=cli.filled,
                   help="one or more evidence ids; omit the flag entirely when there are none")
    d.add_argument("--workflow", default="manual")
    e = sub.add_parser("evidence", help="record a research claim with its source URL")
    e.add_argument("symbol", type=cli.filled)
    e.add_argument("--claim", required=True, type=cli.filled)
    e.add_argument("--source", required=True, type=cli.filled)
    e.add_argument("--kind", default="web", choices=["news", "filing", "data", "web", "perplexity"])
    e.add_argument("--workflow", default="manual")
    e.add_argument("--published", help="source publication date YYYY-MM-DD (needed for freshness)")
    e.add_argument("--provider", default="websearch", choices=["websearch", "tavily", "perplexity", "manual"])
    e.add_argument("--query", type=cli.filled)
    rq = sub.add_parser("research-check", help="is today's recorded evidence for SYM sufficient for an entry?")
    rq.add_argument("symbol", type=cli.filled)
    rq.add_argument("--date")
    m = sub.add_parser("mistake", help="record a mistake and lesson")
    m.add_argument("--title", required=True, type=cli.filled)
    m.add_argument("--what", required=True, type=cli.filled)
    m.add_argument("--lesson", required=True, type=cli.filled)
    m.add_argument("--symbol", type=cli.filled)
    lr = sub.add_parser("learn", help="weekly learning analysis (signal efficacy, outcomes, mistakes)")
    lr.add_argument("--week-ending")
    sub.add_parser("memory-sync", help="restore memory/ from the shared state branch (refuses to overwrite unsaved changes)")
    rc = sub.add_parser("run-claim", help="memory-sync + window/duplicate check + push the run claim to the state branch")
    rc.add_argument("--workflow", required=True, choices=runs.WORKFLOWS)
    rc.add_argument("--force-recover", action="store_true", help="take over a recent unfinished run")
    rs = sub.add_parser("run-start", help="LOCAL ONLY: window/duplicate check + local marker (not pushed)")
    rs.add_argument("--workflow", required=True, choices=runs.WORKFLOWS)
    rs.add_argument("--force-recover", action="store_true", help="take over a recent unfinished run")
    rf = sub.add_parser("run-finish")
    rf.add_argument("--workflow", required=True, choices=runs.WORKFLOWS)
    rf.add_argument("--status", required=True, choices=["completed", "failed"])
    rf.add_argument("--summary", type=cli.filled)
    p = sub.add_parser("persist", help="save memory/ to the state branch (rebase on rejection, never force, never main)")
    p.add_argument("--message", required=True, type=cli.filled)
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("state-audit", help="read-only: state branch changes only memory/ and was never rewritten")
    sr = sub.add_parser("state-recover", help="OWNER ONLY (local terminal): merge a recovery copy into the state branch")
    sr.add_argument("--branch", required=True, type=cli.filled)
    sub.add_parser("activation-check", help="preconditions for the owner's execution decision (read-only)")


HANDLERS = {
    "regime": cmd_regime, "signals": cmd_signals, "backtest": cmd_backtest, "size": cmd_size,
    "decide": cmd_decide, "evidence": cmd_evidence, "research-check": cmd_research_check,
    "mistake": cmd_mistake, "learn": cmd_learn,
    "memory-sync": cmd_memory_sync, "run-claim": cmd_run_claim,
    "run-start": cmd_run_start, "run-finish": cmd_run_finish, "persist": cmd_persist,
    "state-audit": cmd_state_audit, "state-recover": cmd_state_recover,
    "activation-check": cmd_activation_check,
}
