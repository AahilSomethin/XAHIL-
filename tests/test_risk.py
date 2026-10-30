"""Requirement 8: deterministic risk engine."""

import unittest

from tests.helpers import account, asset, limits, position, quote
from xahil.risk import (
    OrderRequest,
    RiskContext,
    check_stop_not_lowered,
    count_week_buys,
    evaluate_order,
)


def ctx(**kw):
    base = dict(account=account(), positions=[], open_orders=[], week_orders=[], limits=limits(),
                asset=asset(), quote=quote(), research_documented=True)
    base.update(kw)
    return RiskContext(**base)


def buy(symbol="AAPL", notional=40.0, **kw):
    return OrderRequest(side="buy", symbol=symbol, notional=notional, **kw)


def failed(verdict):
    return {k for k, v in verdict.checks.items() if not v}


class BuyRules(unittest.TestCase):
    def test_clean_small_buy_approved(self):
        v = evaluate_order(buy(notional=45), ctx())
        self.assertTrue(v.approved, v.reasons)

    def test_position_cap_5_percent(self):
        self.assertTrue(evaluate_order(buy(notional=50), ctx()).approved)
        v = evaluate_order(buy(notional=50.01), ctx())
        self.assertIn("position_cap", failed(v))

    def test_position_cap_counts_existing_holding(self):
        v = evaluate_order(buy(notional=20), ctx(positions=[position("AAPL", 0.4, 100)]))  # $40 held
        self.assertIn("position_cap", failed(v))

    def test_qty_cost_includes_price_buffer(self):
        # 0.49 sh * $100 * 1.02 = $49.98 ok; 0.5 sh -> $51 > $50
        self.assertTrue(evaluate_order(OrderRequest("buy", "AAPL", qty=0.49), ctx()).approved)
        self.assertIn("position_cap", failed(evaluate_order(OrderRequest("buy", "AAPL", qty=0.5), ctx())))

    def test_total_exposure_cap_20_percent(self):
        held = [position(s, 0.45, 100) for s in ("MSFT", "NVDA", "XOM")]  # $135
        v = evaluate_order(buy(notional=50), ctx(positions=held))  # -> 185 ok
        self.assertTrue(v.approved, v.reasons)
        held.append(position("JPM", 0.2, 100))  # $155 total -> +50 = 205 > 200
        v = evaluate_order(buy(notional=50), ctx(positions=held, limits=limits(max_open_positions=5)))
        self.assertIn("exposure_cap", failed(v))

    def test_exposure_includes_pending_buy_orders(self):
        pending = [{"id": "o1", "side": "buy", "symbol": "MSFT", "notional": "180", "status": "new"}]
        v = evaluate_order(buy(notional=30), ctx(open_orders=pending))
        self.assertIn("exposure_cap", failed(v))

    def test_unpriceable_open_buy_fails_closed(self):
        pending = [{"id": "o1", "side": "buy", "symbol": "ZZZ", "qty": "1", "type": "market", "status": "new"}]
        self.assertIn("open_orders_priced", failed(evaluate_order(buy(), ctx(open_orders=pending))))

    def test_weekly_trade_cap(self):
        week = [{"id": f"b{i}", "side": "buy", "status": "filled", "filled_qty": "1"} for i in range(3)]
        self.assertIn("weekly_trades", failed(evaluate_order(buy(), ctx(week_orders=week))))
        self.assertTrue(evaluate_order(buy(), ctx(week_orders=week[:2])).approved)

    def test_canceled_unfilled_buys_do_not_count_but_partials_do(self):
        week = [{"id": "a", "side": "buy", "status": "canceled", "filled_qty": "0"},
                {"id": "b", "side": "buy", "status": "canceled", "filled_qty": "0.3"},
                {"id": "c", "side": "buy", "status": "new", "filled_qty": "0"},
                {"id": "d", "side": "sell", "status": "filled", "filled_qty": "1"}]
        self.assertEqual(count_week_buys(week), 2)

    def test_cash_only_ignores_buying_power(self):
        v = evaluate_order(buy(notional=40), ctx(account=account(cash="30", buying_power="5000")))
        self.assertIn("cash_only", failed(v))

    def test_negative_cash_halts(self):
        v = evaluate_order(buy(notional=10), ctx(account=account(cash="-5")))
        self.assertIn("no_halt", failed(v))

    def test_short_position_halts_buys(self):
        v = evaluate_order(buy(), ctx(positions=[position("TSLA", -1, 10, side="short")]))
        self.assertIn("no_halt", failed(v))

    def test_default_100k_paper_balance_blocks_buys(self):
        v = evaluate_order(buy(notional=40), ctx(account=account(equity="100000", cash="100000")))
        self.assertIn("no_halt", failed(v))
        self.assertTrue(any("reset the paper account" in r for r in v.reasons))

    def test_non_paper_account_halts(self):
        self.assertIn("no_halt", failed(evaluate_order(buy(), ctx(account=account(account_number="9XYZ")))))

    def test_external_halt_reason(self):
        self.assertIn("no_halt", failed(evaluate_order(buy(), ctx(halt_reasons=["kill switch"]))))

    def test_derivatives_crypto_otc_blocked(self):
        cases = {
            "option class": asset(**{"class": "us_option"}),
            "crypto": asset("BTCUSD", **{"class": "crypto", "exchange": "CRYPTO"}),
            "otc": asset(exchange="OTC"),
            "inactive": asset(status="inactive"),
            "untradable": asset(tradable=False),
        }
        for name, a in cases.items():
            with self.subTest(name):
                self.assertIn("asset_allowed", failed(evaluate_order(buy(symbol=a["symbol"]), ctx(asset=a))))

    def test_option_style_symbol_blocked(self):
        v = evaluate_order(buy(symbol="AAPL261218C00200000"), ctx(asset=asset("AAPL261218C00200000")))
        self.assertIn("asset_allowed", failed(v))

    def test_leveraged_and_inverse_etfs_blocked(self):
        self.assertIn("asset_allowed", failed(evaluate_order(buy(symbol="TQQQ"), ctx(asset=asset("TQQQ")))))
        for name in ("Direxion Daily Semiconductor Bull 3X Shares", "ProShares UltraPro QQQ",
                     "ProShares Short S&P500", "AXS 1.5X NVDA Bull Daily ETF", "Tradr 2X Long TSLA",
                     "iPath S&P 500 VIX Short-Term Futures ETN", "Direxion Daily S&P 500 Bear -1X"):
            with self.subTest(name):
                v = evaluate_order(buy(symbol="NEWX"), ctx(asset=asset("NEWX", name=name)))
                self.assertIn("asset_allowed", failed(v))

    def test_normal_etfs_not_blocked_by_name_filter(self):
        for name in ("SPDR S&P 500 ETF Trust", "Vanguard Short-Term Bond ETF",
                     "Invesco S&P 500 Low Volatility ETF", "iShares 1-3 Year Treasury Bond ETF"):
            with self.subTest(name):
                self.assertTrue(evaluate_order(buy(symbol="ETF"), ctx(asset=asset("ETF", name=name))).approved)

    def test_quote_quality(self):
        self.assertIn("quote_valid", failed(evaluate_order(buy(), ctx(quote=quote(0, 0)))))
        self.assertIn("spread", failed(evaluate_order(buy(), ctx(quote=quote(100, 97)))))
        self.assertIn("min_price", failed(evaluate_order(buy(), ctx(quote=quote(0.5, 0.499)))))

    def test_research_required(self):
        self.assertIn("research_documented", failed(evaluate_order(buy(), ctx(research_documented=False))))

    def test_max_positions(self):
        held = [position(s, 0.1, 100) for s in ("A", "B", "C", "D")]
        self.assertIn("max_positions", failed(evaluate_order(buy(notional=10), ctx(positions=held))))

    def test_order_shape_rules(self):
        self.assertIn("order_type", failed(evaluate_order(buy(type="stop"), ctx())))
        self.assertIn("time_in_force", failed(evaluate_order(buy(time_in_force="gtc"), ctx())))
        both = OrderRequest("buy", "AAPL", qty=0.1, notional=10)
        self.assertIn("size_given", failed(evaluate_order(both, ctx())))
        self.assertIn("notional_market_only", failed(evaluate_order(buy(type="limit", limit_price=100), ctx())))

    def test_pdt_room(self):
        self.assertIn("pdt_room", failed(evaluate_order(buy(), ctx(account=account(daytrade_count=3)))))

    def test_unknown_side_rejected(self):
        self.assertFalse(evaluate_order(OrderRequest("sell_short", "AAPL", qty=1), ctx()).approved)


class SellRules(unittest.TestCase):
    def test_sell_held_qty_ok(self):
        v = evaluate_order(OrderRequest("sell", "AAPL", qty=0.4), ctx(positions=[position("AAPL", 0.4, 100)]))
        self.assertTrue(v.approved, v.reasons)

    def test_cannot_sell_what_you_do_not_hold(self):
        v = evaluate_order(OrderRequest("sell", "AAPL", qty=1), ctx())
        self.assertIn("long_position_exists", failed(v))

    def test_oversell_rejected(self):
        v = evaluate_order(OrderRequest("sell", "AAPL", qty=0.5), ctx(positions=[position("AAPL", 0.4, 100)]))
        self.assertIn("no_oversell", failed(v))

    def test_open_stop_reserves_shares(self):
        stop = [{"id": "s", "side": "sell", "symbol": "AAPL", "qty": "0.4", "filled_qty": "0", "type": "stop"}]
        v = evaluate_order(OrderRequest("sell", "AAPL", qty=0.4), ctx(positions=[position("AAPL", 0.4, 100)], open_orders=stop))
        self.assertIn("no_oversell", failed(v))

    def test_trailing_stop_needs_whole_shares(self):
        o = OrderRequest("sell", "AAPL", type="trailing_stop", time_in_force="gtc", qty=0.4, trail_percent=10)
        self.assertIn("trail_whole_shares", failed(evaluate_order(o, ctx(positions=[position("AAPL", 0.4, 100)]))))
        o2 = OrderRequest("sell", "AAPL", type="trailing_stop", time_in_force="gtc", qty=2, trail_percent=10)
        self.assertTrue(evaluate_order(o2, ctx(positions=[position("AAPL", 2, 20)])).approved)

    def test_trail_percent_whitelist(self):
        o = OrderRequest("sell", "AAPL", type="trailing_stop", time_in_force="gtc", qty=2, trail_percent=1)
        self.assertIn("trail_percent", failed(evaluate_order(o, ctx(positions=[position("AAPL", 2, 20)]))))

    def test_fractional_stop_must_be_day(self):
        o = OrderRequest("sell", "AAPL", type="stop", time_in_force="gtc", qty=0.4, stop_price=90)
        self.assertIn("fractional_tif", failed(evaluate_order(o, ctx(positions=[position("AAPL", 0.4, 100)]))))

    def test_stop_must_be_below_price(self):
        o = OrderRequest("sell", "AAPL", type="stop", qty=0.4, stop_price=101)
        self.assertIn("stop_below_price", failed(evaluate_order(o, ctx(positions=[position("AAPL", 0.4, 100)]))))

    def test_notional_sell_rejected(self):
        o = OrderRequest("sell", "AAPL", notional=10)
        self.assertIn("qty_only", failed(evaluate_order(o, ctx(positions=[position("AAPL", 0.4, 100)]))))

    def test_stop_never_lowered(self):
        self.assertIsNotNone(check_stop_not_lowered(95.0, 90.0))
        self.assertIsNone(check_stop_not_lowered(90.0, 95.0))
        self.assertIsNone(check_stop_not_lowered(None, 80.0))


if __name__ == "__main__":
    unittest.main()
