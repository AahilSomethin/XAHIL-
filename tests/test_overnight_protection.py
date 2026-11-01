"""Fractional-share overnight protection gap: a DAY stop is never treated as continuous protection."""

import json
import unittest

from tests.helpers import CLOCK_TS, FakeAlpaca, account, asset, limits, position, quote
from tests.test_cli_execution import CliHarness
from xahil import cli
from xahil.risk import OrderRequest, RiskContext, evaluate_order, overnight_protection_gaps, parse_ts

NOW = parse_ts(CLOCK_TS)


def stop(sym, tif, kind="stop", qty="1"):
    return {"id": f"{sym}-{tif}-{kind}", "symbol": sym, "side": "sell", "type": kind, "time_in_force": tif,
            "qty": qty, "filled_qty": "0", "stop_price": "18"}


def ctx(**kw):
    base = dict(account=account(), positions=[], open_orders=[], week_orders=[], limits=limits(), asset=asset(),
                quote=quote(20, 19.99), research_documented=True, now=NOW, market_open=True, daytrade_count=0,
                same_day_buy_symbols=set(), research_evidence={'sufficient': True, 'reasons': []})  # whole shares default True
    base.update(kw)
    return RiskContext(**base)


def failed(v):
    return {k for k, ok in v.checks.items() if not ok}


class WholeShareEntries(unittest.TestCase):
    def test_whole_share_entry_approved(self):
        v = evaluate_order(OrderRequest("buy", "AAPL", qty=2), ctx())
        self.assertTrue(v.approved, v.reasons)

    def test_notional_and_fractional_entries_rejected(self):
        for order in (OrderRequest("buy", "AAPL", notional=40), OrderRequest("buy", "AAPL", qty=1.5)):
            with self.subTest(order=order):
                v = evaluate_order(order, ctx())
                self.assertIn("gtc_protectable", failed(v))
                self.assertTrue(any("unprotected overnight" in r for r in v.reasons))

    def test_expensive_stock_cannot_be_bought_at_all(self):
        # $1,000 x 5% = $50 cap: a $60 stock fits no whole share, and fractions are refused.
        v = evaluate_order(OrderRequest("buy", "AAPL", qty=1), ctx(quote=quote(60, 59.99)))
        self.assertIn("position_cap", failed(v))


class OvernightGaps(unittest.TestCase):
    def test_only_gtc_on_whole_shares_counts(self):
        cases = [
            ([position("A", 2, 20)], [stop("A", "gtc")], []),
            ([position("A", 2, 20)], [stop("A", "gtc", "trailing_stop")], []),
            ([position("A", 2, 20)], [stop("A", "day")], ["A"]),           # DAY stop expires at the close
            ([position("A", 2, 20)], [], ["A"]),
            ([position("A", 0.4, 100)], [stop("A", "day", qty="0.4")], ["A"]),  # fractional: never protected overnight
        ]
        for pos, orders, expected in cases:
            with self.subTest(pos=pos[0]["qty"], orders=[(o["type"], o["time_in_force"]) for o in orders]):
                self.assertEqual(overnight_protection_gaps(pos, orders), expected)

    def test_day_stop_does_not_unblock_entries(self):
        held = [position("A", 2, 20)]
        v = evaluate_order(OrderRequest("buy", "AAPL", qty=1), ctx(positions=held, protection_gaps=["A"]))
        self.assertIn("positions_protected", failed(v))


class CliOvernight(CliHarness):
    def test_cli_blocks_entries_with_day_stop_only(self):
        fake = FakeAlpaca(positions=[position("XYZ", 2, 20)], orders_open=[stop("XYZ", "day", qty="2")])
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "1"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("overnight (GTC) stop protection: XYZ", text)

    def test_cli_allows_entries_when_gtc_protected(self):
        fake = FakeAlpaca(positions=[position("XYZ", 2, 20)], orders_open=[stop("XYZ", "gtc", "trailing_stop", "2")])
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "1"], fake)
        self.assertEqual(code, cli.EXIT_OK, text)

    def test_cli_rejects_notional_buy(self):
        code, text = self.run_cli(["check-buy", "AAPL", "--notional", "40"], FakeAlpaca())
        self.assertIn('"gtc_protectable": false', text)

    def test_daily_summary_reconcile_alerts_fractional_overnight(self):
        fake = FakeAlpaca(positions=[position("FRC", 0.4, 100)], orders_open=[stop("FRC", "day", qty="0.4")])
        fake.state["clock"] = {**fake.state["clock"], "is_open": False}
        code, text = self.run_cli(["reconcile", "--workflow", "daily-summary"], fake)
        self.assertEqual(json.loads(text)["overnight_unprotected"], ["FRC"])
        self.assertTrue(any("no overnight (GTC) protection" in a for a in self.alerts))

    def test_protect_whole_shares_places_gtc(self):
        fake = FakeAlpaca(positions=[position("XYZ", 2, 20)])
        code, text = self.run_cli(["protect", "XYZ", "--trail-pct", "10"], fake)
        order = json.loads(text)["order"]
        self.assertEqual((order["type"], order["time_in_force"]), ("trailing_stop", "gtc"))

    def test_midday_flags_fractional_positions(self):
        fake = FakeAlpaca(positions=[position("FRC", 0.4, 100)])
        _, text = self.run_cli(["midday-scan"], fake)
        actions = {a["action"] for a in json.loads(text)["actions"]}
        self.assertIn("FRACTIONAL_NO_OVERNIGHT_PROTECTION", actions)


if __name__ == "__main__":
    unittest.main()
