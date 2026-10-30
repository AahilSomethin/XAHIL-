"""Market-regime classification from the benchmark (SPY) daily bars.

Advisory only: the regime informs research and the weekly review. Using it to
block entries would be a new strategy rule and needs owner approval.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from xahil.ta import Indicators, slope_pct

RISK_ON, NEUTRAL, RISK_OFF, HIGH_VOL, UNKNOWN = "risk_on", "neutral", "risk_off", "high_volatility", "unknown"


@dataclass
class Regime:
    as_of: str
    regime: str
    reasons: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def classify_at(ind: Indicators, i: int, params: dict) -> Regime:
    rp = params["regime"]
    as_of = ind.dates[i] if 0 <= i < len(ind.dates) else ""
    c, f, s, v = ind.close[i], ind.sma_fast[i], ind.sma_slow[i], ind.vol20[i]
    if f is None or s is None or v is None:
        return Regime(as_of, UNKNOWN, ["not enough history for SMA200 / volatility"])
    slope = slope_pct(ind.sma_fast, i, rp["slope_lookback"])
    m = {"close": round(c, 2), "sma50": round(f, 2), "sma200": round(s, 2),
         "close_vs_sma200_pct": round((c / s - 1) * 100, 2),
         "sma50_slope_pct": None if slope is None else round(slope, 2),
         "realized_vol_20d_pct": round(v, 1)}
    if v >= rp["high_vol_pct"]:
        return Regime(as_of, HIGH_VOL, [f"20-day realized volatility {v:.1f}% >= {rp['high_vol_pct']}%"], m)
    if c > s and f > s and c > f:
        return Regime(as_of, RISK_ON, ["close > SMA50 > SMA200 (uptrend)",
                                       f"realized volatility {v:.1f}% is normal"], m)
    if c < s and f < s:
        return Regime(as_of, RISK_OFF, ["close and SMA50 below SMA200 (downtrend)"], m)
    reasons = []
    reasons.append("close above SMA200" if c > s else "close below SMA200")
    reasons.append("SMA50 above SMA200" if f > s else "SMA50 below SMA200")
    return Regime(as_of, NEUTRAL, reasons + ["mixed trend signals"], m)


def classify(bars: list, params: dict) -> Regime:
    if not bars:
        return Regime("", UNKNOWN, ["no benchmark bars"])
    ind = Indicators(bars, params)
    return classify_at(ind, len(bars) - 1, params)
