"""End-to-end CLI tests with a fake Alpaca. Order execution must be off by default."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import FakeAlpaca, account, position
from xahil import cli
from xahil.alpaca import AlpacaClient
from xahil.config import ExecutionSettings

PAPER_ENV = {"ALPACA_API_KEY": "PKTEST", "ALPACA_SECRET_KEY": "x"}
APPROVED = ExecutionSettings(order_execution_enabled=True, approved_by="owner", approved_on="2026-11-01")


class CliHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.research = self.tmp / "RESEARCH-LOG.md"
        self.research.write_text("# Research Log\n\n## 2026-10-28 - Pre-market Research\n"
                                 "1. AAPL - catalyst: product event\n\n## 2026-10-27 - old\nMSFT\n", encoding="utf-8")
        patches = {
            "PROPOSALS_FILE": self.tmp / "proposed.jsonl", "EXECUTIONS_FILE": self.tmp / "executed.jsonl",
            "SNAPSHOT_FILE": self.tmp / "snap.json", "HISTORY_FILE": self.tmp / "hist.csv",
            "RECON_LOG": self.tmp / "RECON.md", "RESEARCH_LOG": self.research,
            "HALTS_FILE": self.tmp / "halts.json",
        }
        self._patchers = [mock.patch.object(cli, k, v) for k, v in patches.items()]
        self._patchers.append(mock.patch.dict("os.environ", {}, clear=True))
        self._patchers.append(mock.patch.object(cli, "load_dotenv", lambda *a, **k: None))
        self.alerts = []
        self._patchers.append(mock.patch.object(
            cli, "alert", lambda msg: (self.alerts.append(msg), {"sent": False, "captured": True})[1]))
        for p in self._patchers:
            p.start()
        # research_mentions has a default arg bound at import time; route it to the temp file.
        orig = cli.research_mentions
        self._patchers.append(mock.patch.object(cli, "research_mentions",
                                                lambda d, s, path=self.research: orig(d, s, path)))
        self._patchers[-1].start()
        # Isolated Git-backed memory logs, with sufficient fresh evidence for AAPL on the fake trading day.
        from xahil import learning, tavily
        for name, fname in (("EVIDENCE", "evidence.jsonl"), ("DECISIONS", "decisions.jsonl"),
                            ("SIGNALS", "signals.jsonl"), ("REGIMES", "regimes.jsonl"),
                            ("MISTAKES", "mistakes.jsonl"), ("MISTAKES_MD", "MISTAKES.md")):
            p = mock.patch.object(learning, name, self.tmp / fname)
            p.start()
            self._patchers.append(p)
        for name, val in (("USAGE_LOG", self.tmp / "research_usage.jsonl"), ("CACHE_DIR", self.tmp / "rcache")):
            p = mock.patch.object(tavily, name, val)
            p.start()
            self._patchers.append(p)
        for url in ("https://www.reuters.com/aapl-event", "https://apnews.com/aapl-event"):
            learning.record_evidence("AAPL", "Product event scheduled this week", url, "news", "pre-market",
                                     date="2026-10-28", path=learning.EVIDENCE, published_date="2026-10-27",
                                     provider="tavily")

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()

    def run_cli(self, argv, fake):
        factory = lambda: AlpacaClient.from_env(PAPER_ENV, transport=fake)  # noqa: E731
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(argv, client_factory=factory)
        return code, buf.getvalue()

    def writes(self, fake):
        return fake.posts()


class ExecutionGate(CliHarness):
    def test_default_is_dry_run_and_sends_no_order(self):
        fake = FakeAlpaca()
        code, text = self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(self.writes(fake), [])
        rec = json.loads(text)
        self.assertEqual(rec["status"], "dry_run_proposal")
        self.assertTrue(any("order_execution_enabled=false" in r for r in rec["not_executed_because"]))
        self.assertTrue((self.tmp / "proposed.jsonl").read_text().strip())

    def test_config_flag_alone_is_not_enough(self):
        fake = FakeAlpaca()
        with mock.patch.object(cli, "load_execution_settings", lambda: APPROVED):
            code, text = self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(self.writes(fake), [])
        self.assertIn("XAHIL_ORDER_EXECUTION", text)

    def test_env_flag_alone_is_not_enough(self):
        fake = FakeAlpaca()
        with mock.patch.dict("os.environ", {"XAHIL_ORDER_EXECUTION": "enabled"}):
            self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(self.writes(fake), [])

    def test_enabled_without_approver_stays_dry_run(self):
        fake = FakeAlpaca()
        no_name = ExecutionSettings(order_execution_enabled=True, approved_by="  ")
        with mock.patch.object(cli, "load_execution_settings", lambda: no_name), \
                mock.patch.dict("os.environ", {"XAHIL_ORDER_EXECUTION": "enabled"}):
            self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(self.writes(fake), [])

    def test_both_switches_submit_paper_order_with_idempotent_id(self):
        fake = FakeAlpaca()
        with mock.patch.object(cli, "load_execution_settings", lambda: APPROVED), \
                mock.patch.dict("os.environ", {"XAHIL_ORDER_EXECUTION": "enabled"}):
            code, text = self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event",
                                       "--workflow", "market-open"], fake)
        self.assertEqual(code, 0, text)
        posts = self.writes(fake)
        self.assertEqual(len(posts), 1)
        method, url, _, body = posts[0]
        self.assertEqual(url, "https://paper-api.alpaca.markets/v2/orders")
        sent = json.loads(body)
        self.assertEqual(sent["client_order_id"], "xahil-2026-10-28-market-open-AAPL-buy-market")
        self.assertEqual(sent["qty"], "2")
        self.assertNotIn("notional", sent)

    def test_halt_blocks_even_when_approved(self):
        fake = FakeAlpaca()
        with mock.patch.object(cli, "load_execution_settings", lambda: APPROVED), \
                mock.patch.dict("os.environ", {"XAHIL_ORDER_EXECUTION": "enabled", "XAHIL_HALT": "1"}):
            self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(self.writes(fake), [])

    def test_market_closed_not_submitted(self):
        fake = FakeAlpaca()
        fake.state["clock"] = {**fake.state["clock"], "is_open": False}
        with mock.patch.object(cli, "load_execution_settings", lambda: APPROVED), \
                mock.patch.dict("os.environ", {"XAHIL_ORDER_EXECUTION": "enabled"}):
            code, text = self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertEqual(self.writes(fake), [])

    def test_manual_flag_cannot_confirm_without_terminal(self):
        fake = FakeAlpaca()
        with mock.patch.object(cli.sys.stdin, "isatty", lambda: False):
            self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event", "--manual"], fake)
        self.assertEqual(self.writes(fake), [])

    def test_manual_flag_refused_in_cloud_even_with_terminal(self):
        fake = FakeAlpaca()
        with mock.patch.object(cli.sys.stdin, "isatty", lambda: True), \
                mock.patch.dict("os.environ", {"XAHIL_CLOUD": "1"}), \
                mock.patch("builtins.input", lambda *_: "AAPL"):
            self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event", "--manual"], fake)
        self.assertEqual(self.writes(fake), [])

    def test_manual_wrong_or_missing_confirmation_sends_nothing(self):
        for reply in ("no", "MSFT"):
            fake = FakeAlpaca()
            with self.subTest(reply=reply), mock.patch.object(cli.sys.stdin, "isatty", lambda: True), \
                    mock.patch("builtins.input", lambda *_: reply):
                self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event", "--manual"], fake)
            self.assertEqual(self.writes(fake), [])

    def test_manual_typed_symbol_submits_one_paper_order(self):
        fake = FakeAlpaca()
        with mock.patch.object(cli.sys.stdin, "isatty", lambda: True), \
                mock.patch("builtins.input", lambda *_: "aapl"):
            code, _ = self.run_cli(["buy", "AAPL", "--qty", "2", "--reason", "event", "--manual"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(len([w for w in self.writes(fake) if w[0] == "POST"]), 1)

    def test_manual_still_subject_to_risk_engine(self):
        fake = FakeAlpaca()
        with mock.patch.object(cli.sys.stdin, "isatty", lambda: True), \
                mock.patch("builtins.input", lambda *_: "AAPL"):
            code, _ = self.run_cli(["buy", "AAPL", "--qty", "4", "--reason", "event", "--manual"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertEqual(self.writes(fake), [])


class RiskThroughCli(CliHarness):
    def test_oversized_buy_rejected_exit_4(self):
        code, text = self.run_cli(["buy", "AAPL", "--qty", "3", "--reason", "x"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("position_cap", text)

    def test_symbol_missing_from_todays_research_rejected(self):
        code, text = self.run_cli(["check-buy", "MSFT", "--qty", "1"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("research", text)

    def test_sell_cancels_stop_first_then_sells(self):
        fake = FakeAlpaca(positions=[position("AAPL", 0.4, 100)],
                          orders_open=[{"id": "stop1", "symbol": "AAPL", "side": "sell", "type": "stop",
                                        "qty": "0.4", "filled_qty": "0", "stop_price": "90"}])
        with mock.patch.object(cli, "load_execution_settings", lambda: APPROVED), \
                mock.patch.dict("os.environ", {"XAHIL_ORDER_EXECUTION": "enabled"}):
            code, text = self.run_cli(["sell", "AAPL", "--all", "--reason", "cut"], fake)
        self.assertEqual(code, 0, text)
        writes = [(m, u.rsplit("/", 1)[-1]) for m, u, _, _ in self.writes(fake)]
        self.assertEqual(writes, [("DELETE", "stop1"), ("POST", "orders")])

    def test_protect_refuses_to_lower_stop(self):
        fake = FakeAlpaca(positions=[position("AAPL", 0.4, 100)],
                          orders_open=[{"id": "s", "symbol": "AAPL", "side": "sell", "type": "stop",
                                        "qty": "0.4", "filled_qty": "0", "stop_price": "95"}])
        code, text = self.run_cli(["protect", "AAPL", "--stop-pct", "10"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("never move down", text)

    def test_protect_fractional_uses_day_stop(self):
        fake = FakeAlpaca(positions=[position("AAPL", 0.4, 100)])
        code, text = self.run_cli(["protect", "AAPL", "--trail-pct", "10"], fake)
        rec = json.loads(text)
        self.assertEqual(rec["order"]["type"], "stop")
        self.assertEqual(rec["order"]["time_in_force"], "day")
        self.assertEqual(rec["order"]["stop_price"], "90.00")

    def test_live_endpoint_env_fails_closed(self):
        fake = FakeAlpaca()
        factory = lambda: AlpacaClient.from_env({**PAPER_ENV, "ALPACA_ENDPOINT": "https://api.alpaca.markets/v2"},  # noqa
                                                transport=fake)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["account"], client_factory=factory)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("fail closed", err.getvalue())
        self.assertEqual(fake.calls, [])


class Workflows(CliHarness):
    def test_reconcile_writes_snapshot_and_halts_on_100k(self):
        fake = FakeAlpaca(account=account(equity="100000", cash="100000"))
        code, text = self.run_cli(["reconcile", "--workflow", "pre-market"], fake)
        self.assertEqual(code, cli.EXIT_HALT)
        self.assertTrue((self.tmp / "snap.json").exists())
        self.assertIn("reset the paper account", (self.tmp / "RECON.md").read_text())

    def test_reconcile_detects_account_swap(self):
        (self.tmp / "snap.json").write_text(json.dumps({"account_number": "PA999"}))
        code, _ = self.run_cli(["reconcile", "--workflow", "midday"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_HALT)

    def test_midday_scan_flags_cut_and_tighten(self):
        fake = FakeAlpaca(positions=[position("LOSE", 0.5, 93, entry=100, plpc=-0.07),
                                     position("WIN", 2, 21, entry=17.5, plpc=0.2)])
        code, text = self.run_cli(["midday-scan"], fake)
        actions = {(a["symbol"], a["action"]) for a in json.loads(text)["actions"]}
        self.assertIn(("LOSE", "SELL_ALL"), actions)
        self.assertIn(("WIN", "TIGHTEN_STOP"), actions)
        self.assertIn(("WIN", "ADD_STOP"), actions)
        self.assertEqual(self.writes(fake), [])

    def test_snapshot_records_equity_and_spy(self):
        fake = FakeAlpaca(bars=[{"t": "2026-10-28T04:00:00Z", "c": 580.5}],
                          account=account(equity="1010", last_equity="1000"))
        code, text = self.run_cli(["snapshot"], fake)
        self.assertEqual(code, 0)
        self.assertIn("+$10.00", text)
        self.assertIn("580.5", (self.tmp / "hist.csv").read_text())

    def test_snapshot_refuses_bad_baseline(self):
        fake = FakeAlpaca(account=account(equity="100000", cash="100000"))
        code, text = self.run_cli(["snapshot"], fake)
        self.assertEqual(code, cli.EXIT_HALT)
        self.assertFalse((self.tmp / "hist.csv").exists())

    def test_invalid_credentials_fail_cleanly(self):
        fake = FakeAlpaca()
        fake.state["account"] = None
        orig = fake.__call__

        def unauthorized(method, url, headers, body):
            if url.endswith("/account"):
                return 401, '{"message": "unauthorized."}'
            return orig(method, url, headers, body)

        factory = lambda: AlpacaClient.from_env(PAPER_ENV, transport=unauthorized)  # noqa: E731
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = cli.main(["account"], client_factory=factory)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("HTTP 401", err.getvalue())
        self.assertNotIn("PKTEST", err.getvalue())

    def test_market_status(self):
        code, text = self.run_cli(["market-status"], FakeAlpaca())
        data = json.loads(text)
        self.assertTrue(data["trading_day"])
        self.assertFalse(data["early_close"])


if __name__ == "__main__":
    unittest.main()
