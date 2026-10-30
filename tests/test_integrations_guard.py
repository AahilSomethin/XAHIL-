"""Requirements 4, 5, 6, 10, 12: Telegram, optional Perplexity, future providers, guard, safe config."""

import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from xahil import guard, integrations, notify, research
from xahil.config import (
    CONFIG_DIR,
    ConfigError,
    load_dotenv,
    load_execution_settings,
    load_integrations,
    load_risk_limits,
)

TODAY = dt.date(2026, 10, 28)
ON = {"perplexity": {"enabled": True, "model": "sonar", "monthly_request_cap": 2}}


class Telegram(unittest.TestCase):
    def setUp(self):
        self.outbox = Path(tempfile.mkdtemp()) / "OUTBOX.md"

    def test_fallback_when_unconfigured(self):
        r = notify.send("hello", env={}, outbox=self.outbox)
        self.assertTrue(r["fallback"])
        self.assertIn("hello", self.outbox.read_text())

    def test_sends_plain_text_to_telegram(self):
        seen = {}

        def post(url, payload):
            seen.update(url=url, payload=payload)
            return 200, "{}"

        r = notify.send("EOD", env={"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"}, post=post, outbox=self.outbox)
        self.assertTrue(r["sent"])
        self.assertEqual(seen["url"], "https://api.telegram.org/bot123:abc/sendMessage")
        self.assertEqual(seen["payload"]["chat_id"], "42")
        self.assertNotIn("parse_mode", seen["payload"])

    def test_error_redacts_token_and_falls_back(self):
        post = lambda url, payload: (401, f"bad token at {url}")  # noqa: E731
        r = notify.send("x", env={"TELEGRAM_BOT_TOKEN": "123:SECRET", "TELEGRAM_CHAT_ID": "1"}, post=post, outbox=self.outbox)
        self.assertTrue(r["fallback"])
        self.assertNotIn("SECRET", r["reason"])
        self.assertNotIn("SECRET", self.outbox.read_text())

    def test_network_exception_never_raises(self):
        def post(url, payload):
            raise OSError("down")
        r = notify.send("x", env={"TELEGRAM_BOT_TOKEN": "1:a", "TELEGRAM_CHAT_ID": "1"}, post=post, outbox=self.outbox)
        self.assertTrue(r["fallback"])

    def test_truncates_long_messages(self):
        self.assertLessEqual(len(notify.truncate("x" * 10000)), 4000)


class Perplexity(unittest.TestCase):
    def setUp(self):
        self.usage = Path(tempfile.mkdtemp()) / "usage.json"

    def test_disabled_by_default_in_repo_config(self):
        self.assertFalse(load_integrations()["perplexity"]["enabled"])
        with self.assertRaises(research.ResearchUnavailable):
            research.check_available(load_integrations(), {"PERPLEXITY_API_KEY": "k"}, TODAY, self.usage)

    def test_needs_key(self):
        with self.assertRaises(research.ResearchUnavailable):
            research.check_available(ON, {}, TODAY, self.usage)

    def test_monthly_cap_enforced_and_counted_before_send(self):
        body = json.dumps({"choices": [{"message": {"content": "SPY up"}}], "citations": ["https://x.test/a"]})
        calls = []

        def post(url, headers, payload):
            calls.append(headers)
            return 200, body

        env = {"PERPLEXITY_API_KEY": "k"}
        for _ in range(2):
            r = research.ask("q", env=env, today=TODAY, post=post, settings=ON, usage_path=self.usage)
        self.assertEqual(r["citations"], ["https://x.test/a"])
        with self.assertRaises(research.ResearchUnavailable):
            research.ask("q", env=env, today=TODAY, post=post, settings=ON, usage_path=self.usage)
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(self.usage.read_text())["2026-10"], 2)

    def test_proxy_mode_sends_no_key(self):
        body = json.dumps({"choices": [{"message": {"content": "a"}}]})
        seen = []
        research.ask("q", env={"XAHIL_PERPLEXITY_AUTH": "proxy"}, today=TODAY,
                     post=lambda u, h, p: (seen.append(h), (200, body))[1], settings=ON, usage_path=self.usage)
        self.assertNotIn("Authorization", seen[0])

    def test_uncited_answer_flagged(self):
        md = research.to_markdown({"query": "q", "answer": "a", "citations": [], "model": "sonar"})
        self.assertIn("unverified", md)


class FutureProviders(unittest.TestCase):
    def test_all_off_by_default(self):
        self.assertEqual(integrations.enabled_providers(load_integrations()), [])

    def test_enabled_stub_reports_error_without_raising(self):
        signals, errors = integrations.collect_signals({"quiver_quant": {"enabled": True}}, ["AAPL"])
        self.assertEqual(signals, [])
        self.assertIn("not integrated", errors[0])


class Guard(unittest.TestCase):
    def test_memory_allowed(self):
        self.assertEqual(guard.classify(["memory/TRADE-LOG.md", "memory/state/equity_history.csv"]), [])

    def test_strategy_code_config_env_blocked(self):
        bad = ["memory/TRADING-STRATEGY.md", "memory/PROJECT-CONTEXT.md", "config/execution.json",
               "xahil/risk.py", "routines/midday.md", "CLAUDE.md", ".env", "memory/.env", "tests/test_risk.py"]
        self.assertEqual(guard.classify(bad), bad)

    def test_windows_separators_normalised(self):
        self.assertEqual(guard.classify(["config\\execution.json"]), ["config/execution.json"])


class SafeConfig(unittest.TestCase):
    def copy_config(self):
        d = Path(tempfile.mkdtemp())
        for f in CONFIG_DIR.iterdir():
            shutil.copy(f, d / f.name)
        return d

    def test_repo_defaults(self):
        lim = load_risk_limits()
        self.assertEqual((lim.starting_capital, lim.max_position_pct, lim.max_total_exposure_pct,
                          lim.max_new_trades_per_week), (1000, 5, 20, 3))
        ex = load_execution_settings()
        self.assertFalse(ex.order_execution_enabled)
        self.assertFalse(ex.halt)

    def test_loosening_limits_fails_closed(self):
        for key, value in (("max_position_pct", 6), ("max_total_exposure_pct", 25),
                           ("max_new_trades_per_week", 4), ("starting_capital", 0)):
            with self.subTest(key=key):
                d = self.copy_config()
                raw = json.loads((d / "risk_limits.json").read_text())
                raw[key] = value
                (d / "risk_limits.json").write_text(json.dumps(raw))
                with self.assertRaises(ConfigError):
                    load_risk_limits(d)

    def test_tightening_allowed(self):
        d = self.copy_config()
        raw = json.loads((d / "risk_limits.json").read_text())
        raw["max_position_pct"] = 3
        (d / "risk_limits.json").write_text(json.dumps(raw))
        self.assertEqual(load_risk_limits(d).max_position_pct, 3)

    def test_unknown_key_rejected(self):
        d = self.copy_config()
        raw = json.loads((d / "risk_limits.json").read_text())
        raw["allow_margin"] = True
        (d / "risk_limits.json").write_text(json.dumps(raw))
        with self.assertRaises(ConfigError):
            load_risk_limits(d)

    def test_string_true_is_not_true(self):
        d = self.copy_config()
        (d / "execution.json").write_text(json.dumps({"order_execution_enabled": "true"}))
        with self.assertRaises(ConfigError):
            load_execution_settings(d)

    def test_dotenv_not_executed_and_ignored_in_cloud(self):
        p = Path(tempfile.mkdtemp()) / ".env"
        p.write_text("export ALPACA_API_KEY='PKX'\n$(rm -rf /)\nFOO=bar\n")
        env = {}
        load_dotenv(p, env)
        self.assertEqual(env, {"ALPACA_API_KEY": "PKX", "FOO": "bar"})
        cloud = {"XAHIL_CLOUD": "1"}
        load_dotenv(p, cloud)
        self.assertNotIn("ALPACA_API_KEY", cloud)


if __name__ == "__main__":
    unittest.main()
