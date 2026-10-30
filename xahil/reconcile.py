"""Reconcile Alpaca's actual paper-account state with the last committed snapshot.

The broker is the source of truth. Memory files are notes; when they disagree
with Alpaca, Alpaca wins and the difference is logged.
"""

from __future__ import annotations

import json
from pathlib import Path

from xahil.config import RiskLimits
from xahil.risk import _f, account_halt_reasons, long_market_value


def build_snapshot(as_of: str, workflow: str, account: dict, positions: list, open_orders: list) -> dict:
    return {
        "as_of": as_of,
        "workflow": workflow,
        "account_number": account.get("account_number"),
        "equity": round(_f(account.get("equity")), 2),
        "cash": round(_f(account.get("cash")), 2),
        "long_value": round(long_market_value(positions), 2),
        "positions": {
            p["symbol"]: {
                "qty": _f(p.get("qty")),
                "avg_entry_price": round(_f(p.get("avg_entry_price")), 4),
                "market_value": round(_f(p.get("market_value")), 2),
                "unrealized_plpc": round(_f(p.get("unrealized_plpc")) * 100, 2),
            }
            for p in sorted(positions, key=lambda x: x["symbol"])
        },
        "open_orders": {
            o["id"]: {
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "qty": o.get("qty"),
                "notional": o.get("notional"),
                "stop_price": o.get("stop_price"),
                "trail_percent": o.get("trail_percent"),
                "time_in_force": o.get("time_in_force"),
                "client_order_id": o.get("client_order_id"),
            }
            for o in sorted(open_orders, key=lambda x: x["id"])
        },
    }


def diff_snapshots(prev: dict, cur: dict) -> list:
    if not prev:
        return ["no previous snapshot (first run)"]
    out = []
    if prev.get("account_number") and prev.get("account_number") != cur.get("account_number"):
        out.append(f"ACCOUNT CHANGED: {prev.get('account_number')} -> {cur.get('account_number')}")
    pp, cp = prev.get("positions", {}), cur.get("positions", {})
    for sym in sorted(set(pp) | set(cp)):
        a, b = pp.get(sym), cp.get(sym)
        if a and not b:
            out.append(f"{sym}: position closed since last run (was {a['qty']:g} sh) - stop fill or manual exit?")
        elif b and not a:
            out.append(f"{sym}: new position {b['qty']:g} sh @ {b['avg_entry_price']}")
        elif abs(a["qty"] - b["qty"]) > 1e-9:
            out.append(f"{sym}: qty changed {a['qty']:g} -> {b['qty']:g}")
    po, co = prev.get("open_orders", {}), cur.get("open_orders", {})
    for oid in sorted(set(po) - set(co)):
        o = po[oid]
        out.append(f"order {oid[:8]} ({o['side']} {o['type']} {o['symbol']}) no longer open - filled, canceled or expired")
    for oid in sorted(set(co) - set(po)):
        o = co[oid]
        out.append(f"order {oid[:8]} ({o['side']} {o['type']} {o['symbol']}) is new since last run")
    return out


def protection_gaps(positions: list, open_orders: list) -> list:
    """Positions with no protective sell stop. Fractional positions need a fresh DAY stop each session."""
    gaps = []
    for p in positions:
        if _f(p.get("qty")) <= 0:
            continue
        has_stop = any(
            o.get("symbol") == p["symbol"] and o.get("side") == "sell" and o.get("type") in ("stop", "trailing_stop")
            for o in open_orders
        )
        if not has_stop:
            gaps.append(p["symbol"])
    return gaps


def limit_breaches(account: dict, positions: list, limits: RiskLimits) -> list:
    """Existing positions above caps (e.g. from price drift). These block adds, not exits."""
    equity = _f(account.get("equity"))
    if equity <= 0:
        return []
    out = []
    for p in positions:
        mv = abs(_f(p.get("market_value")))
        if mv > equity * limits.max_position_pct / 100:
            out.append(f"{p['symbol']} is {mv / equity * 100:.1f}% of equity (cap {limits.max_position_pct}%)")
    total = long_market_value(positions)
    if total > equity * limits.max_total_exposure_pct / 100:
        out.append(f"total exposure {total / equity * 100:.1f}% (cap {limits.max_total_exposure_pct}%)")
    return out


def reconcile(prev: dict, as_of: str, workflow: str, account: dict, positions: list,
              open_orders: list, limits: RiskLimits, market_open: bool) -> dict:
    snap = build_snapshot(as_of, workflow, account, positions, open_orders)
    halts = account_halt_reasons(account, positions, limits)
    return {
        "snapshot": snap,
        "differences": diff_snapshots(prev, snap),
        "halt_reasons": halts,
        "limit_breaches": limit_breaches(account, positions, limits),
        "unprotected_positions": protection_gaps(positions, open_orders) if market_open else [],
        "ok": not halts,
    }


def load_snapshot(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_snapshot(path: Path, snap: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def format_log_entry(result: dict) -> str:
    s = result["snapshot"]
    lines = [f"\n## {s['as_of']} - {s['workflow']}",
             f"- Equity ${s['equity']:,.2f} | Cash ${s['cash']:,.2f} | Long ${s['long_value']:,.2f} "
             f"| Positions {len(s['positions'])} | Open orders {len(s['open_orders'])}"]
    for label, key in (("HALT", "halt_reasons"), ("Limit breach", "limit_breaches"), ("Change", "differences")):
        for item in result[key]:
            lines.append(f"- {label}: {item}")
    if result["unprotected_positions"]:
        lines.append(f"- No protective stop: {', '.join(result['unprotected_positions'])}")
    return "\n".join(lines) + "\n"
