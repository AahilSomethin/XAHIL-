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
            "last_equity": "1000", "buying_power": "4000", "daytrade_count": 0, "multiplier": "4",
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


def quote(ask=100.0, bid=99.95) -> dict:
    return {"ap": ask, "bp": bid}


class FakeAlpaca:
    """Routes requests by method+path. Records every call, including blocked writes."""

    def __init__(self, **state):
        self.calls = []
        self.state = {
            "account": account(),
            "positions": [],
            "orders_open": [],
            "orders_all": [],
            "asset": asset(),
            "quote": quote(),
            "clock": {"timestamp": "2026-10-28T10:00:00.123456789-04:00", "is_open": True,
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
        if path.endswith("/orders") and method == "POST":
            return 200, json.dumps({"id": "new-order-id", "status": "accepted", **json.loads(body)})
        if "/orders/" in path and method == "DELETE":
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
            return 200, json.dumps({"bars": self.state["bars"]})
        if path.endswith("/account/activities"):
            return 200, json.dumps(self.state["fills"])
        return 404, "not found"

    def posts(self):
        return [c for c in self.calls if c[0] in ("POST", "DELETE")]
