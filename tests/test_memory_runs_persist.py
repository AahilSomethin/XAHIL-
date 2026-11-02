"""Learning memory, run markers (duplicates, interruptions, calendar windows), Git persistence,
the activation check, and the intelligence CLI commands. Git tests use temporary local repos only."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import FakeAlpaca, account, limits, position, quote
from tests.test_analysis import SIG, make_bars, trading_days, uptrend
from tests.test_cli_execution import CliHarness
from xahil import cli, cli_intel, data, journal, learning, runs

ET = dt.timezone(dt.timedelta(hours=-4))
SESSION = {"date": "2026-10-28", "open": "09:30", "close": "16:00"}
HALF = {"date": "2026-11-27", "open": "09:30", "close": "13:00"}


def at(h, m, day=28, month=10):
    return dt.datetime(2026, month, day, h, m, tzinfo=ET)


# --- learning ----------------------------------------------------------------

class Learning(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ev, self.dec = self.tmp / "evidence.jsonl", self.tmp / "decisions.jsonl"

    def test_evidence_and_decision_linking(self):
        e = learning.record_evidence("aapl", "Beat earnings", "https://example.com/a", "news", "pre-market",
                                     date="2026-10-28", path=self.ev)
        self.assertEqual(e["id"], "e-2026-10-28-1")
        self.assertEqual(e["trust"], "untrusted-external")
        d = learning.record_decision("aapl", "propose", "earnings beat", "pre-market", [e["id"], "e-bogus"],
                                     date="2026-10-28", path=self.dec, evidence_path=self.ev)
        self.assertEqual(d["symbol"], "AAPL")
        self.assertEqual(d["unknown_evidence_ids"], ["e-bogus"])

    def test_validation(self):
        with self.assertRaises(ValueError):
            learning.record_evidence("A", "claim", "not-a-url", "web", "w", path=self.ev)
        with self.assertRaises(ValueError):
            learning.record_decision("A", "buy-now", "x", "w", path=self.dec, evidence_path=self.ev)
        with self.assertRaises(ValueError):
            learning.record_decision("A", "skip", "  ", "w", path=self.dec, evidence_path=self.ev)

    def test_mistake_written_to_both_logs(self):
        md = self.tmp / "MISTAKES.md"
        learning.record_mistake("Chased a gap", "Bought after +8% open", "Wait for a pullback", "XYZ",
                                date="2026-10-28", path=self.tmp / "m.jsonl", md_path=md)
        self.assertIn("Chased a gap", md.read_text())
        self.assertEqual(len(journal.read(self.tmp / "m.jsonl")), 1)

    def test_forward_outcome(self):
        dates = trading_days(30, dt.date(2026, 9, 1))
        bars = make_bars([10 + i * 0.1 for i in range(30)], dates)
        o = learning.forward_outcome(bars, dates[5], 10)
        self.assertEqual(o["entry_date"], dates[6])
        self.assertTrue(o["complete"])
        self.assertGreater(o["ret_10d_pct"], 0)
        self.assertIsNone(o["stop_hit_day"])
        crash = make_bars([10] * 8 + [8] * 22, dates)
        self.assertEqual(learning.forward_outcome(crash, dates[5], 10)["stop_hit_day"], 2)

    def test_weekly_analysis(self):
        dates = trading_days(60, dt.date(2026, 8, 3))
        up = make_bars([10 * 1.01 ** i for i in range(60)], dates)
        down = make_bars([10 * 0.99 ** i for i in range(60)], dates)
        sig_rows = [{"symbol": "UP", "as_of": dates[i], "qualified": True, "stop_pct": 8, "failed": []} for i in range(20, 32)]
        sig_rows += [{"symbol": "DN", "as_of": dates[i], "qualified": False, "failed": ["uptrend"]} for i in range(20, 32)]
        week_end = dates[-1]
        week_start = (dt.date.fromisoformat(week_end) - dt.timedelta(days=4)).isoformat()
        props = [{"date": dates[40], "status": "rejected_by_risk_engine", "order": {"symbol": "DN", "side": "buy"},
                  "checks": {"spread": False}} for _ in range(3)]
        trades = [{"symbol": "DN", "entry_time": dates[-3] + "T14:00:00Z", "exit_time": week_end + "T15:00:00Z",
                   "return_pct": -12.0}]
        a = learning.weekly_analysis(dates[40], week_end, signals=sig_rows, decisions=[], evidence=[],
                                     proposals=props, mistakes=[], regimes=[], closed_trades=trades,
                                     bars={"UP": up, "DN": down}, lookback_days=60)
        self.assertEqual(a["signal_efficacy"]["qualified"]["n"], 12)
        self.assertGreater(a["signal_edge_10d_pct"], 0)
        text = "\n".join(a["mistake_candidates"])
        self.assertIn("exceeded the 10% stop band", text)
        self.assertIn("rejected on 'spread'", text)
        md = learning.analysis_markdown(a)
        self.assertIn("Signal efficacy", md)
        self.assertIn("No rule changes without owner approval", md)


# --- run markers ----------------------------------------------------------------

class Runs(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_window_proceed_duplicate_and_finish(self):
        r = runs.start("pre-market", at(8, 7), SESSION, self.dir)
        self.assertEqual(r["decision"], runs.PROCEED)
        self.assertEqual(runs.start("pre-market", at(8, 20), SESSION, self.dir)["decision"], runs.SKIP_RUNNING)
        self.assertTrue(runs.finish("pre-market", at(8, 30), "completed", "ok", self.dir)["ok"])
        self.assertEqual(runs.start("pre-market", at(8, 40), SESSION, self.dir)["decision"], runs.SKIP_DONE)

    def test_interrupted_run_recovered_after_stale_window(self):
        runs.start("market-open", at(9, 47), SESSION, self.dir)
        r = runs.start("market-open", at(10, 59), SESSION, self.dir)  # 72 min: still considered running
        self.assertEqual(r["decision"], runs.SKIP_RUNNING)
        r = runs.start("market-open", at(9, 47) + dt.timedelta(minutes=91) - dt.timedelta(minutes=0), SESSION, self.dir)
        self.assertEqual(r["decision"], runs.SKIP_WINDOW)  # 11:18 is past the market-open window
        forced = runs.start("market-open", at(10, 0), SESSION, self.dir, force_recover=True)
        self.assertEqual(forced["decision"], runs.PROCEED)
        self.assertIn("interrupted", forced["recovery_note"])
        self.assertEqual(forced["marker"]["attempt"], 2)

    def test_other_interrupted_workflows_reported(self):
        runs.start("pre-market", at(8, 7), SESSION, self.dir)
        r = runs.start("market-open", at(9, 47), SESSION, self.dir)
        self.assertEqual(r["interrupted_today"], ["pre-market"])

    def test_calendar_awareness(self):
        self.assertEqual(runs.start("midday", at(12, 7), None, self.dir)["decision"], runs.SKIP_CLOSED)
        self.assertEqual(runs.start("midday", at(15, 45), SESSION, self.dir)["decision"], runs.SKIP_WINDOW)
        self.assertEqual(runs.start("daily-summary", at(16, 5), SESSION, self.dir)["decision"], runs.SKIP_WINDOW)
        self.assertEqual(runs.start("daily-summary", at(16, 22), SESSION, self.dir)["decision"], runs.PROCEED)
        # half-day: close 13:00, midday must finish by 12:30
        self.assertEqual(runs.start("midday", at(12, 40, 27, 11), HALF, self.dir)["decision"], runs.SKIP_WINDOW)
        self.assertEqual(runs.start("midday", at(12, 7, 27, 11), HALF, self.dir)["decision"], runs.PROCEED)

    def test_weekly_review_once_per_week_and_not_during_a_session(self):
        self.assertEqual(runs.start("weekly-review", at(16, 30), SESSION, self.dir)["decision"], runs.SKIP_WINDOW)
        sat = dt.datetime(2026, 10, 31, 10, 7, tzinfo=ET)
        r = runs.start("weekly-review", sat, None, self.dir)
        self.assertEqual((r["decision"], r["marker"]["date"]), (runs.PROCEED, "2026-W44"))
        runs.finish("weekly-review", sat, "completed", runs_dir=self.dir)
        sun = sat + dt.timedelta(days=1)
        self.assertEqual(runs.start("weekly-review", sun, None, self.dir)["decision"], runs.SKIP_DONE)
        self.assertEqual(runs.completed_count("weekly-review", self.dir), 1)

    def test_finish_without_start(self):
        self.assertFalse(runs.finish("midday", at(12, 30), "completed", runs_dir=self.dir)["ok"])

    def test_weekly_review_started_sunday_night_finishes_after_midnight(self):
        sun = dt.datetime(2026, 11, 1, 23, 50, tzinfo=ET)  # ISO week 44
        self.assertEqual(runs.start("weekly-review", sun, None, self.dir)["marker"]["date"], "2026-W44")
        r = runs.finish("weekly-review", sun + dt.timedelta(minutes=20), "completed", runs_dir=self.dir)  # W45
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["marker"]["date"], "2026-W44")

    def test_daily_finish_never_completes_yesterdays_marker(self):
        runs.start("midday", at(12, 7, 27), {**SESSION, "date": "2026-10-27"}, self.dir)  # interrupted yesterday
        self.assertFalse(runs.finish("midday", at(12, 30), "completed", runs_dir=self.dir)["ok"])
        self.assertEqual(runs.read_marker(runs.marker_path("2026-10-27", "midday", self.dir))["status"], "started")

    def test_markers_are_written_atomically(self):
        runs.start("midday", at(12, 7), SESSION, self.dir)
        runs.finish("midday", at(12, 30), "completed", runs_dir=self.dir)
        self.assertEqual(list(self.dir.rglob("*.tmp")), [])


# --- git persistence ------------------------------------------------------------
# Shared state-branch persistence (memory-sync, run claims, failures, recovery, audit) is tested
# offline in tests/test_state_persistence.py.


# --- activation check -------------------------------------------------------------

class Activation(unittest.TestCase):
    def report(self, **kw):
        base = dict(acct=account(), positions=[], open_orders=[], limits=limits(),
                    env={"TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789", "TELEGRAM_CHAT_ID": "987654321"}, halts=[], completed_summaries=5,
                    completed_reviews=1, latest_backtest={"assessment": {"verdict": "inconclusive"}},
                    snapshot_exists=True)
        base.update(kw)
        return cli_intel.activation_report(**base)

    def test_ready_when_preconditions_hold(self):
        r = self.report()
        self.assertTrue(r["ready_for_owner_activation_decision"])
        owner = next(i for i in r["items"] if i["check"] == "owner_approval")
        self.assertFalse(owner["ok"])  # never auto-approved

    def test_blocked_conditions(self):
        cases = {
            "fractional position": dict(positions=[position("F", 0.4, 10)]),
            "whole shares, DAY stop only": dict(positions=[position("F", 2, 10)],
                                                open_orders=[{"symbol": "F", "side": "sell", "type": "stop",
                                                              "time_in_force": "day"}]),
            "$100k balance": dict(acct=account(equity="100000")),
            "halt": dict(halts=["account-changed"]),
            "no telegram": dict(env={}),
            "no track record": dict(completed_summaries=2),
            "no snapshot": dict(snapshot_exists=False),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                self.assertFalse(self.report(**kw)["ready_for_owner_activation_decision"])

    def test_failed_backtest_is_reported_but_not_blocking(self):
        r = self.report(latest_backtest={"assessment": {"verdict": "fails out-of-sample"}})
        bt = next(i for i in r["items"] if i["check"] == "backtest_evidence")
        self.assertFalse(bt["ok"])
        self.assertFalse(bt["blocking"])


# --- intelligence CLI ---------------------------------------------------------------

class IntelCli(CliHarness):
    def setUp(self):
        super().setUp()
        t = self.tmp
        for name, attr in (("SIGNALS", "signals.jsonl"), ("DECISIONS", "decisions.jsonl"),
                           ("EVIDENCE", "evidence.jsonl"), ("REGIMES", "regimes.jsonl"),
                           ("MISTAKES", "mistakes.jsonl"), ("MISTAKES_MD", "MISTAKES.md")):
            p = mock.patch.object(learning, name, t / attr)
            p.start()
            self._patchers.append(p)
        for obj, name, val in ((runs, "RUNS_DIR", t / "runs"), (cli_intel, "BACKTEST_DIR", t / "backtests"),
                               (cli_intel, "BACKTEST_INDEX", t / "backtests.jsonl"), (data, "CACHE_DIR", t / "cache")):
            p = mock.patch.object(obj, name, val)
            p.start()
            self._patchers.append(p)
        dates = trading_days(700, dt.date(2024, 1, 2))
        dates = [d for d in dates if d <= "2026-10-28"]
        n = len(dates)
        self.bars = {"SPY": make_bars(uptrend(n, base=300, growth=0.0003, wiggle=0.005), dates)}
        for k, s in enumerate(("AAA", "BBB", "CCC")):
            self.bars[s] = make_bars(uptrend(n, base=8 + k * 30, growth=0.001, wiggle=0.03, period=8 + k), dates)
        uni = Path(t / "universe.json")
        self.fake = FakeAlpaca(bars_by_symbol=self.bars)

    def test_regime_records(self):
        code, text = self.run_cli(["regime", "--record", "--workflow", "pre-market"], self.fake)
        self.assertEqual(code, 0)
        self.assertIn(json.loads(text)["regime"], ("risk_on", "neutral", "risk_off", "high_volatility"))
        self.assertEqual(len(journal.read(learning.REGIMES)), 1)

    def test_signals_record_qualified_and_rejected(self):
        code, text = self.run_cli(["signals", "--symbols", "AAA", "BBB", "CCC", "--record"], self.fake)
        self.assertEqual(code, 0, text)
        out = json.loads(text)
        self.assertEqual(out["evaluated"], 3)
        rows = journal.read(learning.SIGNALS)
        self.assertEqual(len(rows), 3)
        expensive = next(r for r in rows if r["symbol"] == "CCC")  # ~$70+: no whole share fits $50
        self.assertIn("whole_share_affordable", expensive["failed"])
        code, text = self.run_cli(["signals", "--symbols", "AAA", "--explain", "AAA"], self.fake)
        self.assertIn("checks", json.loads(text))

    def test_backtest_save(self):
        code, text = self.run_cli(["backtest", "--start", "2025-01-02", "--end", "2026-10-27",
                                   "--symbols", "AAA", "BBB", "--save"], self.fake)
        self.assertEqual(code, 0, text)
        out = json.loads(text)
        self.assertIn("verdict", out["assessment"])
        self.assertTrue(list((self.tmp / "backtests").glob("*.md")))
        self.assertEqual(len(journal.read(self.tmp / "backtests.jsonl")), 1)

    def test_size_whole_shares(self):
        code, text = self.run_cli(["size", "AAPL"], self.fake)  # fake ask $20
        self.assertEqual(json.loads(text)["max_whole_shares"], 2)
        self.fake.state["quote"] = quote(60, 59.9)
        code, _ = self.run_cli(["size", "AAPL"], self.fake)
        self.assertEqual(code, cli.EXIT_REJECTED)

    def test_decide_evidence_mistake(self):
        _, text = self.run_cli(["evidence", "AAA", "--claim", "Guidance raised", "--source", "https://x.test/1"], self.fake)
        eid = json.loads(text)["id"]
        code, _ = self.run_cli(["decide", "AAA", "--action", "propose", "--reason", "catalyst", "--evidence", eid], self.fake)
        self.assertEqual(code, 0)
        code, _ = self.run_cli(["decide", "AAA", "--action", "skip", "--reason", "x", "--evidence", "e-none"], self.fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        code, _ = self.run_cli(["mistake", "--title", "t", "--what", "w", "--lesson", "l"], self.fake)
        self.assertEqual(code, 0)

    def test_learn_runs(self):
        self.run_cli(["signals", "--symbols", "AAA", "BBB", "--record"], self.fake)
        code, text = self.run_cli(["learn", "--week-ending", "2026-10-23"], self.fake)
        self.assertEqual(code, 0, text)
        self.assertIn("Learning analysis", text)

    def test_run_start_and_finish(self):
        code, text = self.run_cli(["run-start", "--workflow", "market-open"], self.fake)  # clock 10:00 ET
        self.assertEqual(code, 0, text)
        code, _ = self.run_cli(["run-start", "--workflow", "market-open"], self.fake)
        self.assertEqual(code, 5)
        code, _ = self.run_cli(["run-finish", "--workflow", "market-open", "--status", "completed"], self.fake)
        self.assertEqual(code, 0)
        code, _ = self.run_cli(["run-start", "--workflow", "pre-market"], self.fake)  # 10:00 is after pre-market
        self.assertEqual(code, 5)

    def test_activation_check_not_ready_by_default(self):
        code, text = self.run_cli(["activation-check"], self.fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertFalse(json.loads(text)["ready_for_owner_activation_decision"])


if __name__ == "__main__":
    unittest.main()
