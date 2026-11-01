"""Owner decision 2026-11-01: keep the existing $100,000 Alpaca paper account.
The production baseline is $100,000; percentage limits, paper-only and cash-only rules are unchanged."""

import json
import unittest

from tests.helpers import CLOCK_TS, FakeAlpaca, account, asset, position, quote
from tests.test_cli_execution import CliHarness
from xahil import cli, cli_intel
from xahil.config import load_risk_limits
from xahil.risk import OrderRequest, RiskContext, account_halt_reasons, evaluate_order, parse_ts

PROD = load_risk_limits()  # the real config/risk_limits.json
ACCT = account(equity="100000", cash="100000", last_equity="100000", buying_power="400000")


def ctx(**kw):
    base = dict(account=ACCT, positions=[], open_orders=[], week_orders=[], limits=PROD, asset=asset("SPY"),
                quote=quote(765.00, 764.90), research_documented=True, now=parse_ts(CLOCK_TS), market_open=True,
                daytrade_count=0, same_day_buy_symbols=set(), research_evidence={"sufficient": True, "reasons": []})
    base.update(kw)
    return RiskContext(**base)


class ProductionBaseline(unittest.TestCase):
    def test_config_values(self):
        self.assertEqual(PROD.starting_capital, 100000)
        self.assertEqual((PROD.max_position_pct, PROD.max_total_exposure_pct, PROD.max_new_trades_per_week,
                          PROD.max_open_positions), (5, 20, 3, 4))

    def test_100k_account_is_not_halted(self):
        self.assertEqual(account_halt_reasons(ACCT, [], PROD), [])

    def test_far_above_baseline_still_halts(self):
        reasons = account_halt_reasons(account(equity="400000", cash="400000"), [], PROD)
        self.assertTrue(any("investigate" in r for r in reasons))

    def test_percentage_caps_scale_to_5k_and_20k(self):
        # 6 SPY x $765 x 1.02 buffer = $4,681 <= $5,000; 7 shares = $5,462 > $5,000
        self.assertTrue(evaluate_order(OrderRequest("buy", "SPY", qty=6), ctx()).approved)
        v = evaluate_order(OrderRequest("buy", "SPY", qty=7), ctx())
        self.assertFalse(v.checks["position_cap"])
        held = [position(s, 6, 765) for s in ("AAA", "BBB", "CCC")]  # $13,770 exposure
        held.append(position("DDD", 3, 765))                          # +$2,295 = $16,065
        v = evaluate_order(OrderRequest("buy", "SPY", qty=6), ctx(positions=held, limits=load_risk_limits()))
        self.assertFalse(v.checks["exposure_cap"])                    # 16,065 + 4,681 > 20,000

    def test_cash_only_ignores_400k_buying_power(self):
        v = evaluate_order(OrderRequest("buy", "SPY", qty=6), ctx(account=account(
            equity="100000", cash="3000", buying_power="400000", non_marginable_buying_power="3000")))
        self.assertFalse(v.checks["cash_only"])

    def test_activation_baseline_band(self):
        env = {"TELEGRAM_BOT_TOKEN": "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ0123456789", "TELEGRAM_CHAT_ID": "987654321"}
        for equity, ok in (("100000", True), ("60000", True), ("40000", False), ("1000", False), ("400000", False)):
            rep = cli_intel.activation_report(account(equity=equity), [], [], PROD, env, [], 5, 1, None, True)
            item = next(i for i in rep["items"] if i["check"] == "baseline_equity")
            with self.subTest(equity=equity):
                self.assertEqual(item["ok"], ok)


class BaselineCli(CliHarness):
    def test_reconcile_100k_is_ok(self):
        code, text = self.run_cli(["reconcile", "--workflow", "smoke-test"], FakeAlpaca(account=ACCT))
        self.assertEqual(code, cli.EXIT_OK, text)
        self.assertEqual(json.loads(text)["equity"], 100000.0)

    def test_size_uses_100k_equity(self):
        fake = FakeAlpaca(account=ACCT, quote=quote(765.00, 764.90))
        code, text = self.run_cli(["size", "SPY"], fake)
        out = json.loads(text)
        self.assertEqual((out["position_cap"], out["max_whole_shares"]), (5000.0, 6))
        self.assertIsNone(out["sizing_note"])

    def test_snapshot_records_100k_baseline(self):
        fake = FakeAlpaca(account=ACCT, bars=[{"t": "2026-10-28T04:00:00Z", "c": 769.5}])
        code, text = self.run_cli(["snapshot"], fake)
        self.assertEqual(code, cli.EXIT_OK, text)
        self.assertIn("$100,000.00", text)


if __name__ == "__main__":
    unittest.main()
