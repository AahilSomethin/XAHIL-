"""Explainable trend-pullback signals.

Every signal lists each rule with its measured value, threshold and a plain
explanation, so a human (or Claude) can see exactly why a symbol did or did
not qualify. Signals are ADVISORY: they never place orders and never bypass
the risk engine. Entries still need a dated catalyst in the research log.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Optional

from xahil.ta import Indicators


@dataclass
class Check:
    name: str
    passed: bool
    value: Optional[float]
    threshold: str
    explanation: str


@dataclass
class Signal:
    symbol: str
    as_of: str
    qualified: bool
    score: float
    checks: list = field(default_factory=list)
    entry_ref: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    stop_pct: Optional[float] = None
    whole_shares_affordable: Optional[int] = None
    regime: Optional[str] = None

    def failed(self) -> list:
        return [c.name for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["failed"] = self.failed()
        return d

    def summary(self) -> str:
        head = f"{self.symbol}: {'QUALIFIED' if self.qualified else 'not qualified'} ({self.score:.0%})"
        if self.qualified:
            return f"{head} ref ${self.entry_ref:.2f}, stop ${self.stop:.2f} ({self.stop_pct:.1f}%), target ${self.target:.2f}"
        return head + " - failed: " + ", ".join(self.failed())


def suggested_levels(entry: float, atr_value: float, params: dict) -> tuple:
    """Stop = entry - k*ATR, clamped to the strategy's 7-10% band. Target = reward_risk * risk."""
    raw = entry - params["stop_atr_mult"] * atr_value
    lo = entry * (1 - params["max_stop_pct"] / 100)   # widest allowed
    hi = entry * (1 - params["min_stop_pct"] / 100)   # tightest allowed
    stop = min(max(raw, lo), hi)
    target = entry + params["reward_risk"] * (entry - stop)
    return round(stop, 2), round(target, 2), round((1 - stop / entry) * 100, 2)


def evaluate_at(symbol: str, ind: Indicators, i: int, bench: Optional[Indicators], params: dict,
                position_budget: Optional[float] = None, price_buffer_pct: float = 2.0,
                regime: Optional[str] = None) -> Signal:
    as_of = ind.dates[i]
    checks = []

    def add(name, passed, value, threshold, explanation):
        checks.append(Check(name, bool(passed), None if value is None else round(value, 4), threshold, explanation))

    enough = i + 1 >= params["min_history_days"] and ind.sma_slow[i] is not None
    add("history", enough, i + 1, f">= {params['min_history_days']} bars", "enough history for SMA200")
    c = ind.close[i]
    f, s, r, a = ind.sma_fast[i], ind.sma_slow[i], ind.rsi[i], ind.atr[i]

    up = enough and f is not None and s is not None and c > f > s
    add("uptrend", up, None if not (f and s) else (c / s - 1) * 100, "close > SMA50 > SMA200",
        "price above a rising trend structure")

    rs = None
    if bench is not None:
        bi = bench.at(as_of)
        if bi is not None and ind.roc_rs[i] is not None and bench.roc_rs[bi] is not None:
            rs = ind.roc_rs[i] - bench.roc_rs[bi]
    add("relative_strength", rs is not None and rs > 0, rs, f"{params['rs_lookback']}-day return > SPY",
        "outperforming the S&P 500 over ~3 months")

    add("rsi_pullback", r is not None and params["rsi_min"] <= r <= params["rsi_max"], r,
        f"{params['rsi_min']} <= RSI{params['rsi_period']} <= {params['rsi_max']}", "not overbought, not broken down")

    hi = ind.high_252[i]
    from_high = None if not hi else (1 - c / hi) * 100
    add("near_high", from_high is not None and from_high <= params["max_from_high_pct"], from_high,
        f"<= {params['max_from_high_pct']}% below 52-week high", "leader, not a falling knife")

    atr_pct = None if a is None or not c else a / c * 100
    add("volatility", atr_pct is not None and atr_pct <= params["max_atr_pct"], atr_pct,
        f"ATR{params['atr_period']} <= {params['max_atr_pct']}% of price", "a 7-10% stop is not inside daily noise")

    shares = None
    if position_budget is not None and c > 0:
        shares = math.floor(position_budget / (c * (1 + price_buffer_pct / 100)))
        add("whole_share_affordable", shares >= 1, shares, "at least 1 whole share within the 5% position cap",
            "whole shares are required so the position can carry a GTC stop overnight")

    passed = sum(1 for ch in checks if ch.passed)
    sig = Signal(symbol, as_of, all(ch.passed for ch in checks), passed / len(checks), checks,
                 whole_shares_affordable=shares, regime=regime)
    if a is not None and c > 0:
        sig.entry_ref = round(c, 2)
        sig.stop, sig.target, sig.stop_pct = suggested_levels(c, a, params)
    return sig


def evaluate(symbol: str, bars: list, bench_bars: Optional[list], params: dict, **kw) -> Signal:
    if not bars:
        return Signal(symbol, "", False, 0.0, [Check("history", False, 0, "bars", "no data")])
    ind = Indicators(bars, params)
    bench = Indicators(bench_bars, params) if bench_bars else None
    return evaluate_at(symbol, ind, len(bars) - 1, bench, params, **kw)
