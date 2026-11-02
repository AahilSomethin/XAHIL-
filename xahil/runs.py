"""Run markers: duplicate-run prevention, interruption detection and market-calendar windows.

One marker file per (trading date, workflow): memory/state/runs/<date>/<workflow>.json.
In the cloud, `run-claim` writes the marker and pushes it to the shared state branch, and the run
proceeds only after that push is verified. Two concurrent runs of the same workflow collide on the
same file; the loser's rebase conflicts on it and it stops (see xahil.persist). `run-start` alone
only writes the local marker (local, unpushed runs).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Optional

from xahil.config import STATE_DIR

RUNS_DIR = STATE_DIR / "runs"
WORKFLOWS = ("pre-market", "market-open", "midday", "daily-summary", "weekly-review")
STALE_MINUTES = 90

PROCEED, SKIP_DONE, SKIP_RUNNING, SKIP_CLOSED, SKIP_WINDOW = (
    "proceed", "skip_already_completed", "skip_in_progress", "skip_market_closed", "skip_outside_window")


def _hm(s: str) -> int:
    h, m = s.split(":")[:2]
    return int(h) * 60 + int(m)


def window_for(workflow: str, session: Optional[dict]) -> Optional[tuple]:
    """Allowed ET window (minutes after midnight) for a workflow on a trading day, honouring early closes."""
    if workflow == "weekly-review" or session is None:
        return None
    open_m, close_m = _hm(session["open"]), _hm(session["close"])
    return {
        "pre-market": (6 * 60, open_m - 5),
        "market-open": (open_m + 10, open_m + 90),
        "midday": (open_m + 90, close_m - 30),
        "daily-summary": (close_m + 10, 20 * 60),
    }[workflow]


def run_key(workflow: str, day: dt.date) -> str:
    """Trading date, or ISO week for the weekly review (runs once per week whatever day it fires)."""
    if workflow == "weekly-review":
        y, w, _ = day.isocalendar()
        return f"{y}-W{w:02d}"
    return day.isoformat()


def _write_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def marker_path(date: str, workflow: str, runs_dir: Path = RUNS_DIR) -> Path:
    return runs_dir / date / f"{workflow}.json"


def read_marker(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "corrupt"}


def interrupted_today(date: str, runs_dir: Path = RUNS_DIR, exclude: str = None) -> list:
    out = []
    day = runs_dir / date
    if day.exists():
        for p in sorted(day.glob("*.json")):
            m = read_marker(p) or {}
            if p.stem != exclude and m.get("status") == "started":
                out.append(p.stem)
    return out


def start(workflow: str, now_et: dt.datetime, session: Optional[dict], runs_dir: Path = RUNS_DIR,
          force_recover: bool = False) -> dict:
    """Decide whether this run may proceed, and write the 'started' marker if so."""
    if workflow not in WORKFLOWS:
        raise ValueError(f"unknown workflow {workflow!r}")
    date = run_key(workflow, now_et.date())
    if workflow == "weekly-review":
        if session is not None and now_et.weekday() < 5:
            return {"decision": SKIP_WINDOW, "reason": "weekly review runs when the market is closed (weekend)"}
    else:
        if session is None:
            return {"decision": SKIP_CLOSED, "reason": f"{date} is not a trading day"}
        lo, hi = window_for(workflow, session)
        minute = now_et.hour * 60 + now_et.minute
        if not (lo <= minute <= hi):
            return {"decision": SKIP_WINDOW,
                    "reason": f"{workflow} allowed {lo // 60:02d}:{lo % 60:02d}-{hi // 60:02d}:{hi % 60:02d} ET "
                              f"(session {session['open']}-{session['close']}); now {now_et:%H:%M}"}
    path = marker_path(date, workflow, runs_dir)
    existing = read_marker(path)
    note = None
    if existing:
        if existing.get("status") == "completed":
            return {"decision": SKIP_DONE, "reason": f"{workflow} already completed for {date}", "marker": existing}
        if existing.get("status") == "started":
            started = dt.datetime.fromisoformat(existing["started_at"])
            age = (now_et - started).total_seconds() / 60
            if age < STALE_MINUTES and not force_recover:
                return {"decision": SKIP_RUNNING, "reason": f"another {workflow} run started {age:.0f} min ago",
                        "marker": existing}
            note = f"previous run {existing.get('run_id')} was interrupted ({age:.0f} min ago); recovering"
        elif existing.get("status") == "failed":
            note = f"previous run {existing.get('run_id')} failed; retrying"
    marker = {"workflow": workflow, "date": date, "run_id": uuid.uuid4().hex[:12], "status": "started",
              "started_at": now_et.isoformat(timespec="seconds"), "attempt": (existing or {}).get("attempt", 0) + 1,
              "recovery_note": note}
    _write_atomic(path, marker)
    return {"decision": PROCEED, "marker": marker, "path": str(path),
            "interrupted_today": interrupted_today(now_et.date().isoformat(), runs_dir, exclude=workflow),
            "recovery_note": note}


def finish(workflow: str, now_et: dt.datetime, status: str, summary: str = "", runs_dir: Path = RUNS_DIR) -> dict:
    if status not in ("completed", "failed"):
        raise ValueError("status must be completed or failed")
    # A weekly review started late on Sunday may finish after midnight (a new ISO week): also try
    # yesterday's key. Daily workflows end by 20:00 ET, so they only ever use today's key.
    days = [now_et.date()]
    if workflow == "weekly-review":
        days.append(now_et.date() - dt.timedelta(days=1))
    keys = []
    for day in days:
        k = run_key(workflow, day)
        if k not in keys:
            keys.append(k)
    for date in keys:
        path = marker_path(date, workflow, runs_dir)
        marker = read_marker(path)
        if marker and marker.get("status") == "started":
            marker.update(status=status, finished_at=now_et.isoformat(timespec="seconds"), summary=summary[:500])
            _write_atomic(path, marker)
            return {"ok": True, "marker": marker}
    return {"ok": False, "reason": f"no started marker for {workflow} {keys[0]}"}


def completed_count(workflow: str, runs_dir: Path = RUNS_DIR) -> int:
    return sum(1 for p in runs_dir.glob(f"*/{workflow}.json") if (read_marker(p) or {}).get("status") == "completed")
