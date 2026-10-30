"""Daily-bar fetching with a local cache (`.cache/bars/`, git-ignored). Read-only market data."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from xahil.alpaca import AlpacaError
from xahil.config import ROOT

CACHE_DIR = ROOT / ".cache" / "bars"


def _cache_path(symbol: str, start: str, end: str) -> Path:
    return CACHE_DIR / f"{symbol}_{start}_{end}.json"


def get_bars(client, symbol: str, start: str, end: str, use_cache: bool = True) -> list:
    """Bars for [start, end]. Only fully past windows are cached (today's bar may still change)."""
    today = dt.date.today().isoformat()
    cacheable = use_cache and end < today
    path = _cache_path(symbol, start, end)
    if cacheable and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    bars = client.daily_bars(symbol, start=start, end=end)
    bars = [b for b in bars if str(b.get("t", ""))[:10] <= end]
    if cacheable:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(bars), encoding="utf-8")
    return bars


def get_many(client, symbols: list, start: str, end: str) -> tuple:
    """Returns (bars_by_symbol, errors_by_symbol). A missing symbol never aborts the batch."""
    out, errors = {}, {}
    for s in symbols:
        try:
            out[s] = get_bars(client, s, start, end)
        except AlpacaError as exc:
            errors[s] = str(exc)[:120]
    return out, errors
