"""Git-backed trading memory and learning analysis.

Append-only JSONL logs under memory/state/ (committed by the routines):
  decisions.jsonl       every decision Claude makes (propose / skip / hold / exit / watch) with its reason
  evidence.jsonl        research claims with source URLs (untrusted data, kept for audit)
  signals.jsonl         every signal evaluation, qualified AND rejected, with the failed checks
  regime_history.jsonl  daily market-regime classification
  mistakes.jsonl        human- or Claude-recorded mistakes and lessons (mirrored in memory/MISTAKES.md)

The weekly analysis measures what actually happened after each signal and
proposal (forward returns, hypothetical stop hits). It RECOMMENDS; it never
changes strategy rules.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from xahil import journal
from xahil.config import MEMORY_DIR, STATE_DIR

DECISIONS = STATE_DIR / "decisions.jsonl"
EVIDENCE = STATE_DIR / "evidence.jsonl"
SIGNALS = STATE_DIR / "signals.jsonl"
REGIMES = STATE_DIR / "regime_history.jsonl"
MISTAKES = STATE_DIR / "mistakes.jsonl"
MISTAKES_MD = MEMORY_DIR / "MISTAKES.md"
ACTIONS = ("propose", "skip", "hold", "exit", "watch")
URL_RE = re.compile(r"^https?://[^\s]+$")


def et_today() -> str:
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).date().isoformat()


def _next_id(path: Path, prefix: str, date: str) -> str:
    n = sum(1 for r in journal.read(path) if str(r.get("id", "")).startswith(f"{prefix}-{date}"))
    return f"{prefix}-{date}-{n + 1}"


def record_decision(symbol: str, action: str, reason: str, workflow: str, evidence_ids: list = None,
                    date: str = None, path: Path = DECISIONS, extra: dict = None,
                    evidence_path: Path = EVIDENCE) -> dict:
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {ACTIONS}")
    if not reason.strip():
        raise ValueError("reason is required")
    date = date or et_today()
    known = {r.get("id") for r in journal.read(evidence_path)}
    missing = [e for e in (evidence_ids or []) if e not in known]
    rec = {"id": _next_id(path, "d", date), "date": date, "symbol": symbol.upper(), "action": action,
           "reason": reason.strip(), "workflow": workflow, "evidence_ids": evidence_ids or [],
           "unknown_evidence_ids": missing, **(extra or {})}
    journal.append(path, rec)
    return rec


def record_evidence(symbol: str, claim: str, source: str, kind: str, workflow: str, date: str = None,
                    path: Path = EVIDENCE, *, published_date: str = None, retrieved_at: str = None,
                    provider: str = "manual", query: str = None, title: str = None, snippet: str = None) -> dict:
    if not URL_RE.match(source or ""):
        raise ValueError("source must be an http(s) URL")
    if not claim.strip():
        raise ValueError("claim is required")
    if published_date:
        try:
            dt.date.fromisoformat(published_date)
        except ValueError:
            raise ValueError("published_date must be YYYY-MM-DD") from None
    date = date or et_today()
    host = re.sub(r"^www\.", "", (re.match(r"^https?://([^/:?#]+)", source).group(1)).lower())
    rec = {"id": _next_id(path, "e", date), "date": date, "symbol": (symbol or "MARKET").upper(),
           "claim": claim.strip()[:1000], "source": source, "domain": host, "kind": kind, "workflow": workflow,
           "provider": provider, "published_date": published_date,
           "retrieved_at": retrieved_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "query": (query or "")[:400] or None, "title": (title or "")[:300] or None,
           "snippet": (snippet or "")[:800] or None, "trust": "untrusted-external"}
    journal.append(path, rec)
    return rec


def evidence_for(symbol: str, date: str, path: Path = EVIDENCE) -> list:
    """Evidence recorded for `symbol` on trading date `date` (market-wide items are excluded)."""
    sym = symbol.upper()
    return [r for r in journal.read(path) if r.get("symbol") == sym and r.get("date") == date and r.get("source")]


def evidence_sufficiency(symbol: str, date: str, req: dict, path: Path = EVIDENCE) -> dict:
    """Is there enough fresh, independent evidence on record today to justify an entry?"""
    from xahil.research import assess_sufficiency  # local import: research imports nothing from here
    rows = evidence_for(symbol, date, path)
    sources = [{"url": r["source"], "domain": r.get("domain"), "published_date": r.get("published_date")} for r in rows]
    out = assess_sufficiency(sources, dt.date.fromisoformat(date), req)
    out["evidence_ids"] = [r["id"] for r in rows]
    return out


def record_mistake(title: str, what: str, lesson: str, symbol: str = "", date: str = None,
                   path: Path = MISTAKES, md_path: Path = MISTAKES_MD) -> dict:
    date = date or et_today()
    rec = {"id": _next_id(path, "m", date), "date": date, "title": title.strip(), "what": what.strip(),
           "lesson": lesson.strip(), "symbol": symbol.upper()}
    journal.append(path, rec)
    if not md_path.exists():
        md_path.write_text("# Mistakes and Lessons (PAPER)\n\nAppend-only. Lessons feed STRATEGY-PROPOSALS.md; "
                           "they never change rules by themselves.\n", encoding="utf-8")
    with md_path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {date} - {rec['title']} ({rec['id']}{', ' + rec['symbol'] if rec['symbol'] else ''})\n"
                 f"- What happened: {rec['what']}\n- Lesson: {rec['lesson']}\n")
    return rec


def record_signals(sigs: list, workflow: str, path: Path = SIGNALS) -> int:
    for s in sigs:
        d = s.to_dict() if hasattr(s, "to_dict") else dict(s)
        journal.append(path, {"workflow": workflow, **d})
    return len(sigs)


def record_regime(reg: dict, workflow: str, path: Path = REGIMES) -> None:
    journal.append(path, {"workflow": workflow, **reg})


# --- outcomes ---------------------------------------------------------------------

def forward_outcome(bars: list, signal_date: str, stop_pct: float, horizons=(5, 10)) -> Optional[dict]:
    """Hypothetical entry at the next day's open after `signal_date`; returns per horizon and stop hit."""
    dates = [str(b["t"])[:10] for b in bars]
    after = [i for i, d in enumerate(dates) if d > signal_date]
    if not after:
        return None
    e = after[0]
    entry = float(bars[e]["o"])
    stop = entry * (1 - stop_pct / 100)
    out = {"entry_date": dates[e], "entry": round(entry, 4), "stop": round(stop, 4), "stop_hit_day": None}
    for k, i in enumerate(range(e, len(bars))):
        if float(bars[i]["l"]) <= stop:
            out["stop_hit_day"] = k
            break
    for h in horizons:
        j = e + h - 1
        out[f"ret_{h}d_pct"] = None if j >= len(bars) else round((float(bars[j]["c"]) / entry - 1) * 100, 2)
    out["complete"] = all(out.get(f"ret_{h}d_pct") is not None for h in horizons)
    return out


def _in_week(date: str, start: str, end: str) -> bool:
    return bool(date) and start <= date[:10] <= end


def weekly_analysis(week_start: str, week_end: str, *, signals: list, decisions: list, evidence: list,
                    proposals: list, mistakes: list, regimes: list, closed_trades: list, bars: dict,
                    default_stop_pct: float = 10.0, lookback_days: int = 35) -> dict:
    """Pure analysis over loaded records and price bars. Returns a dict for the weekly review."""
    a: dict = {"week_start": week_start, "week_end": week_end}

    wk_regimes = [r for r in regimes if _in_week(r.get("as_of", ""), week_start, week_end)]
    a["regimes"] = dict(Counter(r.get("regime") for r in wk_regimes))

    # Signal efficacy: signals old enough to have 10 trading days of outcome, within the lookback window.
    horizon_start = (dt.date.fromisoformat(week_end) - dt.timedelta(days=lookback_days)).isoformat()
    groups = {"qualified": [], "rejected": []}
    seen = set()
    for s in signals:
        key = (s.get("symbol"), s.get("as_of"))
        if key in seen or not s.get("as_of") or not (horizon_start <= s["as_of"] <= week_end):
            continue
        seen.add(key)
        sym_bars = bars.get(s.get("symbol"))
        if not sym_bars:
            continue
        stop_pct = s.get("stop_pct") or default_stop_pct
        o = forward_outcome(sym_bars, s["as_of"], stop_pct)
        if o and o["complete"]:
            groups["qualified" if s.get("qualified") else "rejected"].append(o)

    def summarize(rows):
        if not rows:
            return {"n": 0}
        r10 = [r["ret_10d_pct"] for r in rows]
        return {"n": len(rows), "avg_ret_5d_pct": round(sum(r["ret_5d_pct"] for r in rows) / len(rows), 2),
                "avg_ret_10d_pct": round(sum(r10) / len(rows), 2),
                "hit_rate_10d_pct": round(sum(1 for x in r10 if x > 0) / len(rows) * 100, 1),
                "stop_hit_within_10d_pct": round(sum(1 for r in rows if r["stop_hit_day"] is not None
                                                     and r["stop_hit_day"] < 10) / len(rows) * 100, 1)}

    a["signal_efficacy"] = {k: summarize(v) for k, v in groups.items()}
    q, rj = a["signal_efficacy"]["qualified"], a["signal_efficacy"]["rejected"]
    if q.get("n", 0) >= 10 and rj.get("n", 0) >= 10:
        a["signal_edge_10d_pct"] = round(q["avg_ret_10d_pct"] - rj["avg_ret_10d_pct"], 2)
    else:
        a["signal_edge_10d_pct"] = None
    wk_signals = [s for s in signals if _in_week(s.get("as_of", ""), week_start, week_end)]
    a["signals_this_week"] = {"evaluated": len(wk_signals), "qualified": sum(1 for s in wk_signals if s.get("qualified")),
                              "top_failed_checks": Counter(f for s in wk_signals for f in s.get("failed", [])).most_common(5)}

    # Proposals and risk-engine rejections
    wk_props = [p for p in proposals if _in_week(p.get("date", ""), week_start, week_end)]
    a["proposals"] = dict(Counter(p.get("status") for p in wk_props))
    rejected_checks = Counter()
    per_symbol_check = Counter()
    for p in wk_props:
        if p.get("status") in ("rejected_by_risk_engine", "rejected_on_reverify"):
            for name, ok in (p.get("checks") or {}).items():
                if not ok:
                    rejected_checks[name] += 1
                    per_symbol_check[((p.get("order") or {}).get("symbol"), name)] += 1
    a["risk_rejections_by_check"] = dict(rejected_checks.most_common())
    outcomes = []
    for p in wk_props:
        o = p.get("order") or {}
        if p.get("status") == "dry_run_proposal" and o.get("side") == "buy" and bars.get(o.get("symbol")):
            res = forward_outcome(bars[o["symbol"]], p["date"], default_stop_pct)
            if res:
                outcomes.append({"symbol": o["symbol"], "date": p["date"], **res})
    a["dry_run_outcomes"] = outcomes

    # Decisions and evidence
    wk_dec = [d for d in decisions if _in_week(d.get("date", ""), week_start, week_end)]
    a["decisions"] = dict(Counter(d.get("action") for d in wk_dec))
    a["decisions_without_evidence"] = [d["id"] for d in wk_dec if d.get("action") == "propose" and not d.get("evidence_ids")]
    a["evidence_items"] = sum(1 for e in evidence if _in_week(e.get("date", ""), week_start, week_end))

    # Mistake candidates (deterministic; Claude reviews and records real mistakes)
    cands = []
    for t in closed_trades:
        exit_day = (t.get("exit_time") or "")[:10]
        if not _in_week(exit_day, week_start, week_end):
            continue
        if t["return_pct"] <= -10.5:
            cands.append(f"{t['symbol']}: loss {t['return_pct']:.1f}% exceeded the 10% stop band (gap or slippage)")
        held = (dt.date.fromisoformat(exit_day) - dt.date.fromisoformat(t["entry_time"][:10])).days
        if t["return_pct"] < 0 and held <= 3:
            cands.append(f"{t['symbol']}: stopped/cut within {held} days of entry (entry timing?)")
    for (sym, check), n in per_symbol_check.items():
        if n >= 3:
            cands.append(f"{sym}: {n} orders rejected on '{check}' this week (fighting a limit?)")
    for d_id in a["decisions_without_evidence"]:
        cands.append(f"decision {d_id}: proposal recorded without linked evidence")
    for o in outcomes:
        if o.get("stop_hit_day") is not None and o["stop_hit_day"] < 5:
            cands.append(f"{o['symbol']} dry-run proposal ({o['date']}) would have hit its stop within 5 days")
    a["mistake_candidates"] = cands
    a["mistakes_logged"] = [m for m in mistakes if _in_week(m.get("date", ""), week_start, week_end)]
    return a


def analysis_markdown(a: dict) -> str:
    se = a["signal_efficacy"]

    def eff(name):
        s = se[name]
        if not s.get("n"):
            return f"| {name} | 0 | n/a | n/a | n/a | n/a |"
        return (f"| {name} | {s['n']} | {s['avg_ret_5d_pct']}% | {s['avg_ret_10d_pct']}% | {s['hit_rate_10d_pct']}% | "
                f"{s['stop_hit_within_10d_pct']}% |")

    lines = [f"### Learning analysis (computed by `xahil learn`, week {a['week_start']} to {a['week_end']})", "",
             f"- Regime days: {a['regimes'] or 'none recorded'}",
             f"- Signals this week: {a['signals_this_week']['evaluated']} evaluated, "
             f"{a['signals_this_week']['qualified']} qualified; most common failed checks: "
             f"{a['signals_this_week']['top_failed_checks'] or 'n/a'}",
             f"- Decisions: {a['decisions'] or 'none'}; evidence items: {a['evidence_items']}",
             f"- Proposals by status: {a['proposals'] or 'none'}",
             f"- Risk-engine rejections by check: {a['risk_rejections_by_check'] or 'none'}", "",
             "#### Signal efficacy (signals from the last 5 weeks with a complete 10-day outcome)",
             "| Group | N | Avg 5d | Avg 10d | 10d hit rate | Stop hit within 10d |", "|---|---|---|---|---|---|",
             eff("qualified"), eff("rejected"),
             f"- Qualified minus rejected (10d): "
             f"{'n/a (need at least 10 of each)' if a['signal_edge_10d_pct'] is None else str(a['signal_edge_10d_pct']) + '%'}",
             "", "#### Dry-run proposal outcomes (hypothetical next-open entry, 10% stop)",
             "| Symbol | Proposed | Entry | 5d | 10d | Stop hit (day) |", "|---|---|---|---|---|---|"]
    for o in a["dry_run_outcomes"]:
        lines.append(f"| {o['symbol']} | {o['date']} | ${o['entry']:.2f} | {o.get('ret_5d_pct', 'n/a')} | "
                     f"{o.get('ret_10d_pct', 'n/a')} | {o['stop_hit_day'] if o['stop_hit_day'] is not None else '-'} |")
    if not a["dry_run_outcomes"]:
        lines.append("| (none) | | | | | |")
    lines += ["", "#### Mistake candidates (review, then record real ones with `xahil mistake`)"]
    lines += [f"- {c}" for c in a["mistake_candidates"]] or ["- none detected"]
    lines += ["", "#### Mistakes recorded this week"]
    lines += [f"- {m['id']}: {m['title']} - lesson: {m['lesson']}" for m in a["mistakes_logged"]] or ["- none"]
    lines += ["", "_Recommendations from this analysis go to memory/STRATEGY-PROPOSALS.md as PENDING. "
                  "No rule changes without owner approval._"]
    return "\n".join(lines) + "\n"


def symbols_needing_bars(signals: list, proposals: list, since: str) -> set:
    syms = {s.get("symbol") for s in signals if (s.get("as_of") or "") >= since}
    syms |= {(p.get("order") or {}).get("symbol") for p in proposals if (p.get("date") or "") >= since}
    return {s for s in syms if s}
