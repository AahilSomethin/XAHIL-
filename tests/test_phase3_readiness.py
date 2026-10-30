"""Phase 3 readiness: stale data, market closures, day trades, same-day round trips, cash-only,
fractional limits, protection gaps, uncertain responses, duplicate fills, interrupted runs,
reconciliation recovery and account resets. All offline against a fake Alpaca."""

import json
import unittest
from unittest import mock

from tests.helpers import CLOCK_TS, FakeAlpaca, account, asset, fill, limits, position, quote
from tests.test_cli_execution import APPROVED, CliHarness
from xahil import cli, journal, metrics, reconcile
from xahil.risk import (
    OrderRequest,
    RiskContext,
    count_day_trades,
    evaluate_order,
    parse_ts,
    protection_gaps,
    quote_age_problem,
)

NOW = parse_ts(CLOCK_TS)  # 2026-10-28 14:00:00Z


def ctx(**kw):
    base = dict(account=account(), positions=[], open_orders=[], week_orders=[], limits=limits(),
                asset=asset(), quote=quote(), research_documented=True, now=NOW, market_open=True,
                daytrade_count=0, same_day_buy_symbols=set(), require_whole_shares=False)
    base.update(kw)
    return RiskContext(**base)


def failed(v):
    return {k for k, ok in v.checks.items() if not ok}


def buy(**kw):
    return OrderRequest("buy", kw.pop("symbol", "AAPL"), notional=kw.pop("notional", 40.0), **kw)


# --- 1. quote freshness ------------------------------------------------------------

class QuoteFreshness(unittest.TestCase):
    def test_fresh_quote_ok(self):
        self.assertTrue(evaluate_order(buy(), ctx()).approved)

    def test_stale_missing_bad_future_quotes_rejected(self):
        cases = {
            "stale 61s": quote(t="2026-10-28T13:58:59Z"),
            "hours old (after-hours)": quote(t="2026-10-27T20:00:00.008716059Z"),
            "missing timestamp": quote(t=None),
            "garbage timestamp": quote(t="yesterday"),
            "naive timestamp": quote(t="2026-10-28T13:59:59"),
            "future": quote(t="2026-10-28T14:01:00Z"),
        }
        for name, q in cases.items():
            with self.subTest(name):
                self.assertIn("quote_fresh", failed(evaluate_order(buy(), ctx(quote=q))))

    def test_missing_broker_clock_rejected(self):
        self.assertIn("quote_fresh", failed(evaluate_order(buy(), ctx(now=None))))

    def test_limit_is_configurable_but_capped(self):
        q = quote(t="2026-10-28T13:58:30Z")  # 90 s old
        self.assertIn("quote_fresh", failed(evaluate_order(buy(), ctx(quote=q))))
        self.assertTrue(evaluate_order(buy(), ctx(quote=q, limits=limits(max_quote_age_seconds=120))).approved)
        from xahil.config import ConfigError
        with self.assertRaises(ConfigError):
            limits(max_quote_age_seconds=301).validate()

    def test_nanosecond_timestamps_parse(self):
        self.assertIsNone(quote_age_problem({"t": "2026-10-28T13:59:59.123456789Z"}, NOW, 60))


# --- 2. market closures --------------------------------------------------------------

class MarketClosed(unittest.TestCase):
    def test_closed_or_unknown_market_blocks_entries(self):
        for state in (False, None):
            with self.subTest(state=state):
                self.assertIn("market_open", failed(evaluate_order(buy(), ctx(market_open=state))))


# --- 3. day trades and same-day round trips ------------------------------------------

class DayTrades(unittest.TestCase):
    def test_unknown_daytrade_count_fails_closed(self):
        self.assertIn("daytrade_info", failed(evaluate_order(buy(), ctx(daytrade_count=None))))

    def test_count_from_fills(self):
        fills = [
            fill("AAA", "buy", 1, 10, "2026-10-27T14:00:00Z"), fill("AAA", "sell", 1, 11, "2026-10-27T15:00:00Z"),
            fill("BBB", "sell", 1, 10, "2026-10-27T14:00:00Z"), fill("BBB", "buy", 1, 10, "2026-10-27T15:00:00Z"),
            fill("CCC", "buy", 1, 10, "2026-10-26T14:00:00Z"), fill("CCC", "sell", 1, 10, "2026-10-27T14:00:00Z"),
        ]
        self.assertEqual(count_day_trades(fills), 1)  # only AAA: buy then sell, same ET day
        self.assertEqual(count_day_trades(fills + fills), 1)  # duplicates ignored

    def test_et_date_boundary(self):
        # 02:30Z on the 28th is 22:30 ET on the 27th: same ET day as a 27th afternoon buy.
        fills = [fill("AAA", "buy", 1, 10, "2026-10-27T19:00:00Z"), fill("AAA", "sell", 1, 10, "2026-10-28T02:30:00Z")]
        self.assertEqual(count_day_trades(fills), 1)

    def test_resolve_sources(self):
        self.assertEqual(cli.resolve_daytrades({"daytrade_count": None}, []), (0, "computed_from_fills"))
        self.assertEqual(cli.resolve_daytrades({"daytrade_count": None}, None), (None, "unavailable"))
        self.assertEqual(cli.resolve_daytrades({"daytrade_count": 1}, None), (1, "broker"))
        trades = [fill("A", "buy", 1, 1, "2026-10-27T14:00:00Z"), fill("A", "sell", 1, 1, "2026-10-27T15:00:00Z"),
                  fill("B", "buy", 1, 1, "2026-10-27T14:00:00Z"), fill("B", "sell", 1, 1, "2026-10-27T15:00:00Z")]
        self.assertEqual(cli.resolve_daytrades({"daytrade_count": 1}, trades), (2, "broker+fills"))


class SameDayRoundTrips(unittest.TestCase):
    held = [position("AAPL", 0.4, 100, entry=100, plpc=-0.01)]

    def sell(self, purpose, **kw):
        return OrderRequest("sell", "AAPL", qty=0.4, purpose=purpose, **kw)

    def test_discretionary_same_day_sell_blocked(self):
        v = evaluate_order(self.sell("discretionary"), ctx(positions=self.held, same_day_buy_symbols={"AAPL"}))
        self.assertIn("no_same_day_round_trip", failed(v))

    def test_unknown_same_day_status_blocks_discretionary(self):
        v = evaluate_order(self.sell("discretionary"), ctx(positions=self.held, same_day_buy_symbols=None))
        self.assertIn("no_same_day_round_trip", failed(v))

    def test_discretionary_sell_of_older_position_ok(self):
        self.assertTrue(evaluate_order(self.sell("discretionary"), ctx(positions=self.held)).approved)

    def test_protective_stop_always_allowed_same_day(self):
        stop = OrderRequest("sell", "AAPL", type="stop", qty=0.4, stop_price=90, purpose="protective")
        for known in ({"AAPL"}, None):
            with self.subTest(known=known):
                self.assertTrue(evaluate_order(stop, ctx(positions=self.held, same_day_buy_symbols=known)).approved)

    def test_verified_risk_exit_allowed_same_day(self):
        losing = [position("AAPL", 0.4, 92, entry=100, plpc=-0.08)]
        v = evaluate_order(self.sell("risk_exit"), ctx(positions=losing, same_day_buy_symbols={"AAPL"}))
        self.assertTrue(v.approved, v.reasons)

    def test_risk_exit_claim_is_verified(self):
        v = evaluate_order(self.sell("risk_exit"), ctx(positions=self.held, same_day_buy_symbols=set()))
        self.assertIn("risk_exit_verified", failed(v))

    def test_purpose_must_match_order_type(self):
        bad_stop = OrderRequest("sell", "AAPL", type="stop", qty=0.4, stop_price=90, purpose="discretionary")
        self.assertIn("protective_purpose", failed(evaluate_order(bad_stop, ctx(positions=self.held))))
        bad_mkt = self.sell("protective")
        self.assertIn("exit_purpose", failed(evaluate_order(bad_mkt, ctx(positions=self.held))))
        self.assertIn("purpose", failed(evaluate_order(self.sell("entry"), ctx(positions=self.held))))


# --- 4. cash only, long only, regardless of margin ------------------------------------

class CashOnly(unittest.TestCase):
    def test_margin_buying_power_never_used(self):
        acct = account(cash="30", buying_power="4000", regt_buying_power="2000", multiplier="4")
        self.assertIn("cash_only", failed(evaluate_order(buy(), ctx(account=acct))))

    def test_uses_smaller_of_cash_and_non_marginable_bp(self):
        acct = account(cash="1000", non_marginable_buying_power="35")
        self.assertIn("cash_only", failed(evaluate_order(buy(), ctx(account=acct))))

    def test_short_or_leveraged_account_state_halts(self):
        for acct in (account(short_market_value="-50"), account(long_market_value="1500"),
                     account(pattern_day_trader=True)):
            with self.subTest(acct=acct):
                self.assertIn("no_halt", failed(evaluate_order(buy(), ctx(account=acct))))

    def test_sell_never_creates_short(self):
        v = evaluate_order(OrderRequest("sell", "AAPL", qty=1, purpose="discretionary"), ctx())
        self.assertIn("long_position_exists", failed(v))


# --- 5. fractional limitations --------------------------------------------------------

class Fractional(unittest.TestCase):
    def test_non_fractionable_asset_needs_whole_shares(self):
        a = asset(fractionable=False)
        self.assertIn("fractionable", failed(evaluate_order(buy(), ctx(asset=a))))
        whole = OrderRequest("buy", "AAPL", qty=1, type="limit", limit_price=40)
        v = evaluate_order(whole, ctx(asset=a, quote=quote(40, 39.99)))
        self.assertNotIn("fractionable", failed(v))

    def test_protective_order_selection_matches_alpaca_support(self):
        frac = cli.protective_order(position("AAPL", 0.4, 100), 10, None, None)
        self.assertEqual((frac[0].type, frac[0].time_in_force), ("stop", "day"))
        self.assertIn("DAY stops only", frac[2])
        whole_trail = cli.protective_order(position("AAPL", 2, 20), 10, None, None)
        self.assertEqual((whole_trail[0].type, whole_trail[0].time_in_force), ("trailing_stop", "gtc"))
        whole_stop = cli.protective_order(position("AAPL", 2, 20), None, 10, None)
        self.assertEqual((whole_stop[0].type, whole_stop[0].time_in_force), ("stop", "gtc"))

    def test_engine_rejects_unsupported_fractional_orders(self):
        held = [position("AAPL", 2.5, 20)]
        gtc = OrderRequest("sell", "AAPL", type="stop", time_in_force="gtc", qty=2.5, stop_price=18, purpose="protective")
        self.assertIn("fractional_tif", failed(evaluate_order(gtc, ctx(positions=held))))
        # whole qty out of a fractional holding is still a fractional position at Alpaca
        trail = OrderRequest("sell", "AAPL", type="trailing_stop", time_in_force="gtc", qty=2, trail_percent=10,
                             purpose="protective")
        self.assertIn("trail_whole_shares", failed(evaluate_order(trail, ctx(positions=held))))


# --- 6. protection gaps (pure) ---------------------------------------------------------

class ProtectionGaps(unittest.TestCase):
    def test_gap_detection_and_blocking(self):
        held = [position("AAA", 0.3, 100), position("BBB", 0.3, 100)]
        stops = [{"id": "s", "symbol": "AAA", "side": "sell", "type": "stop", "qty": "0.3"}]
        self.assertEqual(protection_gaps(held, stops), ["BBB"])
        v = evaluate_order(buy(symbol="CCC", notional=10), ctx(positions=held, protection_gaps=["BBB"],
                                                                asset=asset("CCC")))
        self.assertIn("positions_protected", failed(v))


class DuplicateFillsInMetrics(unittest.TestCase):
    def test_same_fill_twice_counts_once(self):
        b = fill("AAA", "buy", 1, 10, "2026-10-26T14:00:00Z", order_id="b1", fid="f1")
        s = fill("AAA", "sell", 1, 12, "2026-10-27T14:00:00Z", order_id="s1", fid="f2")
        trades = metrics.closed_trades_from_fills([b, b, s, s])
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].pnl, 2.0)
        self.assertAlmostEqual(trades[0].qty, 1.0)


# --- CLI-level scenarios --------------------------------------------------------------

def execute_mode():
    return mock.patch.object(cli, "load_execution_settings", lambda: APPROVED), \
        mock.patch.dict("os.environ", {"XAHIL_ORDER_EXECUTION": "enabled"})


class CliScenarios(CliHarness):
    def run_exec(self, argv, fake):
        a, b = execute_mode()
        with a, b:
            return self.run_cli(argv, fake)

    def journal_statuses(self):
        return [r.get("status") for r in journal.read(self.tmp / "executed.jsonl")]

    # stale data and closures
    def test_stale_quote_blocks_check_buy(self):
        fake = FakeAlpaca(quote=quote(t="2026-10-28T13:55:00Z"))
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "2"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("quote is 300s old", text)

    def test_market_closed_blocks_check_buy(self):
        fake = FakeAlpaca()
        fake.state["clock"] = {**fake.state["clock"], "is_open": False}
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "2"], fake)
        self.assertIn('"market_open": false', text)

    def test_holiday_market_status(self):
        fake = FakeAlpaca(calendar=[{"date": "2026-10-29", "open": "09:30", "close": "16:00"}])
        code, text = self.run_cli(["market-status"], fake)
        self.assertFalse(json.loads(text)["trading_day"])

    def test_half_day_detected(self):
        fake = FakeAlpaca(calendar=[{"date": "2026-10-28", "open": "09:30", "close": "13:00"}])
        _, text = self.run_cli(["market-status"], fake)
        self.assertTrue(json.loads(text)["early_close"])

    # day-trade information unavailable
    def test_fills_unavailable_blocks_entries_but_not_risk_exits(self):
        # Real paper account returns daytrade_count: null (Phase 2); with fills also unavailable -> unknown.
        fake = FakeAlpaca(fills_error=True, account=account(daytrade_count=None),
                          positions=[position("AAPL", 0.4, 92, entry=100, plpc=-0.08)])
        code, text = self.run_cli(["check-buy", "MSFT", "--qty", "1"], fake)
        self.assertIn("day-trade count unavailable", text)
        code, text = self.run_cli(["sell", "AAPL", "--all", "--reason", "thesis"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("same-day round trip blocked", text)
        code, text = self.run_cli(["sell", "AAPL", "--all", "--risk-exit", "--reason", "cut"], fake)
        self.assertEqual(code, cli.EXIT_OK, text)
        self.assertEqual(json.loads(text)["status"], "dry_run_proposal")

    def test_same_day_buy_from_fills_blocks_discretionary_sell(self):
        fake = FakeAlpaca(positions=[position("AAPL", 0.4, 100)],
                          fills=[fill("AAPL", "buy", 0.4, 100, "2026-10-28T13:50:00Z")])
        code, text = self.run_cli(["sell", "AAPL", "--all", "--reason", "changed my mind"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("AAPL was bought today", text)

    # cash only through the CLI
    def test_margin_account_cannot_buy_beyond_cash(self):
        fake = FakeAlpaca(account=account(cash="20", buying_power="4000", multiplier="4"))
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "2"], fake)
        self.assertIn('"cash_only": false', text)

    # protection gaps
    def test_unprotected_position_blocks_entry_and_alerts(self):
        fake = FakeAlpaca(positions=[position("MSFT", 0.1, 100)])
        code, text = self.run_cli(["buy", "AAPL", "--qty", "1", "--reason", "event"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("positions without overnight (GTC) stop protection: MSFT", text)
        self.assertTrue(any("no protective stop" in a for a in self.alerts))

    def test_midday_reconcile_alerts_on_gap(self):
        fake = FakeAlpaca(positions=[position("MSFT", 0.1, 100)])
        code, text = self.run_cli(["reconcile", "--workflow", "midday"], fake)
        self.assertEqual(json.loads(text)["unprotected_positions"], ["MSFT"])
        self.assertTrue(any("without a protective stop" in a for a in self.alerts))

    def test_market_open_reconcile_does_not_alert_expected_gaps(self):
        fake = FakeAlpaca(positions=[position("MSFT", 0.1, 100)])
        self.run_cli(["reconcile", "--workflow", "market-open"], fake)
        self.assertEqual(self.alerts, [])

    def test_broker_rejected_protective_order_alerts(self):
        fake = FakeAlpaca(positions=[position("AAPL", 0.4, 100)])
        fake.post_behaviour = ("http", 403, '{"message": "insufficient qty"}')
        code, text = self.run_exec(["protect", "AAPL", "--stop-pct", "10"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertIn("rejected_by_broker", self.journal_statuses())
        self.assertTrue(any("may be unprotected" in a for a in self.alerts))

    def test_async_broker_rejection_of_stop_alerts(self):
        fake = FakeAlpaca(positions=[position("AAPL", 0.4, 100)])
        fake.post_behaviour = "broker_rejects"
        code, _ = self.run_exec(["protect", "AAPL", "--stop-pct", "10"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertTrue(self.alerts)

    def test_failed_cancel_stops_and_alerts(self):
        fake = FakeAlpaca(positions=[position("AAPL", 0.4, 92, entry=100, plpc=-0.08)],
                          orders_open=[{"id": "stop-1", "symbol": "AAPL", "side": "sell", "type": "stop",
                                        "qty": "0.4", "filled_qty": "0", "stop_price": "90"}])
        fake.cancel_behaviour = (422, '{"message": "order is not cancelable"}')
        code, _ = self.run_exec(["sell", "AAPL", "--all", "--risk-exit", "--reason", "cut"], fake)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertEqual([c for c in fake.posts() if c[0] == "POST"], [])
        self.assertTrue(self.alerts)

    def test_protect_without_position_does_not_alert(self):
        code, _ = self.run_exec(["protect", "AAPL", "--stop-pct", "10"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertEqual(self.alerts, [])

    # uncertain order responses
    def test_timeout_after_broker_accepted_is_recovered(self):
        fake = FakeAlpaca()
        fake.post_behaviour = "timeout_after_accept"
        code, text = self.run_exec(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(code, cli.EXIT_OK, text)
        self.assertEqual(json.loads(text)["status"], "recovered_submitted")
        self.assertEqual(len(fake.submitted), 1)

    def test_timeout_before_accept_halts_until_reconcile(self):
        fake = FakeAlpaca()
        fake.post_behaviour = "timeout_before_accept"
        code, _ = self.run_exec(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(code, cli.EXIT_HALT)
        self.assertTrue(any("outcome unknown" in a for a in self.alerts))
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "2"], fake)
        self.assertIn("unresolved order", text)
        fake.post_behaviour = None
        code, text = self.run_cli(["reconcile", "--workflow", "midday"], fake)
        self.assertIn("not placed", text)
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "2"], fake)
        self.assertEqual(code, cli.EXIT_OK, text)

    def test_server_error_with_order_absent_is_uncertain(self):
        fake = FakeAlpaca()
        fake.post_behaviour = ("http", 503, "unavailable")
        code, _ = self.run_exec(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(code, cli.EXIT_HALT)
        self.assertIn("uncertain", self.journal_statuses())

    def test_duplicate_submission_is_idempotent(self):
        fake = FakeAlpaca()
        for _ in range(2):
            code, text = self.run_exec(["buy", "AAPL", "--qty", "2", "--reason", "event",
                                        "--workflow", "market-open"], fake)
            self.assertEqual(code, cli.EXIT_OK, text)
        self.assertEqual(len(fake.submitted), 1)
        self.assertEqual(json.loads(text)["status"], "recovered_submitted")

    def test_definitive_broker_rejection_of_entry(self):
        fake = FakeAlpaca()
        fake.post_behaviour = ("http", 403, '{"message": "insufficient buying power"}')
        code, _ = self.run_exec(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertEqual(self.alerts, [])  # entry rejection is not a protection problem

    def test_reverification_catches_state_change_before_submit(self):
        class Shifting(FakeAlpaca):
            n = 0

            def __call__(self, method, url, headers, body):
                if url.endswith("/account"):
                    Shifting.n += 1
                    if Shifting.n > 1:
                        self.state["account"] = account(cash="10")
                return super().__call__(method, url, headers, body)

        fake = Shifting()
        code, text = self.run_exec(["buy", "AAPL", "--qty", "2", "--reason", "event"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        self.assertEqual(json.loads(text)["status"], "rejected_on_reverify")
        self.assertEqual(fake.submitted, {})

    # interrupted runs and recovery
    def test_interrupted_after_submit_is_recovered_by_reconcile(self):
        fake = FakeAlpaca()
        coid = "xahil-2026-10-28-market-open-AAPL-buy-market"
        journal.append(self.tmp / "executed.jsonl", {"client_order_id": coid, "status": "submitting"})
        fake.submitted[coid] = {"id": "abc", "status": "filled", "client_order_id": coid}
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "2"], fake)
        self.assertIn("unresolved order", text)
        code, text = self.run_cli(["reconcile", "--workflow", "midday"], fake)
        self.assertIn("journal recovered", text)
        self.assertEqual(journal.unresolved(self.tmp / "executed.jsonl"), [])

    def test_unlogged_broker_orders_recovered_and_foreign_flagged(self):
        coid = "xahil-2026-10-28-market-open-AAPL-buy-market"
        fake = FakeAlpaca(orders_all=[
            {"id": "o1", "client_order_id": coid, "symbol": "AAPL", "side": "buy", "type": "market", "status": "filled",
             "qty": "0.4", "filled_qty": "0.4"},
            {"id": "o2", "client_order_id": "web-123", "symbol": "TSLA", "side": "buy", "type": "market",
             "status": "canceled", "filled_qty": "0"}])
        code, text = self.run_cli(["reconcile", "--workflow", "midday"], fake)
        data = json.loads(text)
        self.assertTrue(any("missing from journal; recovered" in j for j in data["journal"]))
        self.assertTrue(any("not placed by XAHIL" in d for d in data["differences"]))
        self.assertIn(coid, journal.known_coids(self.tmp / "executed.jsonl"))

    def test_interrupted_between_cancel_and_sell_is_caught(self):
        # Previous run cancelled the stop, then died before selling: the position is now unprotected.
        fake = FakeAlpaca(positions=[position("AAPL", 0.4, 100)])
        self.run_cli(["reconcile", "--workflow", "midday"], fake)
        self.assertTrue(self.alerts)
        code, text = self.run_cli(["check-buy", "MSFT", "--qty", "1"], fake)
        self.assertIn('"positions_protected": false', text)

    def test_corrupt_journal_halts(self):
        (self.tmp / "executed.jsonl").write_text("{not json\n", encoding="utf-8")
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "2"], FakeAlpaca())
        self.assertIn("corrupt", text)

    def test_snapshot_write_is_atomic(self):
        path = self.tmp / "snap.json"
        reconcile.save_snapshot(path, {"a": 1})
        self.assertFalse(path.with_suffix(".tmp").exists())
        self.assertEqual(json.loads(path.read_text())["a"], 1)

    # account resets and unexplained changes
    def write_prev(self, equity, positions=(), number="PA1234567"):
        snap = reconcile.build_snapshot("2026-10-27T16:00:00-04:00", "daily-summary",
                                        account(equity=str(equity), cash=str(equity), account_number=number),
                                        list(positions), [])
        reconcile.save_snapshot(self.tmp / "snap.json", snap)

    def test_account_reset_detected_and_requires_human_rebaseline(self):
        self.write_prev(100000)
        fake = FakeAlpaca()  # now $1,000, no positions, no fills
        code, text = self.run_cli(["reconcile", "--workflow", "pre-market"], fake)
        self.assertEqual(code, cli.EXIT_HALT)
        self.assertIn("account reset, deposit or withdrawal", text)
        self.assertTrue(any("new halt" in a for a in self.alerts))
        # halt persists across runs
        code, _ = self.run_cli(["reconcile", "--workflow", "midday"], fake)
        self.assertEqual(code, cli.EXIT_HALT)
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "2"], fake)
        self.assertIn('"no_halt": false', text)
        code, _ = self.run_cli(["snapshot"], fake)
        self.assertEqual(code, cli.EXIT_HALT)
        # an agent (no interactive terminal) cannot clear it
        with mock.patch.object(cli.sys.stdin, "isatty", lambda: False):
            code, _ = self.run_cli(["ack-halt", "--all"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        with mock.patch.object(cli.sys.stdin, "isatty", lambda: False):
            code, _ = self.run_cli(["reset-baseline"], fake)
        self.assertEqual(code, cli.EXIT_REJECTED)
        # the human re-baselines in a local terminal
        (self.tmp / "hist.csv").write_text("date,equity,cash,long_value,spy_close\n2026-10-27,100000,100000,0,700\n")
        with mock.patch.object(cli.sys.stdin, "isatty", lambda: True), mock.patch("builtins.input", lambda *_: "RESET"):
            code, text = self.run_cli(["reset-baseline"], fake)
        self.assertEqual(code, cli.EXIT_OK, text)
        self.assertFalse((self.tmp / "hist.csv").exists())
        self.assertTrue(list(self.tmp.glob("hist.archived-*.csv")))
        code, text = self.run_cli(["check-buy", "AAPL", "--qty", "2"], fake)
        self.assertEqual(code, cli.EXIT_OK, text)

    def test_reset_baseline_refused_in_cloud(self):
        with mock.patch.object(cli.sys.stdin, "isatty", lambda: True), mock.patch("builtins.input", lambda *_: "RESET"), \
                mock.patch.dict("os.environ", {"XAHIL_CLOUD": "1"}):
            code, _ = self.run_cli(["reset-baseline"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_REJECTED)

    def test_account_number_change_keeps_old_snapshot(self):
        self.write_prev(1000, number="PA0000001")
        code, _ = self.run_cli(["reconcile", "--workflow", "pre-market"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_HALT)
        self.assertEqual(json.loads((self.tmp / "snap.json").read_text())["account_number"], "PA0000001")
        self.assertIn("account-changed", (self.tmp / "halts.json").read_text())

    def test_position_vanished_without_fill_halts(self):
        self.write_prev(1000, positions=[position("AAPL", 0.4, 100)])
        code, text = self.run_cli(["reconcile", "--workflow", "midday"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_HALT)
        self.assertIn("AAPL: qty 0.4 -> 0", text)

    def test_position_closed_by_stop_fill_is_explained(self):
        self.write_prev(1000, positions=[position("AAPL", 0.4, 100)])
        fake = FakeAlpaca(fills=[fill("AAPL", "sell", 0.4, 92, "2026-10-28T13:45:00Z")])
        code, text = self.run_cli(["reconcile", "--workflow", "midday"], fake)
        self.assertEqual(code, cli.EXIT_OK, text)
        self.assertEqual(json.loads(text)["unexplained"], [])

    def test_ack_halt_by_human(self):
        journal.add_halt(self.tmp / "halts.json", "unexplained-x", "test")
        with mock.patch.object(cli.sys.stdin, "isatty", lambda: True), mock.patch("builtins.input", lambda *_: "ACK"):
            code, text = self.run_cli(["ack-halt", "--all"], FakeAlpaca())
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(journal.load_halts(self.tmp / "halts.json"), [])

    # switches stay off
    def test_repo_switches_still_disabled(self):
        from xahil.config import load_execution_settings
        self.assertFalse(load_execution_settings().order_execution_enabled)
        mode, why = cli.execution_mode({})
        self.assertEqual(mode, "dry-run")


if __name__ == "__main__":
    unittest.main()
