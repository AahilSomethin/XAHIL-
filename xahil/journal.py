"""Order journal and persistent halts (Git-backed, under memory/state/).

Order journal (`executed_orders.jsonl`): an append-only log. Every live
submission writes "submitting" BEFORE the request and a terminal status
after, so an interrupted run leaves evidence the next reconcile can resolve
against Alpaca using the deterministic client_order_id.

Persistent halts (`halts.json`): conditions that block new entries until a
human acknowledges them in a local terminal (account change or reset,
unexplained position or equity changes).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

UNRESOLVED = {"submitting", "uncertain"}
TERMINAL_OK = {"submitted", "recovered_submitted", "recovered_from_broker"}
TERMINAL_NOT_PLACED = {"not_placed", "rejected_by_broker"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"logged_at": _now(), **record}, default=str) + "\n")


def read(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"status": "corrupt_journal_line", "raw": line[:200]})
    return out


def latest_by_coid(records: list) -> dict:
    latest = {}
    for r in records:
        coid = r.get("client_order_id") or (r.get("order") or {}).get("client_order_id")
        if coid:
            latest[coid] = r
    return latest


def unresolved(path: Path) -> list:
    """client_order_ids whose last journal status is submitting/uncertain."""
    return sorted(c for c, r in latest_by_coid(read(path)).items() if r.get("status") in UNRESOLVED)


def known_coids(path: Path) -> set:
    return set(latest_by_coid(read(path)))


def corrupt_lines(path: Path) -> int:
    return sum(1 for r in read(path) if r.get("status") == "corrupt_journal_line")


# --- persistent halts ---------------------------------------------------------

def load_halts(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [{"id": "corrupt-halts-file", "reason": "halts.json is unreadable", "at": _now()}]
    return data if isinstance(data, list) else []


def add_halt(path: Path, halt_id: str, reason: str) -> bool:
    halts = load_halts(path)
    if any(h.get("id") == halt_id for h in halts):
        return False
    halts.append({"id": halt_id, "reason": reason, "at": _now()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(halts, indent=2) + "\n", encoding="utf-8")
    return True


def clear_halts(path: Path, ids: list = None) -> list:
    halts = load_halts(path)
    keep = [h for h in halts if ids is not None and h.get("id") not in ids]
    removed = [h for h in halts if h not in keep]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keep, indent=2) + "\n", encoding="utf-8")
    return removed
