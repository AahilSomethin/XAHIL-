"""Paper-only Alpaca REST client.

Only two URL prefixes are reachable: the approved paper trading endpoint and
the market-data endpoint. Redirects are refused. There is deliberately no
close-all, cancel-all or raw-JSON order method.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from xahil.config import (
    APPROVED_DATA_ENDPOINT,
    APPROVED_TRADING_ENDPOINT,
    AlpacaCredentials,
    ConfigError,
    resolve_alpaca_credentials,
    resolve_data_endpoint,
    resolve_trading_endpoint,
)

# transport(method, url, headers, body_bytes_or_None) -> (status, body_text)
Transport = Callable[[str, str, dict, Optional[bytes]], tuple]


class AlpacaError(RuntimeError):
    def __init__(self, message: str, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise AlpacaError(f"Refusing redirect to {newurl}", status=code)


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes]) -> tuple:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # DNS failure, refused connection, timeout: fail closed with a clean error.
        reason = getattr(exc, "reason", exc)
        raise AlpacaError(f"Network error calling Alpaca ({type(exc).__name__}: {reason})") from None


def _check_url(url: str) -> None:
    for base in (APPROVED_TRADING_ENDPOINT, APPROVED_DATA_ENDPOINT):
        if url == base or url.startswith(base + "/") or url.startswith(base + "?"):
            return
    raise ConfigError(f"Blocked request to non-approved URL: {url}")


class AlpacaClient:
    def __init__(
        self,
        credentials: AlpacaCredentials,
        trading_base: str = APPROVED_TRADING_ENDPOINT,
        data_base: str = APPROVED_DATA_ENDPOINT,
        transport: Transport = urllib_transport,
    ):
        if trading_base != APPROVED_TRADING_ENDPOINT:
            raise ConfigError("AlpacaClient only accepts the approved paper trading endpoint")
        if data_base != APPROVED_DATA_ENDPOINT:
            raise ConfigError("AlpacaClient only accepts the approved data endpoint")
        self.credentials = credentials
        self.trading_base = trading_base
        self.data_base = data_base
        self.transport = transport

    @classmethod
    def from_env(cls, env=None, transport: Transport = urllib_transport) -> "AlpacaClient":
        return cls(
            credentials=resolve_alpaca_credentials(env),
            trading_base=resolve_trading_endpoint(env),
            data_base=resolve_data_endpoint(env),
            transport=transport,
        )

    # --- low level -----------------------------------------------------------
    def _request(self, method: str, base: str, path: str, params=None, body=None):
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        _check_url(url)
        headers = {"Accept": "application/json", **self.credentials.headers()}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        status, text = self.transport(method, url, headers, data)
        if status >= 400:
            raise AlpacaError(f"Alpaca {method} {path} failed with HTTP {status}: {text[:300]}", status, text)
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise AlpacaError(f"Non-JSON response from {path}", status, text) from exc

    def _get(self, path, params=None):
        return self._request("GET", self.trading_base, path, params)

    def _data_get(self, path, params=None):
        return self._request("GET", self.data_base, path, params)

    # --- read --------------------------------------------------------------
    def account(self) -> dict:
        acct = self._get("/account")
        number = str(acct.get("account_number", ""))
        if not number.startswith("PA"):
            raise ConfigError(
                "Connected account is not a paper account (paper account numbers start with 'PA'). Refusing to run."
            )
        return acct

    def positions(self) -> list:
        return self._get("/positions") or []

    def orders(self, status: str = "open", after: str = None, limit: int = 500, side: str = None) -> list:
        params = {"status": status, "limit": limit, "after": after, "direction": "desc"}
        if side:
            params["side"] = side
        return self._get("/orders", params) or []

    def asset(self, symbol: str) -> dict:
        return self._get(f"/assets/{urllib.parse.quote(symbol)}")

    def clock(self) -> dict:
        return self._get("/clock")

    def calendar(self, start: str, end: str) -> list:
        return self._get("/calendar", {"start": start, "end": end}) or []

    def fills(self, after: str = None, max_pages: int = 20) -> list:
        """All FILL activities after `after` (ISO time), paginated, oldest first."""
        out, token = [], None
        for _ in range(max_pages):
            params = {"activity_types": "FILL", "direction": "asc", "page_size": 100, "after": after,
                      "page_token": token}
            page = self._get("/account/activities", params) or []
            out.extend(page)
            if len(page) < 100:
                return out
            token = page[-1].get("id")
        raise AlpacaError("fill history exceeded pagination limit; refusing to compute on partial data")

    def order_by_client_id(self, client_order_id: str) -> Optional[dict]:
        """Look up an order by our own client_order_id. Returns None if Alpaca has no such order."""
        try:
            return self._get("/orders:by_client_order_id", {"client_order_id": client_order_id})
        except AlpacaError as exc:
            if exc.status == 404:
                return None
            raise

    def latest_quote(self, symbol: str) -> dict:
        data = self._data_get(f"/stocks/{urllib.parse.quote(symbol)}/quotes/latest", {"feed": "iex"})
        return (data or {}).get("quote", {})

    def daily_bars(self, symbol: str, start: str, end: str = None, max_pages: int = 20) -> list:
        """Split/dividend-adjusted daily bars from the free IEX feed, paginated."""
        out, token = [], None
        for _ in range(max_pages):
            params = {"timeframe": "1Day", "start": start, "end": end, "feed": "iex", "adjustment": "all",
                      "limit": 10000, "page_token": token}
            data = self._data_get(f"/stocks/{urllib.parse.quote(symbol)}/bars", params) or {}
            out.extend(data.get("bars") or [])
            token = data.get("next_page_token")
            if not token:
                return out
        raise AlpacaError(f"bar history for {symbol} exceeded pagination limit")

    # --- write (only called by the execution layer after the risk engine) ----
    def submit_order(self, order: dict) -> dict:
        return self._request("POST", self.trading_base, "/orders", body=order)

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", self.trading_base, f"/orders/{urllib.parse.quote(order_id)}")
