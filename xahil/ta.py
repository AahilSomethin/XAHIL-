"""Technical indicators on daily bars. Pure Python, no third-party packages.

Bars are dicts with Alpaca's keys: t (timestamp), o, h, l, c, v.
Every series function returns a list aligned with its input, with None
where there is not yet enough history. Value at index i uses only data at
indices <= i (no lookahead).
"""

from __future__ import annotations

import math
from typing import Optional


def closes(bars: list) -> list:
    return [float(b["c"]) for b in bars]


def sma(values: list, n: int) -> list:
    out, total = [None] * len(values), 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: list, n: int) -> list:
    out = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list, n: int = 14) -> list:
    """Wilder's RSI."""
    out = [None] * len(values)
    if len(values) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = values[i] - values[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / n, losses / n

    def value(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out[n] = value(avg_g, avg_l)
    for i in range(n + 1, len(values)):
        d = values[i] - values[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = value(avg_g, avg_l)
    return out


def true_range(bars: list) -> list:
    out = []
    for i, b in enumerate(bars):
        h, l = float(b["h"]), float(b["l"])
        if i == 0:
            out.append(h - l)
        else:
            pc = float(bars[i - 1]["c"])
            out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def atr(bars: list, n: int = 14) -> list:
    """Wilder's ATR."""
    tr = true_range(bars)
    out = [None] * len(bars)
    if len(bars) < n:
        return out
    prev = sum(tr[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(bars)):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def roc(values: list, n: int) -> list:
    """Percent change over n periods."""
    return [None if i < n or not values[i - n] else (values[i] / values[i - n] - 1) * 100
            for i in range(len(values))]


def rolling_max(values: list, n: int) -> list:
    out = [None] * len(values)
    for i in range(len(values)):
        if i >= n - 1:
            out[i] = max(values[i - n + 1:i + 1])
    return out


def realized_vol(values: list, n: int = 20) -> list:
    """Annualized standard deviation of daily log returns, in percent."""
    rets = [None] + [math.log(values[i] / values[i - 1]) if values[i - 1] > 0 and values[i] > 0 else 0.0
                     for i in range(1, len(values))]
    out = [None] * len(values)
    for i in range(n, len(values)):
        window = rets[i - n + 1:i + 1]
        mean = sum(window) / n
        var = sum((r - mean) ** 2 for r in window) / (n - 1)
        out[i] = math.sqrt(var) * math.sqrt(252) * 100
    return out


def slope_pct(series: list, i: int, lookback: int) -> Optional[float]:
    if i < lookback or series[i] is None or series[i - lookback] in (None, 0):
        return None
    return (series[i] / series[i - lookback] - 1) * 100


class Indicators:
    """Precomputed indicator series for one symbol (computed once, read at any index)."""

    def __init__(self, bars: list, params: dict):
        self.bars = bars
        self.dates = [str(b["t"])[:10] for b in bars]
        self.index = {d: i for i, d in enumerate(self.dates)}
        c = closes(bars)
        self.close = c
        self.sma_fast = sma(c, params["sma_fast"])
        self.sma_slow = sma(c, params["sma_slow"])
        self.rsi = rsi(c, params["rsi_period"])
        self.atr = atr(bars, params["atr_period"])
        self.roc_rs = roc(c, params["rs_lookback"])
        self.high_252 = rolling_max([float(b["h"]) for b in bars], 252)
        self.vol20 = realized_vol(c, 20)

    def at(self, date: str) -> Optional[int]:
        return self.index.get(date)

    def last_index_on_or_before(self, date: str) -> Optional[int]:
        idx = None
        for i, d in enumerate(self.dates):
            if d <= date:
                idx = i
            else:
                break
        return idx
