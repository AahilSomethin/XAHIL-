"""Deterministic risk engine.

Pure functions over broker state. No network, no LLM. Every order the agent
wants to place must get an approved Verdict from `evaluate_order` first; the
CLI is the only code path that submits orders and it always calls this, then
calls it again on freshly fetched state immediately before submitting.

Enforced (owner requirements):
  * paper account only, cash only (margin ignored), long only, no leverage
  * US-listed stocks/ETFs only; no options, crypto, OTC, leveraged/inverse ETFs
  * max 3 new buy orders per week (counted from the broker, not the log)
  * max 5% of equity per position, max 20% total long exposure
  * fresh quotes only, market open, day-trade information known
  * no new entries while any position lacks a protective stop or an order is unresolved
  * no discretionary same-day round trips; protective exits are always allowed
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from xahil.config import RiskLimits

SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
LEVERAGE_NAME_RE = re.compile(
    r"(\b[1-9](\.\d+)?X\b|-[1-9](\.\d+)?X\b|ULTRA|INVERSE|\bBEAR\b|\bSHORT\b(?![- ]?(TERM|DURATION|MATURITY))"
    r"|LEVERAGED|DAILY TARGET|\bVIX\b|VOLATILITY (INDEX|FUTURES)"
    # Derivative or crypto exposure inside an ETP is still a derivative or crypto position.
    r"|FUTURES|BITCOIN|\bETHER(EUM)?\b|CRYPTO|OIL FUND|GAS FUND|COMMODITY INDEX|\bDB\b|STRATEGY ETF"
    r"|BUFFER|COVERED CALL|OPTION INCOME|PREMIUM INCOME|\bDEFINED OUTCOME\b)",
    re.IGNORECASE,
)
DEAD_ORDER_STATUSES = {"canceled", "expired", "rejected", "replaced", "done_for_day"}
BUY_TYPES = {"market", "limit"}
SELL_TYPES = {"market", "limit", "stop", "trailing_stop"}
PROTECTIVE_TYPES = {"stop", "trailing_stop"}
SELL_PURPOSES = {"protective", "risk_exit", "discretionary"}
MAX_QUOTE_FUTURE_SKEW_S = 5.0


def _f(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def is_whole(qty: float) -> bool:
    return abs(qty - round(qty)) < 1e-9


def parse_ts(ts: str) -> dt.datetime:
    """Parse Alpaca timestamps (nanoseconds, Z or offset) into an aware datetime."""
    ts = str(ts).strip().replace("Z", "+00:00")
    ts = re.sub(r"(\.\d{6})\d+", r"\1", ts)
    parsed = dt.datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed


@dataclass
class OrderRequest:
    side: str
    symbol: str
    type: str = "market"
    time_in_force: str = "day"
    qty: Optional[float] = None
    notional: Optional[float] = None
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    trail_percent: Optional[float] = None
    purpose: str = "entry"  # buys: "entry"; sells: protective | risk_exit | discretionary

    @property
    def fractional(self) -> bool:
        return self.notional is not None or (self.qty is not None and not is_whole(self.qty))

    def to_alpaca(self, client_order_id: str) -> dict:
        body = {
            "symbol": self.symbol,
            "side": self.side,
            "type": self.type,
            "time_in_force": self.time_in_force,
            "client_order_id": client_order_id,
        }
        if self.qty is not None:
            body["qty"] = _fmt(self.qty)
        if self.notional is not None:
            body["notional"] = f"{self.notional:.2f}"
        if self.limit_price is not None:
            body["limit_price"] = f"{self.limit_price:.2f}"
        if self.stop_price is not None:
            body["stop_price"] = f"{self.stop_price:.2f}"
        if self.trail_percent is not None:
            body["trail_percent"] = _fmt(self.trail_percent)
        return body


def _fmt(x: float) -> str:
    return str(int(round(x))) if is_whole(x) else f"{x:.9f}".rstrip("0").rstrip(".")


@dataclass
class RiskContext:
    account: dict
    positions: list
    open_orders: list
    week_orders: list
    limits: RiskLimits
    asset: Optional[dict] = None
    quote: Optional[dict] = None
    research_documented: bool = False
    halt_reasons: list = field(default_factory=list)
    now: Optional[dt.datetime] = None          # broker clock time
    market_open: Optional[bool] = None         # broker clock is_open
    daytrade_count: Optional[int] = None       # resolved by the CLI; None = unknown -> fail closed
    same_day_buy_symbols: Optional[set] = None # None = unknown -> treat every symbol as bought today
    protection_gaps: list = field(default_factory=list)
    cut_loss_pct: float = -7.0                 # risk_exit is only allowed at or below this unrealized %
    require_whole_shares: bool = True          # entries must be able to carry a GTC stop overnight


@dataclass
class Verdict:
    approved: bool
    reasons: list
    checks: dict
    estimated_cost: float = 0.0

    def summary(self) -> str:
        head = "APPROVED" if self.approved else "REJECTED"
        return head if self.approved else head + ": " + "; ".join(self.reasons)


# --- portfolio helpers ----------------------------------------------------------

def equity_of(account: dict) -> float:
    return _f(account.get("equity"))


def long_market_value(positions: list) -> float:
    return sum(abs(_f(p.get("market_value"))) for p in positions if _f(p.get("qty")) > 0)


def position_for(positions: list, symbol: str) -> Optional[dict]:
    for p in positions:
        if p.get("symbol") == symbol:
            return p
    return None


def spendable_cash(account: dict) -> float:
    """Settled-style cash only. Margin buying power is never used.

    Uses the smaller of `cash` and `non_marginable_buying_power` (when Alpaca
    reports it), so a margin multiplier on the paper account changes nothing.
    """
    cash = _f(account.get("cash"))
    nmbp = account.get("non_marginable_buying_power")
    if nmbp not in (None, ""):
        cash = min(cash, _f(nmbp))
    return cash


def _open_order_cost(order: dict, positions: list, quote_symbol: str = None, quote_price: float = 0.0) -> Optional[float]:
    if _f(order.get("notional")) > 0:
        return _f(order.get("notional")) - _f(order.get("filled_avg_price")) * _f(order.get("filled_qty"))
    remaining = _f(order.get("qty")) - _f(order.get("filled_qty"))
    if remaining <= 0:
        return 0.0
    price = _f(order.get("limit_price")) or _f(order.get("stop_price"))
    if not price:
        pos = position_for(positions, order.get("symbol"))
        price = _f(pos.get("current_price")) if pos else 0.0
    if not price and order.get("symbol") == quote_symbol:
        price = quote_price
    if not price:
        return None
    return remaining * price


def pending_buy_cost(open_orders: list, positions: list, symbol: str = None, quote_price: float = 0.0):
    """Cash reserved by open buy orders. Returns (total, per_symbol_dict, unpriceable_ids)."""
    total, per_symbol, unknown = 0.0, {}, []
    for o in open_orders:
        if o.get("side") != "buy":
            continue
        cost = _open_order_cost(o, positions, symbol, quote_price)
        if cost is None:
            unknown.append(o.get("id", "?"))
            continue
        total += cost
        per_symbol[o.get("symbol")] = per_symbol.get(o.get("symbol"), 0.0) + cost
    return total, per_symbol, unknown


def count_week_buys(week_orders: list) -> int:
    """Buy orders this week that are live or got any fill. Adds count as trades too (conservative)."""
    n = 0
    seen = set()
    for o in week_orders:
        if o.get("side") != "buy" or o.get("id") in seen:
            continue
        seen.add(o.get("id"))
        if o.get("status") not in DEAD_ORDER_STATUSES or _f(o.get("filled_qty")) > 0:
            n += 1
    return n


def reserved_sell_qty(open_orders: list, symbol: str) -> float:
    return sum(
        _f(o.get("qty")) - _f(o.get("filled_qty"))
        for o in open_orders
        if o.get("side") == "sell" and o.get("symbol") == symbol
    )


def protection_gaps(positions: list, open_orders: list) -> list:
    """Long positions with no open protective sell stop."""
    gaps = []
    for p in positions:
        if _f(p.get("qty")) <= 0:
            continue
        if not any(o.get("symbol") == p["symbol"] and o.get("side") == "sell" and o.get("type") in PROTECTIVE_TYPES
                   for o in open_orders):
            gaps.append(p["symbol"])
    return sorted(gaps)


def overnight_protection_gaps(positions: list, open_orders: list) -> list:
    """Positions with no protection that survives the close.

    Only a GTC stop or trailing stop on a WHOLE-share position persists overnight
    at Alpaca. Fractional positions can only hold DAY stops, which expire at the
    close, so they are always reported. Note: even a GTC stop cannot prevent an
    overnight gap; a gap through the stop fills at the next open.
    """
    gaps = []
    for p in positions:
        qty = _f(p.get("qty"))
        if qty <= 0:
            continue
        if not is_whole(qty):
            gaps.append(p["symbol"])
            continue
        if not any(o.get("symbol") == p["symbol"] and o.get("side") == "sell" and o.get("type") in PROTECTIVE_TYPES
                   and o.get("time_in_force") == "gtc" for o in open_orders):
            gaps.append(p["symbol"])
    return sorted(gaps)


def account_halt_reasons(account: dict, positions: list, limits: RiskLimits) -> list:
    """Conditions that block ALL new buys until a human looks."""
    reasons = []
    if not str(account.get("account_number", "")).startswith("PA"):
        reasons.append("account is not a paper account")
    if str(account.get("status", "")).upper() != "ACTIVE":
        reasons.append(f"account status is {account.get('status')!r}, not ACTIVE")
    for flag in ("trading_blocked", "account_blocked", "trade_suspended_by_user"):
        if account.get(flag):
            reasons.append(f"account flag {flag} is set")
    equity = equity_of(account)
    cash = _f(account.get("cash"))
    if equity <= 0:
        reasons.append("equity is not positive")
    if cash < 0:
        reasons.append(f"cash is negative ({cash:.2f}): margin in use")
    if abs(_f(account.get("short_market_value"))) > 0.005:
        reasons.append("account reports short market value: short exposure exists")
    lmv = _f(account.get("long_market_value"), long_market_value(positions))
    if equity > 0 and lmv > equity * 1.001:
        reasons.append(f"long market value {lmv:,.2f} exceeds equity {equity:,.2f}: leverage in use")
    cap = limits.starting_capital * limits.max_equity_multiple_of_start
    if equity > cap:
        reasons.append(
            f"equity {equity:,.2f} exceeds {limits.max_equity_multiple_of_start:g}x configured starting capital "
            f"({limits.starting_capital:,.2f}); reset the paper account balance"
        )
    if account.get("pattern_day_trader") is True:
        reasons.append("account is flagged as a pattern day trader")
    for p in positions:
        if _f(p.get("qty")) < 0 or p.get("side") == "short":
            reasons.append(f"short position detected in {p.get('symbol')}")
        if p.get("asset_class") not in (None, "us_equity"):
            reasons.append(f"non-equity position {p.get('symbol')} ({p.get('asset_class')})")
    return reasons


def quote_age_problem(quote: Optional[dict], now: Optional[dt.datetime], max_age_s: float) -> Optional[str]:
    if now is None:
        return "broker clock time unavailable; cannot judge quote freshness"
    ts = (quote or {}).get("t")
    if not ts:
        return "quote has no timestamp"
    try:
        qt = parse_ts(ts)
    except (ValueError, TypeError):
        return f"quote timestamp {ts!r} is unparseable"
    age = (now - qt).total_seconds()
    if age < -MAX_QUOTE_FUTURE_SKEW_S:
        return f"quote timestamp is {-age:.0f}s in the future"
    if age > max_age_s:
        return f"quote is {age:.0f}s old (limit {max_age_s:g}s)"
    return None


# --- asset checks ------------------------------------------------------------

def asset_problems(symbol: str, asset: Optional[dict], limits: RiskLimits) -> list:
    problems = []
    if not SYMBOL_RE.match(symbol or ""):
        problems.append(f"symbol {symbol!r} is not a plain US stock ticker")
    if symbol in limits.denylist:
        problems.append(f"{symbol} is on the leveraged/inverse denylist")
    if not asset:
        problems.append("asset details unavailable")
        return problems
    if asset.get("class") != "us_equity":
        problems.append(f"asset class is {asset.get('class')!r}, not us_equity")
    if asset.get("status") != "active":
        problems.append("asset is not active")
    if not asset.get("tradable"):
        problems.append("asset is not tradable")
    if asset.get("exchange") not in limits.allowed_exchanges:
        problems.append(f"exchange {asset.get('exchange')!r} not allowed (OTC/crypto blocked)")
    if LEVERAGE_NAME_RE.search(str(asset.get("name", ""))):
        problems.append(f"asset name suggests leverage/inverse/volatility product: {asset.get('name')!r}")
    return problems


# --- main entry --------------------------------------------------------------

def evaluate_order(order: OrderRequest, ctx: RiskContext) -> Verdict:
    if order.side == "buy":
        return _evaluate_buy(order, ctx)
    if order.side == "sell":
        return _evaluate_sell(order, ctx)
    return Verdict(False, [f"unknown side {order.side!r} (short selling is not allowed)"], {})


def _checker():
    checks: dict = {}
    reasons: list = []

    def check(name: str, ok: bool, why: str):
        checks[name] = bool(ok)
        if not ok:
            reasons.append(why)

    return checks, reasons, check


def _evaluate_buy(order: OrderRequest, ctx: RiskContext) -> Verdict:
    lim = ctx.limits
    checks, reasons, check = _checker()

    halts = list(ctx.halt_reasons) + account_halt_reasons(ctx.account, ctx.positions, lim)
    check("no_halt", not halts, "halted: " + "; ".join(halts))
    check("market_open", ctx.market_open is True, "market is not open (broker clock)")
    check("positions_protected", not ctx.protection_gaps,
          f"positions without overnight (GTC) stop protection: {', '.join(ctx.protection_gaps)}; "
          f"no new entries until fixed")

    check("purpose", order.purpose == "entry", "buy purpose must be 'entry'")
    check("order_type", order.type in BUY_TYPES, f"buy type {order.type!r} not allowed (market/limit only)")
    check("time_in_force", order.time_in_force == "day", "buys must be time_in_force=day")
    has_qty = order.qty is not None and order.qty > 0
    has_notional = order.notional is not None and order.notional > 0
    check("size_given", has_qty != has_notional, "give exactly one of qty or notional, positive")
    if has_notional:
        check("notional_market_only", order.type == "market", "notional buys must be market orders")
    if order.type == "limit":
        check("limit_price", (order.limit_price or 0) > 0, "limit buy needs a positive limit_price")

    problems = asset_problems(order.symbol, ctx.asset, lim)
    check("asset_allowed", not problems, "; ".join(problems))
    if ctx.require_whole_shares:
        check("gtc_protectable", not order.fractional,
              "entries must be whole shares (--qty N): fractional positions cannot carry a GTC stop at Alpaca "
              "and would be unprotected overnight")
    if order.fractional:
        check("fractionable", bool((ctx.asset or {}).get("fractionable")),
              f"{order.symbol} is not fractionable at Alpaca; use a whole-share --qty")

    quote = ctx.quote or {}
    ask, bid = _f(quote.get("ap")), _f(quote.get("bp"))
    quote_ok = ask > 0 and bid > 0 and ask >= bid
    check("quote_valid", quote_ok, "no valid bid/ask (halted, illiquid or market closed)")
    stale = quote_age_problem(quote, ctx.now, lim.max_quote_age_seconds)
    check("quote_fresh", stale is None, stale or "")
    spread_pct = (ask - bid) / ask * 100 if quote_ok else math.inf
    check("spread", spread_pct <= lim.max_spread_pct, f"spread {spread_pct:.2f}% exceeds {lim.max_spread_pct}%")
    check("min_price", ask >= lim.min_share_price, f"price {ask:.2f} below minimum {lim.min_share_price}")

    ref_price = max(ask, order.limit_price or 0.0)
    if has_notional:
        cost = order.notional
    elif has_qty:
        cost = order.qty * ref_price * (1 + lim.price_buffer_pct / 100)
    else:
        cost = math.inf
    check("min_notional", cost >= lim.min_order_notional, f"order value {cost:.2f} below minimum {lim.min_order_notional}")

    equity = equity_of(ctx.account)
    pending_total, pending_by_symbol, unpriced = pending_buy_cost(ctx.open_orders, ctx.positions, order.symbol, ask)
    check("open_orders_priced", not unpriced, f"cannot price open buy orders {unpriced}; resolve them first")

    # Cash only, no leverage: buying_power and the margin multiplier are ignored on purpose.
    free_cash = spendable_cash(ctx.account) - pending_total
    check("cash_only", cost <= free_cash, f"cost {cost:.2f} exceeds free cash {free_cash:.2f} (no margin)")

    pos = position_for(ctx.positions, order.symbol)
    existing = abs(_f(pos.get("market_value"))) if pos else 0.0
    position_after = existing + pending_by_symbol.get(order.symbol, 0.0) + cost
    position_cap = equity * lim.max_position_pct / 100
    check("position_cap", position_after <= position_cap + 1e-9,
          f"position would be {position_after:.2f} > {lim.max_position_pct}% of equity ({position_cap:.2f})")

    exposure_after = long_market_value(ctx.positions) + pending_total + cost
    exposure_cap = equity * lim.max_total_exposure_pct / 100
    check("exposure_cap", exposure_after <= exposure_cap + 1e-9,
          f"total exposure would be {exposure_after:.2f} > {lim.max_total_exposure_pct}% of equity ({exposure_cap:.2f})")

    week_buys = count_week_buys(ctx.week_orders)
    check("weekly_trades", week_buys + 1 <= lim.max_new_trades_per_week,
          f"{week_buys} buys already this week; cap is {lim.max_new_trades_per_week}")

    held = {p.get("symbol") for p in ctx.positions if _f(p.get("qty")) > 0}
    pending_syms = {o.get("symbol") for o in ctx.open_orders if o.get("side") == "buy"}
    count_after = len(held | pending_syms | {order.symbol})
    check("max_positions", count_after <= lim.max_open_positions,
          f"would hold {count_after} positions; cap is {lim.max_open_positions}")

    # Day-trade information must be known (broker field or computed from broker fills). Fail closed.
    if ctx.daytrade_count is None:
        check("daytrade_info", False, "day-trade count unavailable from broker and fills; refusing new entries")
    elif equity < 25000:
        check("pdt_room", ctx.daytrade_count <= lim.max_daytrade_count,
              f"day-trade count {ctx.daytrade_count} leaves no room (max {lim.max_daytrade_count})")

    check("research_documented", ctx.research_documented, "no catalyst for this symbol in today's RESEARCH-LOG entry")

    return Verdict(not reasons, reasons, checks, round(cost, 2) if math.isfinite(cost) else cost)


def _evaluate_sell(order: OrderRequest, ctx: RiskContext) -> Verdict:
    lim = ctx.limits
    checks, reasons, check = _checker()

    check("paper_account", str(ctx.account.get("account_number", "")).startswith("PA"), "not a paper account")
    check("purpose", order.purpose in SELL_PURPOSES, f"sell purpose must be one of {sorted(SELL_PURPOSES)}")
    check("order_type", order.type in SELL_TYPES, f"sell type {order.type!r} not allowed")
    if order.type in PROTECTIVE_TYPES:
        check("protective_purpose", order.purpose == "protective", "stop orders must have purpose 'protective'")
    else:
        check("exit_purpose", order.purpose in ("risk_exit", "discretionary"),
              "market/limit sells must be 'risk_exit' or 'discretionary'")
    check("qty_only", order.notional is None and (order.qty or 0) > 0, "sells must give a positive qty (no notional)")

    pos = position_for(ctx.positions, order.symbol)
    held = _f(pos.get("qty")) if pos else 0.0
    check("long_position_exists", held > 0, f"no long position in {order.symbol}; short selling is not allowed")
    qty = order.qty or 0.0
    reserved = reserved_sell_qty(ctx.open_orders, order.symbol)
    check("no_oversell", qty + reserved <= held + 1e-9,
          f"sell {qty} + already-reserved {reserved} exceeds held {held}; cancel the existing stop first")

    # Fractional shares at Alpaca: DAY time-in-force only, and no trailing stops.
    fractional = not is_whole(qty) or not is_whole(held)
    if fractional:
        check("fractional_tif", order.time_in_force == "day",
              "fractional quantities support time_in_force=day only (no GTC)")
    check("tif_allowed", order.time_in_force in ("day", "gtc"), "time_in_force must be day or gtc")

    current = _f(pos.get("current_price")) if pos else 0.0
    if order.type == "stop":
        sp = order.stop_price or 0.0
        check("stop_below_price", 0 < sp < current, f"stop {sp:.2f} must be below current price {current:.2f}")
    if order.type == "limit":
        check("limit_price", (order.limit_price or 0) > 0, "limit sell needs a positive limit_price")
    if order.type == "trailing_stop":
        check("trail_whole_shares", not fractional, "Alpaca does not support trailing stops on fractional quantities")
        check("trail_percent", order.trail_percent in lim.allowed_trail_percents,
              f"trail_percent must be one of {lim.allowed_trail_percents}")

    if order.purpose == "risk_exit":
        plpc = _f(pos.get("unrealized_plpc")) * 100 if pos else 0.0
        check("risk_exit_verified", pos is not None and plpc <= ctx.cut_loss_pct + 1e-9,
              f"risk_exit needs unrealized P&L <= {ctx.cut_loss_pct}% (is {plpc:.2f}%); use a discretionary sell")
    if order.purpose == "discretionary":
        bought_today = ctx.same_day_buy_symbols is None or order.symbol in ctx.same_day_buy_symbols
        why = ("cannot confirm whether shares were bought today" if ctx.same_day_buy_symbols is None
               else f"{order.symbol} was bought today")
        check("no_same_day_round_trip", not bought_today,
              f"discretionary same-day round trip blocked: {why}. Protective stops and -7% risk exits are still allowed")

    return Verdict(not reasons, reasons, checks, 0.0)


def check_stop_not_lowered(old_stop: Optional[float], new_stop: float) -> Optional[str]:
    """Strategy rule turned into code: a stop may only move up."""
    if old_stop is not None and new_stop < old_stop - 1e-9:
        return f"new stop {new_stop:.2f} is below existing stop {old_stop:.2f}; stops never move down"
    return None


# --- day-trade accounting from broker fills ----------------------------------

def _et_date(ts: str) -> Optional[str]:
    try:
        t = parse_ts(ts)
    except (ValueError, TypeError):
        return None
    try:
        from zoneinfo import ZoneInfo
        return t.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # no tz database: EST is the conservative (later) boundary
        return (t - dt.timedelta(hours=5)).date().isoformat()


def count_day_trades(fills: list) -> int:
    """Day trades = (ET date, symbol) pairs where a buy fill precedes a sell fill. Deduplicates fill ids."""
    seen, per_key = set(), {}
    for f in sorted(fills, key=lambda x: x.get("transaction_time", "")):
        fid = f.get("id")
        if fid and fid in seen:
            continue
        seen.add(fid)
        d = _et_date(f.get("transaction_time", ""))
        if d is None:
            continue
        key = (d, f.get("symbol"))
        state = per_key.setdefault(key, {"bought": False, "round_trip": False})
        if f.get("side") == "buy":
            state["bought"] = True
        elif str(f.get("side", "")).startswith("sell") and state["bought"]:
            state["round_trip"] = True
    return sum(1 for s in per_key.values() if s["round_trip"])


def symbols_bought_on(fills: list, et_date: str) -> set:
    return {f.get("symbol") for f in fills
            if f.get("side") == "buy" and _et_date(f.get("transaction_time", "")) == et_date}
