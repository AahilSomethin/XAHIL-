"""Requirement 1: paper only, fail closed."""

import re
import unittest
from pathlib import Path

from tests.helpers import FakeAlpaca, account
from xahil.alpaca import AlpacaClient, _check_url
from xahil.config import (
    APPROVED_TRADING_ENDPOINT,
    AlpacaCredentials,
    ConfigError,
    resolve_alpaca_credentials,
    resolve_data_endpoint,
    resolve_trading_endpoint,
)

ROOT = Path(__file__).resolve().parent.parent
PAPER_ENV = {"ALPACA_API_KEY": "PKTEST123", "ALPACA_SECRET_KEY": "s3cret"}


class EndpointGuard(unittest.TestCase):
    def test_unset_endpoint_defaults_to_paper(self):
        self.assertEqual(resolve_trading_endpoint({}), "https://paper-api.alpaca.markets/v2")

    def test_exact_paper_endpoint_allowed(self):
        self.assertEqual(resolve_trading_endpoint({"ALPACA_ENDPOINT": APPROVED_TRADING_ENDPOINT}), APPROVED_TRADING_ENDPOINT)

    def test_live_and_near_miss_endpoints_rejected(self):
        for bad in ("https://api.alpaca.markets/v2", "https://paper-api.alpaca.markets/v2/",
                    "https://paper-api.alpaca.markets", "http://paper-api.alpaca.markets/v2",
                    "https://paper-api.alpaca.markets.evil.com/v2", "HTTPS://PAPER-API.ALPACA.MARKETS/v2",
                    " https://paper-api.alpaca.markets/v2"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                resolve_trading_endpoint({"ALPACA_ENDPOINT": bad})

    def test_data_endpoint_must_be_exact(self):
        self.assertEqual(resolve_data_endpoint({}), "https://data.alpaca.markets/v2")
        with self.assertRaises(ConfigError):
            resolve_data_endpoint({"ALPACA_DATA_ENDPOINT": "https://data.example.com/v2"})

    def test_client_refuses_non_paper_base(self):
        with self.assertRaises(ConfigError):
            AlpacaClient(AlpacaCredentials("proxy"), trading_base="https://api.alpaca.markets/v2")

    def test_url_check_blocks_prefix_tricks(self):
        _check_url("https://paper-api.alpaca.markets/v2/account")
        for bad in ("https://paper-api.alpaca.markets/v2evil/account", "https://api.alpaca.markets/v2/orders",
                    "https://paper-api.alpaca.markets/v1/account"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                _check_url(bad)

    def test_from_env_fails_closed_on_live_endpoint_before_any_request(self):
        fake = FakeAlpaca()
        with self.assertRaises(ConfigError):
            AlpacaClient.from_env({**PAPER_ENV, "ALPACA_ENDPOINT": "https://api.alpaca.markets/v2"}, transport=fake)
        self.assertEqual(fake.calls, [])


class CredentialGuard(unittest.TestCase):
    def test_paper_key_accepted(self):
        creds = resolve_alpaca_credentials(PAPER_ENV)
        self.assertEqual(creds.headers()["APCA-API-KEY-ID"], "PKTEST123")

    def test_live_looking_key_rejected(self):
        with self.assertRaises(ConfigError):
            resolve_alpaca_credentials({"ALPACA_API_KEY": "AKLIVE123", "ALPACA_SECRET_KEY": "x"})

    def test_missing_keys_rejected(self):
        with self.assertRaises(ConfigError):
            resolve_alpaca_credentials({})

    def test_proxy_mode_sends_no_auth_headers(self):
        creds = resolve_alpaca_credentials({"XAHIL_ALPACA_AUTH": "proxy"})
        self.assertEqual(creds.headers(), {})

    def test_unknown_auth_mode_rejected(self):
        with self.assertRaises(ConfigError):
            resolve_alpaca_credentials({"XAHIL_ALPACA_AUTH": "live"})


class AccountGuard(unittest.TestCase):
    def test_non_paper_account_number_fails_closed(self):
        fake = FakeAlpaca(account=account(account_number="123456789"))
        client = AlpacaClient.from_env(PAPER_ENV, transport=fake)
        with self.assertRaises(ConfigError):
            client.account()

    def test_paper_account_ok_and_only_paper_hosts_called(self):
        fake = FakeAlpaca()
        client = AlpacaClient.from_env(PAPER_ENV, transport=fake)
        client.account()
        client.latest_quote("AAPL")
        for _, url, _, _ in fake.calls:
            self.assertTrue(url.startswith(("https://paper-api.alpaca.markets/v2/", "https://data.alpaca.markets/v2/")), url)

    def test_client_has_no_bulk_liquidation_methods(self):
        for name in ("close_all", "cancel_all", "close_all_positions", "cancel_all_orders", "order"):
            self.assertFalse(hasattr(AlpacaClient, name), name)


class RepoScan(unittest.TestCase):
    """No live trading URL may appear in code, prompts, commands or templates."""

    LIVE = re.compile(r"https?://api\.alpaca\.markets")

    def test_no_live_endpoint_anywhere_runnable(self):
        targets = list((ROOT / "xahil").rglob("*.py")) + list((ROOT / "routines").glob("*.md")) + \
            list((ROOT / ".claude" / "commands").glob("*.md")) + [ROOT / "CLAUDE.md", ROOT / ".env.example"] + \
            list((ROOT / "config").glob("*"))
        for path in targets:
            with self.subTest(path=path.name):
                self.assertIsNone(self.LIVE.search(path.read_text(encoding="utf-8")), path)

    def test_routines_never_curl_or_bulk_close(self):
        prompts = [p for p in (ROOT / "routines").glob("*.md") if p.name != "README.md"]
        self.assertEqual(len(prompts), 5)
        for path in prompts:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                for bad in ("curl ", "close-all", "cancel-all", "--force", "alpaca.sh", "XAHIL_ORDER_EXECUTION=enabled"):
                    self.assertNotIn(bad, text)
                self.assertIn("PAPER", text)
                self.assertIn("python3 -m xahil guard", text)
                self.assertIn("python3 -m xahil reconcile --workflow", text)


if __name__ == "__main__":
    unittest.main()
