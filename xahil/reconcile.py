"""Reconcile Alpaca's actual paper-account state with the last committed snapshot.

The broker is the source of truth. Memory files are notes; when they disagree
with Alpaca, Alpaca wins and the difference is logged. Changes the broker's
own fills cannot explain (account resets, deposits, manual dashboard trades)
raise a persistent halt that a human must acknowledge.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from xahil.config import RiskLimits
from xahil.risk import _f, account_halt_reasons, long_market_value, overnight_protection_gaps, protection_gaps

QTY_EPS = 1e-6


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
        out.append("ACCOUNT CHANGED since last snapshot")
    pp, cp = prev.get("positions", {}), cur.get("positions", {})
    for sym in sorted(set(pp) | set(cp)):
        a, b = pp.get(sym), cp.get(sym)
        if a and not b:
            out.append(f"{sym}: position closed since last run (was {a['qty']:g} sh) - stop fill or exit?")
        elif b and not a:
            out.append(f"{sym}: new position {b['qty']:g} sh @ {b['avg_entry_price']}")
        elif abs(a["qty"] - b["qty"]) > QTY_EPS:
            out.append(f"{sym}: qty changed {a['qty']:g} -> {b['qty']:g}")
    po, co = prev.get("open_orders", {}), cur.get("open_orders", {})
    for oid in sorted(set(po) - set(co)):
        o = po[oid]
        out.append(f"order {oid[:8]} ({o['side']} {o['type']} {o['symbol']}) no longer open - filled, canceled or expired")
    for oid in sorted(set(co) - set(po)):
        o = co[oid]
        out.append(f"order {oid[:8]} ({o['side']} {o['type']} {o['symbol']}) is new since last run")
    return out


def _dedupe_fills(fills: list) -> list:
    seen, out = set(), []
    for f in fills:
        fid = f.get("id")
        if fid and fid in seen:
            continue
        seen.add(fid)
        out.append(f)
    return out


def unexplained_changes(prev: dict, cur: dict, fills_since_prev: list) -> list:
    """Position or equity changes that the broker's own fills since the last snapshot do not explain."""
    if not prev or prev.get("account_number") != cur.get("account_number"):
        return []
    fills = _dedupe_fills(fills_since_prev)
    net = defaultdict(float)
    notional = 0.0
    for f in fills:
        q = _f(f.get("qty"))
        notional += q * _f(f.get("price"))
        net[f.get("symbol")] += q if f.get("side") == "buy" else -q
    out = []
    pp, cp = prev.get("positions", {}), cur.get("positions", {})
    for sym in sorted(set(pp) | set(cp) | set(net)):
        before = (pp.get(sym) or {}).get("qty", 0.0)
        after = (cp.get(sym) or {}).get("qty", 0.0)
        if abs((after - before) - net[sym]) > QTY_EPS + 1e-6 * max(abs(before), abs(after)):
            out.append(f"{sym}: qty {before:g} -> {after:g} but fills since last run explain {net[sym]:+g}")
    d_eq = cur.get("equity", 0.0) - prev.get("equity", 0.0)
    allowance = prev.get("long_value", 0.0) + notional + max(0.01 * prev.get("equity", 0.0), 5.0)
    if abs(d_eq) > allowance:
        out.append(f"equity changed {d_eq:+,.2f} (from {prev.get('equity', 0):,.2f}), more than open positions "
                   f"and fills can explain - account reset, deposit or withdrawal?")
    return out


def foreign_or_unlogged_orders(broker_orders: list, known_coids: set) -> tuple:
    """(xahil orders missing from the journal, orders not placed by xahil)."""
    unlogged, foreign = [], []
    for o in broker_orders:
        coid = o.get("client_order_id") or ""
        if coid.startswith("xahil-"):
            if coid not in known_coids:
                unlogged.append(o)
        else:
            foreign.append(o)
    return unlogged, foreign


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
              open_orders: list, limits: RiskLimits, market_open: bool, fills_since_prev: list = None) -> dict:
    snap = build_snapshot(as_of, workflow, account, positions, open_orders)
    halts = account_halt_reasons(account, positions, limits)
    account_changed = bool(prev.get("account_number")) and prev.get("account_number") != snap["account_number"]
    return {
        "snapshot": snap,
        "differences": diff_snapshots(prev, snap),
        "halt_reasons": halts,
        "account_changed": account_changed,
        "unexplained": unexplained_changes(prev, snap, fills_since_prev or []),
        "limit_breaches": limit_breaches(account, positions, limits),
        "unprotected_positions": protection_gaps(positions, open_orders) if market_open else [],
        "overnight_unprotected": overnight_protection_gaps(positions, open_orders),
        "ok": not halts,
    }


def load_snapshot(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"corrupt": True}


def save_snapshot(path: Path, snap: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic: an interrupted run never leaves a half-written snapshot


def format_log_entry(result: dict) -> str:
    s = result["snapshot"]
    lines = [f"\n## {s['as_of'][:16]} - {s['workflow']}",
             f"- Equity ${s['equity']:,.2f} | Cash ${s['cash']:,.2f} | Long ${s['long_value']:,.2f} "
             f"| Positions {len(s['positions'])} | Open orders {len(s['open_orders'])}"]
    for label, key in (("HALT", "halt_reasons"), ("Unexplained", "unexplained"),
                       ("Limit breach", "limit_breaches"), ("Change", "differences")):
        for item in result.get(key, []):
            lines.append(f"- {label}: {item}")
    for item in result.get("journal", []):
        lines.append(f"- Journal: {item}")
    if result["unprotected_positions"]:
        lines.append(f"- No protective stop: {', '.join(result['unprotected_positions'])}")
    if result.get("overnight_unprotected"):
        lines.append(f"- No overnight (GTC) protection: {', '.join(result['overnight_unprotected'])} - entries blocked")
    return "\n".join(lines) + "\n"
