"""Deterministic risk engine.

Pure functions over broker state. No network, no LLM. Every order the agent
wants to place must get an approved Verdict from `evaluate_order` first; the
CLI is the only code path that submits orders and it always calls this.

Enforced (owner requirements):
  * paper account only, cash only, long only, no leverage
  * US-listed stocks/ETFs only; no options, crypto, OTC, leveraged/inverse ETFs
  * max 3 new buy orders per week (counted from the broker, not the log)
  * max 5% of equity per position, max 20% total long exposure
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Optional

from xahil.config import RiskLimits

SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
LEVERAGE_NAME_RE = re.compile(
    r"(\b[1-9](\.\d+)?X\b|-[1-9](\.\d+)?X\b|ULTRA|INVERSE|\bBEAR\b|\bSHORT\b(?![- ]?(TERM|DURATION|MATURITY))"
    r"|LEVERAGED|DAILY TARGET|\bVIX\b|VOLATILITY (INDEX|FUTURES))",
    re.IGNORECASE,
)
DEAD_ORDER_STATUSES = {"canceled", "expired", "rejected", "replaced", "done_for_day"}
BUY_TYPES = {"market", "limit"}
SELL_TYPES = {"market", "limit", "stop", "trailing_stop"}


def _f(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def is_whole(qty: float) -> bool:
    return abs(qty - round(qty)) < 1e-9


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
    cap = limits.starting_capital * limits.max_equity_multiple_of_start
    if equity > cap:
        reasons.append(
            f"equity {equity:,.2f} exceeds {limits.max_equity_multiple_of_start:g}x configured starting capital "
            f"({limits.starting_capital:,.2f}); reset the paper account balance"
        )
    for p in positions:
        if _f(p.get("qty")) < 0 or p.get("side") == "short":
            reasons.append(f"short position detected in {p.get('symbol')}")
        if p.get("asset_class") not in (None, "us_equity"):
            reasons.append(f"non-equity position {p.get('symbol')} ({p.get('asset_class')})")
    return reasons


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


def _evaluate_buy(order: OrderRequest, ctx: RiskContext) -> Verdict:
    lim = ctx.limits
    checks: dict = {}
    reasons: list = []

    def check(name: str, ok: bool, why: str):
        checks[name] = bool(ok)
        if not ok:
            reasons.append(why)

    halts = list(ctx.halt_reasons) + account_halt_reasons(ctx.account, ctx.positions, lim)
    check("no_halt", not halts, "halted: " + "; ".join(halts))

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

    quote = ctx.quote or {}
    ask, bid = _f(quote.get("ap")), _f(quote.get("bp"))
    quote_ok = ask > 0 and bid > 0 and ask >= bid
    check("quote_valid", quote_ok, "no valid bid/ask (halted, illiquid or market closed)")
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
    cash = _f(ctx.account.get("cash"))
    pending_total, pending_by_symbol, unpriced = pending_buy_cost(ctx.open_orders, ctx.positions, order.symbol, ask)
    check("open_orders_priced", not unpriced, f"cannot price open buy orders {unpriced}; resolve them first")

    # Cash only, no leverage: buying_power is ignored on purpose.
    free_cash = cash - pending_total
    check("cash_only", cost <= free_cash, f"cost {cost:.2f} exceeds free cash {free_cash:.2f} (no margin)")

    pos = position_for(ctx.positions, order.symbol)
    existing = abs(_f(pos.get("market_value"))) if pos else 0.0
    position_after = existing + pending_by_symbol.get(order.symbol, 0.0) + cost
    position_cap = equity * lim.max_position_pct / 100
    check(
        "position_cap",
        position_after <= position_cap + 1e-9,
        f"position would be {position_after:.2f} > {lim.max_position_pct}% of equity ({position_cap:.2f})",
    )

    exposure_after = long_market_value(ctx.positions) + pending_total + cost
    exposure_cap = equity * lim.max_total_exposure_pct / 100
    check(
        "exposure_cap",
        exposure_after <= exposure_cap + 1e-9,
        f"total exposure would be {exposure_after:.2f} > {lim.max_total_exposure_pct}% of equity ({exposure_cap:.2f})",
    )

    week_buys = count_week_buys(ctx.week_orders)
    check(
        "weekly_trades",
        week_buys + 1 <= lim.max_new_trades_per_week,
        f"{week_buys} buys already this week; cap is {lim.max_new_trades_per_week}",
    )

    held = {p.get("symbol") for p in ctx.positions if _f(p.get("qty")) > 0}
    pending_syms = {o.get("symbol") for o in ctx.open_orders if o.get("side") == "buy"}
    count_after = len(held | pending_syms | {order.symbol})
    check(
        "max_positions",
        count_after <= lim.max_open_positions,
        f"would hold {count_after} positions; cap is {lim.max_open_positions}",
    )

    if equity < 25000:
        dt = int(_f(ctx.account.get("daytrade_count")))
        check("pdt_room", dt <= lim.max_daytrade_count, f"daytrade_count {dt} leaves no PDT room")

    check("research_documented", ctx.research_documented, "no catalyst for this symbol in today's RESEARCH-LOG entry")

    return Verdict(not reasons, reasons, checks, round(cost, 2) if math.isfinite(cost) else cost)


def _evaluate_sell(order: OrderRequest, ctx: RiskContext) -> Verdict:
    lim = ctx.limits
    checks: dict = {}
    reasons: list = []

    def check(name: str, ok: bool, why: str):
        checks[name] = bool(ok)
        if not ok:
            reasons.append(why)

    check("paper_account", str(ctx.account.get("account_number", "")).startswith("PA"), "not a paper account")
    check("order_type", order.type in SELL_TYPES, f"sell type {order.type!r} not allowed")
    check("qty_only", order.notional is None and (order.qty or 0) > 0, "sells must give a positive qty (no notional)")

    pos = position_for(ctx.positions, order.symbol)
    held = _f(pos.get("qty")) if pos else 0.0
    check("long_position_exists", held > 0, f"no long position in {order.symbol}; short selling is not allowed")
    qty = order.qty or 0.0
    reserved = reserved_sell_qty(ctx.open_orders, order.symbol)
    check(
        "no_oversell",
        qty + reserved <= held + 1e-9,
        f"sell {qty} + already-reserved {reserved} exceeds held {held}; cancel the existing stop first",
    )

    fractional = not is_whole(qty) or not is_whole(held)
    if order.type in ("market", "limit", "stop") and fractional:
        check("fractional_tif", order.time_in_force == "day", "fractional orders must be time_in_force=day")
    check("tif_allowed", order.time_in_force in ("day", "gtc"), "time_in_force must be day or gtc")

    current = _f(pos.get("current_price")) if pos else 0.0
    if order.type == "stop":
        sp = order.stop_price or 0.0
        check("stop_below_price", 0 < sp < current, f"stop {sp:.2f} must be below current price {current:.2f}")
    if order.type == "limit":
        check("limit_price", (order.limit_price or 0) > 0, "limit sell needs a positive limit_price")
    if order.type == "trailing_stop":
        check("trail_whole_shares", is_whole(qty), "Alpaca does not support trailing stops on fractional qty")
        check(
            "trail_percent",
            order.trail_percent in lim.allowed_trail_percents,
            f"trail_percent must be one of {lim.allowed_trail_percents}",
        )

    return Verdict(not reasons, reasons, checks, 0.0)


def check_stop_not_lowered(old_stop: Optional[float], new_stop: float) -> Optional[str]:
    """Strategy rule turned into code: a stop may only move up."""
    if old_stop is not None and new_stop < old_stop - 1e-9:
        return f"new stop {new_stop:.2f} is below existing stop {old_stop:.2f}; stops never move down"
    return None
