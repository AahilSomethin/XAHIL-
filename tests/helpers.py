"""Fake Alpaca transport and fixtures. No test touches the network."""

import json
import urllib.parse

from xahil.config import RiskLimits, load_denylist

PAPER = "https://paper-api.alpaca.markets/v2"
DATA = "https://data.alpaca.markets/v2"


def limits(**kw) -> RiskLimits:
    return RiskLimits(denylist=load_denylist(), **kw)


def account(**kw) -> dict:
    base = {"account_number": "PA1234567", "status": "ACTIVE", "equity": "1000", "cash": "1000",
            "last_equity": "1000", "buying_power": "4000", "daytrade_count": None, "multiplier": "4",
            "trading_blocked": False, "account_blocked": False}
    base.update(kw)
    return base


def position(symbol, qty, price, entry=None, plpc=0.0, **kw) -> dict:
    entry = entry if entry is not None else price
    p = {"symbol": symbol, "qty": str(qty), "current_price": str(price), "avg_entry_price": str(entry),
         "market_value": str(qty * price), "unrealized_plpc": str(plpc), "unrealized_pl": str(qty * (price - entry)),
         "change_today": "0", "side": "long", "asset_class": "us_equity"}
    p.update(kw)
    return p


def asset(symbol="AAPL", **kw) -> dict:
    a = {"symbol": symbol, "class": "us_equity", "status": "active", "tradable": True,
         "exchange": "NASDAQ", "name": f"{symbol} Inc. Common Stock", "fractionable": True}
    a.update(kw)
    return a


CLOCK_TS = "2026-10-28T10:00:00.123456789-04:00"   # 14:00:00Z, market open
FRESH_QUOTE_TS = "2026-10-28T13:59:50.5Z"           # 10 s before the clock


def quote(ask=100.0, bid=99.95, t=FRESH_QUOTE_TS) -> dict:
    q = {"ap": ask, "bp": bid}
    if t is not None:
        q["t"] = t
    return q


def fill(symbol, side, qty, price, when, order_id=None, fid=None) -> dict:
    return {"id": fid or f"{symbol}-{side}-{when}", "order_id": order_id or f"o-{symbol}-{side}-{when}",
            "activity_type": "FILL", "symbol": symbol, "side": side, "qty": str(qty), "price": str(price),
            "transaction_time": when}


class FakeAlpaca:
    """Routes requests by method+path. Records every call, including blocked writes."""

    def __init__(self, **state):
        self.calls = []
        self.submitted = {}       # client_order_id -> order (what Alpaca "has")
        self.post_behaviour = None  # None | "timeout_after_accept" | "timeout_before_accept" | ("http", code, body)
        self.cancel_behaviour = None
        self.state = {
            "account": account(),
            "positions": [],
            "orders_open": [],
            "orders_all": [],
            "asset": asset(),
            "quote": quote(20.0, 19.99),  # $20: whole shares fit the $50 position cap
            "clock": {"timestamp": CLOCK_TS, "is_open": True,
                      "next_open": "2026-10-29T09:30:00-04:00", "next_close": "2026-10-28T16:00:00-04:00"},
            "calendar": [{"date": "2026-10-28", "open": "09:30", "close": "16:00"}],
            "bars": [],
            "fills": [],
        }
        self.state.update(state)

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, body))
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path.endswith("/account"):
            return 200, json.dumps(self.state["account"])
        if path.endswith("/positions"):
            return 200, json.dumps(self.state["positions"])
        if path.endswith("/orders") and method == "GET":
            key = "orders_all" if qs.get("status") == ["all"] else "orders_open"
            return 200, json.dumps(self.state[key])
        if path.endswith("/orders:by_client_order_id"):
            coid = qs.get("client_order_id", [""])[0]
            if coid in self.submitted:
                return 200, json.dumps(self.submitted[coid])
            return 404, '{"message": "order not found"}'
        if path.endswith("/orders") and method == "POST":
            order = {"id": f"id-{len(self.submitted) + 1}", "status": "accepted", **json.loads(body)}
            b = self.post_behaviour
            if b == "timeout_before_accept":
                from xahil.alpaca import AlpacaError
                raise AlpacaError("Network error calling Alpaca (TimeoutError: timed out)")
            if isinstance(b, tuple) and b[0] == "http":
                return b[1], b[2]
            if order["client_order_id"] in self.submitted:
                return 422, '{"message": "client_order_id must be unique"}'
            self.submitted[order["client_order_id"]] = order
            if b == "timeout_after_accept":
                from xahil.alpaca import AlpacaError
                raise AlpacaError("Network error calling Alpaca (TimeoutError: timed out)")
            if b == "broker_rejects":
                order["status"] = "rejected"
            return 200, json.dumps(order)
        if "/orders/" in path and method == "DELETE":
            if self.cancel_behaviour:
                return self.cancel_behaviour
            return 204, ""
        if "/assets/" in path:
            return 200, json.dumps(self.state["asset"])
        if path.endswith("/clock"):
            return 200, json.dumps(self.state["clock"])
        if path.endswith("/calendar"):
            return 200, json.dumps(self.state["calendar"])
        if path.endswith("/quotes/latest"):
            return 200, json.dumps({"quote": self.state["quote"]})
        if path.endswith("/bars"):
            sym = path.split("/stocks/")[1].split("/")[0]
            bars = self.state.get("bars_by_symbol", {}).get(sym, self.state["bars"])
            start, end = qs.get("start", [""])[0], qs.get("end", [None])[0]
            bars = [b for b in bars if str(b["t"])[:10] >= start[:10] and (not end or str(b["t"])[:10] <= end[:10])]
            return 200, json.dumps({"bars": bars})
        if path.endswith("/account/activities"):
            if self.state.get("fills_error"):
                return 500, '{"message": "internal error"}'
            fills = self.state["fills"]
            after = qs.get("after", [None])[0]
            if after:
                from xahil.risk import parse_ts
                cut = parse_ts(after)
                fills = [f for f in fills if parse_ts(f["transaction_time"]) > cut]
            return 200, json.dumps(fills)
        return 404, "not found"

    def posts(self):
        return [c for c in self.calls if c[0] in ("POST", "DELETE")]
