"""Performance metrics computed from broker data and the committed equity history."""

from __future__ import annotations

import csv
import datetime as dt
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HISTORY_FIELDS = ["date", "equity", "cash", "long_value", "spy_close"]


def _f(v, default=0.0):
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# --- equity history ----------------------------------------------------------

def load_history(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        rows = []
        for r in csv.DictReader(fh):
            rows.append({
                "date": r["date"],
                "equity": _f(r.get("equity")),
                "cash": _f(r.get("cash")),
                "long_value": _f(r.get("long_value")),
                "spy_close": _f(r.get("spy_close"), None) if r.get("spy_close") else None,
            })
    return sorted(rows, key=lambda r: r["date"])


def upsert_row(rows: list, row: dict) -> list:
    out = [r for r in rows if r["date"] != row["date"]]
    out.append(row)
    return sorted(out, key=lambda r: r["date"])


def save_history(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({
                "date": r["date"],
                "equity": f"{r['equity']:.2f}",
                "cash": f"{r['cash']:.2f}",
                "long_value": f"{r['long_value']:.2f}",
                "spy_close": "" if r.get("spy_close") is None else f"{r['spy_close']:.4f}",
            })


def pct(a: float, b: float) -> Optional[float]:
    """Percent change from b to a."""
    if not b:
        return None
    return (a - b) / b * 100


def max_drawdown(equities: list) -> dict:
    peak = None
    worst = 0.0
    peak_at = trough_at = None
    current_dd = 0.0
    for i, e in enumerate(equities):
        if peak is None or e > peak:
            peak, cur_peak_i = e, i
        dd = (e - peak) / peak * 100 if peak else 0.0
        current_dd = dd
        if dd < worst:
            worst, peak_at, trough_at = dd, cur_peak_i, i
    return {"max_drawdown_pct": round(worst, 4), "current_drawdown_pct": round(current_dd, 4),
            "peak_index": peak_at, "trough_index": trough_at}


def week_start(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def period_stats(rows: list, start: dt.date, end: dt.date) -> dict:
    """Return over [start, end] using the last row before `start` as the opening value."""
    before = [r for r in rows if r["date"] < start.isoformat()]
    inside = [r for r in rows if start.isoformat() <= r["date"] <= end.isoformat()]
    if not inside:
        return {"available": False}
    open_row = before[-1] if before else inside[0]
    close_row = inside[-1]
    out = {
        "available": True,
        "start_date": open_row["date"],
        "end_date": close_row["date"],
        "start_equity": open_row["equity"],
        "end_equity": close_row["equity"],
        "pnl": round(close_row["equity"] - open_row["equity"], 2),
        "return_pct": pct(close_row["equity"], open_row["equity"]),
        "spy_return_pct": None,
    }
    if open_row.get("spy_close") and close_row.get("spy_close"):
        out["spy_return_pct"] = pct(close_row["spy_close"], open_row["spy_close"])
    if out["return_pct"] is not None and out["spy_return_pct"] is not None:
        out["vs_spy_pct"] = out["return_pct"] - out["spy_return_pct"]
    window = ([open_row] if before else []) + inside
    out["max_drawdown_pct"] = max_drawdown([r["equity"] for r in window])["max_drawdown_pct"]
    return out


def inception_stats(rows: list) -> dict:
    if not rows:
        return {"available": False}
    first, last = rows[0], rows[-1]
    out = {
        "available": True,
        "start_date": first["date"],
        "start_equity": first["equity"],
        "end_equity": last["equity"],
        "pnl": round(last["equity"] - first["equity"], 2),
        "return_pct": pct(last["equity"], first["equity"]),
        "spy_return_pct": None,
        **max_drawdown([r["equity"] for r in rows]),
    }
    spy_rows = [r for r in rows if r.get("spy_close")]
    if len(spy_rows) >= 1 and spy_rows[0]["date"] == first["date"]:
        out["spy_return_pct"] = pct(spy_rows[-1]["spy_close"], spy_rows[0]["spy_close"])
    if out["return_pct"] is not None and out["spy_return_pct"] is not None:
        out["vs_spy_pct"] = out["return_pct"] - out["spy_return_pct"]
    return out


# --- closed trades from fills ------------------------------------------------

@dataclass
class ClosedTrade:
    symbol: str
    qty: float
    entry_price: float
    exit_price: float
    entry_time: str
    exit_time: str

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        return (self.exit_price - self.entry_price) / self.entry_price * 100 if self.entry_price else 0.0


def _group_fills(fills: list) -> list:
    """Merge partial fills of the same order into one VWAP execution."""
    groups: "OrderedDict[str, dict]" = OrderedDict()
    for f in sorted(fills, key=lambda x: x.get("transaction_time", "")):
        key = f.get("order_id") or f.get("id")
        qty, price = _f(f.get("qty")), _f(f.get("price"))
        g = groups.setdefault(key, {"symbol": f.get("symbol"), "side": f.get("side"),
                                    "qty": 0.0, "value": 0.0, "time": f.get("transaction_time", "")})
        g["qty"] += qty
        g["value"] += qty * price
        g["time"] = f.get("transaction_time", g["time"])
    out = []
    for g in groups.values():
        if g["qty"] > 0:
            out.append({**g, "price": g["value"] / g["qty"]})
    return out


def closed_trades_from_fills(fills: list) -> list:
    lots: dict = {}
    trades = []
    for ex in _group_fills(fills):
        sym = ex["symbol"]
        if ex["side"] == "buy":
            lots.setdefault(sym, deque()).append([ex["qty"], ex["price"], ex["time"]])
            continue
        remaining = ex["qty"]
        q = lots.get(sym, deque())
        matched_qty, cost, entry_time = 0.0, 0.0, None
        while remaining > 1e-12 and q:
            lot = q[0]
            take = min(lot[0], remaining)
            matched_qty += take
            cost += take * lot[1]
            entry_time = entry_time or lot[2]
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-12:
                q.popleft()
        if matched_qty > 0:
            trades.append(ClosedTrade(sym, matched_qty, cost / matched_qty, ex["price"], entry_time, ex["time"]))
    return trades


def trade_stats(trades: list) -> dict:
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else None,
        "realized_pnl": round(gross_win - gross_loss, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "best": max(trades, key=lambda t: t.return_pct) if trades else None,
        "worst": min(trades, key=lambda t: t.return_pct) if trades else None,
    }


def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else "+"
    return f"{sign}${abs(x):,.2f}"


def fmt_pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:+.2f}%"
