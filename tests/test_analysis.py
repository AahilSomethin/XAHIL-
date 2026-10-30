"""Technical analysis, regime, explainable signals and backtesting (synthetic data, offline)."""

import datetime as dt
import json
import math
import unittest
from collections import Counter
from pathlib import Path

from tests.helpers import limits
from xahil import backtest, regime, signals, ta

ROOT = Path(__file__).resolve().parent.parent
SIG = json.loads((ROOT / "config" / "signal_params.json").read_text())
STRAT = json.loads((ROOT / "config" / "strategy_params.json").read_text())


def trading_days(n, start=dt.date(2019, 1, 2)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def make_bars(prices, dates=None, gap_down=None):
    dates = dates or trading_days(len(prices))
    bars, prev = [], prices[0]
    for i, (d, c) in enumerate(zip(dates, prices)):
        o = prev if not (gap_down and d in gap_down) else prices[i]
        hi, lo = max(o, c) * 1.004, min(o, c) * 0.996
        bars.append({"t": f"{d}T04:00:00Z", "o": o, "h": hi, "l": lo, "c": c, "v": 1_000_000})
        prev = c
    return bars


def uptrend(n=600, base=10.0, growth=0.0012, wiggle=0.03, period=9.0):
    return [base * (1 + growth) ** i * (1 + wiggle * math.sin(i / period)) for i in range(n)]


class Indicators(unittest.TestCase):
    def test_sma_ema(self):
        self.assertEqual(ta.sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])
        e = ta.ema([1, 2, 3, 4, 5], 3)
        self.assertIsNone(e[1])
        self.assertAlmostEqual(e[2], 2.0)
        self.assertAlmostEqual(e[4], 4.0)

    def test_rsi_extremes(self):
        self.assertEqual(ta.rsi(list(range(1, 30)), 14)[-1], 100.0)
        self.assertLess(ta.rsi(list(range(30, 1, -1)), 14)[-1], 1.0)
        self.assertIsNone(ta.rsi(list(range(10)), 14)[-1])

    def test_atr_constant_range(self):
        bars = [{"t": "x", "o": 10, "h": 11, "l": 9, "c": 10} for _ in range(30)]
        self.assertAlmostEqual(ta.atr(bars, 14)[-1], 2.0)

    def test_realized_vol_flat_is_zero(self):
        self.assertAlmostEqual(ta.realized_vol([10.0] * 30, 20)[-1], 0.0)

    def test_no_lookahead(self):
        bars = make_bars(uptrend(400))
        full = ta.Indicators(bars, SIG)
        for k in (220, 300, 399):
            part = ta.Indicators(bars[:k + 1], SIG)
            for name in ("sma_fast", "sma_slow", "rsi", "atr", "roc_rs", "high_252", "vol20"):
                with self.subTest(k=k, series=name):
                    self.assertAlmostEqual(getattr(full, name)[k], getattr(part, name)[k], places=9)


class Regimes(unittest.TestCase):
    def test_classes(self):
        self.assertEqual(regime.classify(make_bars(uptrend(400, wiggle=0.002)), SIG).regime, regime.RISK_ON)
        down = [100 * 0.998 ** i for i in range(400)]
        self.assertEqual(regime.classify(make_bars(down), SIG).regime, regime.RISK_OFF)
        noisy = [100 * (1 + 0.04 * (-1) ** i) for i in range(400)]
        self.assertEqual(regime.classify(make_bars(noisy), SIG).regime, regime.HIGH_VOL)
        self.assertEqual(regime.classify(make_bars(uptrend(100)), SIG).regime, regime.UNKNOWN)

    def test_reasons_and_metrics_present(self):
        r = regime.classify(make_bars(uptrend(400, wiggle=0.002)), SIG)
        self.assertTrue(r.reasons)
        self.assertIn("sma200", r.metrics)


class Signals(unittest.TestCase):
    def setUp(self):
        self.spy = make_bars(uptrend(600, base=300, growth=0.0003, wiggle=0.005))

    def test_every_check_is_explained(self):
        s = signals.evaluate("AAA", make_bars(uptrend(600)), self.spy, SIG, position_budget=50)
        names = {c.name for c in s.checks}
        self.assertEqual(names, {"history", "uptrend", "relative_strength", "rsi_pullback", "near_high",
                                 "volatility", "whole_share_affordable"})
        for c in s.checks:
            self.assertTrue(c.threshold and c.explanation)
        self.assertIn("QUALIFIED" if s.qualified else "failed", s.summary())

    def test_some_day_in_uptrend_qualifies_and_levels_are_in_band(self):
        bars = make_bars(uptrend(600))
        ind, bench = ta.Indicators(bars, SIG), ta.Indicators(self.spy, SIG)
        hits = [signals.evaluate_at("AAA", ind, i, bench, SIG, position_budget=50) for i in range(250, 600)]
        q = [s for s in hits if s.qualified]
        self.assertTrue(q, Counter(n for s in hits for n in s.failed()))
        for s in q:
            self.assertTrue(7 - 1e-6 <= s.stop_pct <= 10 + 1e-6)
            self.assertAlmostEqual(s.target - s.entry_ref, 2 * (s.entry_ref - s.stop), delta=0.05)

    def test_downtrend_fails_uptrend_check(self):
        s = signals.evaluate("BBB", make_bars([50 * 0.998 ** i for i in range(600)]), self.spy, SIG)
        self.assertFalse(s.qualified)
        self.assertIn("uptrend", s.failed())

    def test_expensive_stock_fails_whole_share_affordability(self):
        s = signals.evaluate("CCC", make_bars(uptrend(600, base=60)), self.spy, SIG, position_budget=50)
        self.assertIn("whole_share_affordable", s.failed())

    def test_short_history(self):
        s = signals.evaluate("DDD", make_bars(uptrend(100)), self.spy, SIG)
        self.assertIn("history", s.failed())

    def test_levels_clamped(self):
        stop, target, pct = signals.suggested_levels(100, 1, SIG)   # 2*ATR = 2% -> clamped to 7%
        self.assertEqual((stop, pct), (93.0, 7.0))
        stop, _, pct = signals.suggested_levels(100, 10, SIG)       # 20% -> clamped to 10%
        self.assertEqual((stop, pct), (90.0, 10.0))


class Backtests(unittest.TestCase):
    def setUp(self):
        n = 700
        self.dates = trading_days(n)
        self.spy = make_bars(uptrend(n, base=300, growth=0.0003, wiggle=0.005), self.dates)
        self.universe = {f"S{k}": make_bars(uptrend(n, base=8 + k, growth=0.0010 + k * 0.0001,
                                                    wiggle=0.03, period=7 + k), self.dates) for k in range(8)}
        self.lim = limits()

    def bt(self, start=None, end=None, **kw):
        return backtest.run_backtest(self.universe, self.spy, SIG, STRAT, self.lim,
                                     start or self.dates[250], end or self.dates[-1], **kw)

    def test_caps_enforced_every_day(self):
        r = self.bt()
        self.assertTrue(r.trades, "expected some trades on synthetic uptrends")
        for d, equity, exposure in r.equity_curve:
            self.assertLessEqual(exposure, equity * 0.20 * 1.25 + 1)  # caps apply at entry; drift may exceed
        weeks = Counter(dt.date.fromisoformat(t.entry_date).isocalendar()[:2] for t in r.trades)
        self.assertLessEqual(max(weeks.values()), 3)
        for t in r.trades:
            self.assertIsInstance(t.shares, int)
            self.assertGreaterEqual(t.shares, 1)
            self.assertLessEqual(t.shares * t.entry_price, 1000 * 0.05 * 1.6)  # equity grows; 5% of then-equity

    def test_entry_cost_within_position_cap_of_current_equity(self):
        r = self.bt()
        eq = {d: e for d, e, _ in r.equity_curve}
        prev = {self.dates[i + 1]: eq.get(self.dates[i]) for i in range(len(self.dates) - 1)}
        for t in r.trades:
            ref = prev.get(t.entry_date)
            if ref:
                self.assertLessEqual(t.shares * t.entry_price, ref * 0.05 * 1.02 + 0.5)

    def test_never_more_than_four_positions(self):
        r = self.bt()
        for d in self.dates[250:]:
            open_n = sum(1 for t in r.trades if t.entry_date <= d and (t.exit_date is None or t.exit_date > d))
            self.assertLessEqual(open_n, 4)

    def test_gap_down_fills_at_open_not_stop(self):
        prices = uptrend(700, base=10, growth=0.0012, wiggle=0.03, period=9)
        crash_day = self.dates[500]
        prices = prices[:500] + [p * 0.7 for p in prices[500:]]
        bars = make_bars(prices, self.dates, gap_down={crash_day})
        r = backtest.run_backtest({"GAP": bars}, self.spy, SIG, STRAT, self.lim, self.dates[250], self.dates[-1])
        gapped = [t for t in r.trades if t.exit_date == crash_day]
        self.assertTrue(gapped, "a position should be open into the gap on a steady uptrend")
        self.assertIn("gapped", gapped[0].exit_reason)
        slip = SIG["backtest"]["slippage_bps"] / 10000
        self.assertAlmostEqual(gapped[0].exit_price, prices[500] * (1 - slip), places=3)  # the open, not the stop

    def test_no_lookahead_in_backtest(self):
        cut = self.dates[450]
        a = self.bt(end=self.dates[-1])
        altered = {s: b[:451] + [{**x, "o": x["o"] * 0.5, "h": x["h"] * 0.5, "l": x["l"] * 0.5, "c": x["c"] * 0.5}
                                 for x in b[451:]] for s, b in self.universe.items()}
        b = backtest.run_backtest(altered, self.spy, SIG, STRAT, self.lim, self.dates[250], self.dates[-1])
        before = lambda r: [(t.symbol, t.entry_date, t.entry_price) for t in r.trades if t.entry_date <= cut]  # noqa
        self.assertEqual(before(a), before(b))

    def test_out_of_sample_split(self):
        res = backtest.out_of_sample(self.universe, self.spy, SIG, STRAT, self.lim, self.dates[250], self.dates[-1])
        ins, oos = res["in_sample"], res["out_of_sample"]
        self.assertLess(ins.equity_curve[-1][0], res["split_date"])
        self.assertEqual(oos.equity_curve[0][0], res["split_date"])
        self.assertEqual(oos.metrics["start_equity"], 1000.0)
        self.assertIn(res["assessment"]["verdict"], ("passes basic out-of-sample checks (still not proof of an edge)",
                                                     "fails out-of-sample", "inconclusive"))
        md = backtest.report_markdown(res, list(self.universe), False, "test")
        self.assertIn("## Out-of-sample", md)

    def test_assessment_flags(self):
        a = backtest.assess({"total_return_pct": 20}, {"available": True, "closed_trades": 5, "total_return_pct": -3,
                                                      "excess_vs_benchmark_pct": -8, "profit_factor": 0.7,
                                                      "gap_through_stop_exits": 2})
        self.assertEqual(a["verdict"], "fails out-of-sample")
        inconsistent = backtest.assess({"total_return_pct": -3, "profit_factor": 0.77},
                                       {"available": True, "closed_trades": 71, "total_return_pct": 10,
                                        "profit_factor": 2.1, "excess_vs_benchmark_pct": 5})
        self.assertEqual(inconsistent["verdict"], "inconclusive")
        self.assertTrue(any("not consistent across periods" in f for f in inconsistent["flags"]))
        self.assertEqual(len(a["flags"]), 6)  # exposure-matched flag needs that metric

    def test_regime_filter_blocks_entries_in_downtrend(self):
        spy_down = make_bars([400 * 0.998 ** i for i in range(700)], self.dates)
        r = backtest.run_backtest(self.universe, spy_down, SIG, STRAT, self.lim, self.dates[250], self.dates[-1],
                                  regime_filter=True)
        self.assertEqual(r.trades, [])


if __name__ == "__main__":
    unittest.main()
