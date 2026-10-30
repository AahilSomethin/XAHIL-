"""Requirements 9 and 11: reconciliation and performance tracking."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from tests.helpers import account, limits, position
from xahil import metrics, reconcile


def row(date, equity, spy=None):
    return {"date": date, "equity": equity, "cash": equity, "long_value": 0.0, "spy_close": spy}


class Metrics(unittest.TestCase):
    def test_max_drawdown(self):
        dd = metrics.max_drawdown([1000, 1100, 990, 1050, 880, 1200])
        self.assertAlmostEqual(dd["max_drawdown_pct"], -20.0)
        self.assertEqual(dd["current_drawdown_pct"], 0.0)

    def test_week_stats_vs_spy(self):
        rows = [row("2026-10-23", 1000, 500), row("2026-10-26", 1010, 505), row("2026-10-30", 1030, 510)]
        s = metrics.period_stats(rows, dt.date(2026, 10, 26), dt.date(2026, 10, 30))
        self.assertEqual(s["start_equity"], 1000)  # previous Friday close
        self.assertAlmostEqual(s["return_pct"], 3.0)
        self.assertAlmostEqual(s["spy_return_pct"], 2.0)
        self.assertAlmostEqual(s["vs_spy_pct"], 1.0)

    def test_inception_stats(self):
        rows = [row("2026-10-23", 1000, 500), row("2026-10-30", 950, 525)]
        s = metrics.inception_stats(rows)
        self.assertAlmostEqual(s["return_pct"], -5.0)
        self.assertAlmostEqual(s["vs_spy_pct"], -10.0)

    def test_history_roundtrip_and_upsert(self):
        path = Path(tempfile.mkdtemp()) / "h.csv"
        rows = metrics.upsert_row([], row("2026-10-28", 1000, 580.0))
        rows = metrics.upsert_row(rows, row("2026-10-28", 1005, 581.0))
        metrics.save_history(path, rows)
        loaded = metrics.load_history(path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["equity"], 1005)

    def test_fifo_win_rate_with_partial_fills(self):
        fills = [
            {"order_id": "b1", "symbol": "AAA", "side": "buy", "qty": "0.2", "price": "100", "transaction_time": "2026-10-26T14:00:00Z"},
            {"order_id": "b1", "symbol": "AAA", "side": "buy", "qty": "0.2", "price": "100", "transaction_time": "2026-10-26T14:00:01Z"},
            {"order_id": "s1", "symbol": "AAA", "side": "sell", "qty": "0.4", "price": "110", "transaction_time": "2026-10-28T15:00:00Z"},
            {"order_id": "b2", "symbol": "BBB", "side": "buy", "qty": "1", "price": "40", "transaction_time": "2026-10-26T14:00:00Z"},
            {"order_id": "s2", "symbol": "BBB", "side": "sell", "qty": "1", "price": "37", "transaction_time": "2026-10-29T15:00:00Z"},
            {"order_id": "b3", "symbol": "CCC", "side": "buy", "qty": "1", "price": "10", "transaction_time": "2026-10-29T15:00:00Z"},
        ]
        trades = metrics.closed_trades_from_fills(fills)
        self.assertEqual(len(trades), 2)  # CCC still open
        s = metrics.trade_stats(trades)
        self.assertEqual((s["wins"], s["losses"]), (1, 1))
        self.assertEqual(s["win_rate_pct"], 50.0)
        self.assertAlmostEqual(s["realized_pnl"], 1.0)  # +4 - 3
        self.assertAlmostEqual(s["profit_factor"], 1.33)
        self.assertEqual(s["best"].symbol, "AAA")

    def test_no_trades(self):
        s = metrics.trade_stats([])
        self.assertIsNone(s["win_rate_pct"])
        self.assertIsNone(s["profit_factor"])


class Reconcile(unittest.TestCase):
    def test_first_run(self):
        r = reconcile.reconcile({}, "2026-10-28T09:47", "market-open", account(), [], [], limits(), True)
        self.assertTrue(r["ok"])
        self.assertIn("first run", r["differences"][0])

    def test_detects_stopped_out_and_new_positions(self):
        prev = reconcile.build_snapshot("t0", "w", account(), [position("AAA", 0.4, 100)],
                                        [{"id": "stopid123", "symbol": "AAA", "side": "sell", "type": "stop"}])
        r = reconcile.reconcile(prev, "t1", "midday", account(), [position("BBB", 1, 30)], [], limits(), True)
        text = "\n".join(r["differences"])
        self.assertIn("AAA: position closed", text)
        self.assertIn("BBB: new position", text)
        self.assertIn("no longer open", text)
        self.assertEqual(r["unprotected_positions"], ["BBB"])

    def test_limit_breach_from_drift_is_reported(self):
        r = reconcile.reconcile({}, "t", "w", account(), [position("AAA", 1, 80)], [], limits(), False)
        self.assertTrue(any("AAA is 8.0%" in b for b in r["limit_breaches"]))
        self.assertTrue(r["ok"])  # drift blocks adds via the risk engine, not a full halt

    def test_short_position_halts(self):
        r = reconcile.reconcile({}, "t", "w", account(), [position("AAA", -1, 10)], [], limits(), True)
        self.assertFalse(r["ok"])

    def test_log_entry_format(self):
        r = reconcile.reconcile({}, "2026-10-28T09:47", "market-open", account(), [], [], limits(), True)
        self.assertIn("## 2026-10-28T09:47 - market-open", reconcile.format_log_entry(r))


if __name__ == "__main__":
    unittest.main()
