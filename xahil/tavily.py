"""Tavily search adapter (free tier), read-only.

Budget rules (config/integrations.json -> "tavily"):
  * Only cheap search depths are allowed (basic/fast = 1 credit; advanced = 2 is refused by default).
  * Credits are RESERVED in a Git-backed append-only log BEFORE each request, so a crash or
    interrupted run can never under-count. Monthly and daily caps sit below Tavily's free
    1,000 credits/month.
  * HTTP 432 (plan limit reached) blocks Tavily for the rest of the month. 429 is not retried.
  * Identical queries within the cache TTL are served from `.cache/research/` for 0 credits.
Fail closed: any budget, key, network or response problem raises ResearchUnavailable.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from xahil import journal
from xahil.config import ROOT, STATE_DIR

TAVILY_URL = "https://api.tavily.com/search"
USAGE_LOG = STATE_DIR / "research_usage.jsonl"
CACHE_DIR = ROOT / ".cache" / "research"
CREDITS = {"basic": 1, "fast": 1, "ultra-fast": 1, "advanced": 2}
TOPICS = ("news", "finance", "general")
TIME_RANGES = ("day", "week", "month", "year")


class TavilyUnavailable(RuntimeError):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --- usage accounting -------------------------------------------------------------

def usage_summary(now: dt.datetime, path: Path = USAGE_LOG) -> dict:
    month, day = now.strftime("%Y-%m"), now.date().isoformat()
    month_credits = day_credits = requests = 0
    blocked = None
    for r in journal.read(path):
        if r.get("provider") != "tavily":
            continue
        ts = str(r.get("ts", ""))
        if ts[:7] != month:
            continue
        c = int(r.get("credits", 0))
        month_credits += c
        if ts[:10] == day:
            day_credits += c
        if r.get("status") == "reserved":
            requests += 1
        if r.get("status") == "plan_limit_reached":
            blocked = r.get("ts")
    return {"month": month, "month_credits": month_credits, "day_credits": day_credits,
            "requests": requests, "blocked_since": blocked}


def check_budget(settings: dict, depth: str, now: dt.datetime, path: Path = USAGE_LOG) -> dict:
    t = settings.get("tavily", {})
    if not t.get("enabled", False):
        raise TavilyUnavailable("Tavily disabled in config/integrations.json")
    if depth not in t.get("allowed_depths", ["basic"]):
        raise TavilyUnavailable(f"search_depth {depth!r} not allowed (allowed: {t.get('allowed_depths', ['basic'])})")
    cost = CREDITS.get(depth)
    if cost is None:
        raise TavilyUnavailable(f"unknown search_depth {depth!r}")
    u = usage_summary(now, path)
    if u["blocked_since"]:
        raise TavilyUnavailable(f"Tavily reported its plan limit reached this month ({u['blocked_since'][:10]})")
    if u["month_credits"] + cost > int(t.get("monthly_credit_cap", 0)):
        raise TavilyUnavailable(f"Tavily monthly credit cap reached ({u['month_credits']}/{t.get('monthly_credit_cap')})")
    if u["day_credits"] + cost > int(t.get("daily_credit_cap", 0)):
        raise TavilyUnavailable(f"Tavily daily credit cap reached ({u['day_credits']}/{t.get('daily_credit_cap')})")
    return {"cost": cost, **u}


def _log(path: Path, **rec) -> None:
    journal.append(path, {"provider": "tavily", **rec})


# --- cache ------------------------------------------------------------------------

def cache_key(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:32]


def cache_get(key: str, ttl_hours: float, now: dt.datetime, cache_dir: Path = CACHE_DIR) -> Optional[dict]:
    p = cache_dir / f"{key}.json"
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        fetched = dt.datetime.fromisoformat(rec["fetched_at"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None
    if (now - fetched).total_seconds() > ttl_hours * 3600:
        return None
    return rec


def cache_put(key: str, response: dict, fetched_at: dt.datetime, cache_dir: Path = CACHE_DIR) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{key}.json").write_text(
        json.dumps({"fetched_at": fetched_at.isoformat(timespec="seconds"), "response": response}), encoding="utf-8")


# --- parsing ------------------------------------------------------------------------

def normalize_date(raw) -> Optional[str]:
    """Tavily's published_date is a best estimate (ISO or RFC 2822) or null. Returns YYYY-MM-DD or None."""
    if not raw:
        return None
    s = str(raw).strip()
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(s)
        return parsed.date().isoformat() if parsed else None
    except (TypeError, ValueError):
        pass
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def parse_results(data: dict, retrieved_at: str) -> list:
    out = []
    for r in data.get("results") or []:
        url = r.get("url") or ""
        if not url.startswith(("http://", "https://")):
            continue
        out.append({
            "url": url, "domain": domain_of(url), "title": " ".join(str(r.get("title") or "").split())[:300],
            "published_date": normalize_date(r.get("published_date")),
            "published_raw": r.get("published_date"), "retrieved_at": retrieved_at,
            "score": r.get("score"), "snippet": " ".join(str(r.get("content") or "").split())[:800],
        })
    return out


# --- HTTP -------------------------------------------------------------------------

def _post(url: str, headers: dict, payload: dict) -> tuple:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TavilyUnavailable(f"Tavily network error: {type(exc).__name__}") from None


def _redact(text: str, key: str) -> str:
    text = re.sub(r"tvly-[A-Za-z0-9_-]+", "tvly-<redacted>", text)
    return text.replace(key, "<redacted>") if key else text


def search(query: str, settings: dict, env, *, topic: str = None, time_range: str = None,
           max_results: int = None, depth: str = None, post=_post, now: dt.datetime = None,
           usage_path: Path = USAGE_LOG, cache_dir: Path = CACHE_DIR) -> dict:
    t = settings.get("tavily", {})
    now = now or _utcnow()
    topic = topic or t.get("default_topic", "news")
    time_range = time_range or t.get("default_time_range", "week")
    depth = depth or t.get("search_depth", "basic")
    max_results = int(max_results or t.get("max_results", 8))
    if topic not in TOPICS:
        raise TavilyUnavailable(f"topic must be one of {TOPICS}")
    if time_range not in TIME_RANGES:
        raise TavilyUnavailable(f"time_range must be one of {TIME_RANGES}")
    if not (1 <= max_results <= 20):
        raise TavilyUnavailable("max_results must be 1..20")
    if not query.strip():
        raise TavilyUnavailable("empty query")
    proxy = str(env.get("XAHIL_TAVILY_AUTH", "")).lower() == "proxy"
    key = "" if proxy else str(env.get("TAVILY_API_KEY", "")).strip()
    if not proxy and not key:
        raise TavilyUnavailable("TAVILY_API_KEY not set")

    payload = {"query": query.strip()[:400], "topic": topic, "time_range": time_range, "search_depth": depth,
               "max_results": max_results, "include_answer": False, "include_raw_content": False,
               "include_usage": True}
    ckey = cache_key(payload)
    cached = cache_get(ckey, float(t.get("cache_ttl_hours", 6)), now, cache_dir)
    if cached:
        return {"payload": payload, "response": cached["response"], "retrieved_at": cached["fetched_at"],
                "cached": True, "credits": 0}

    budget = check_budget(settings, depth, now, usage_path)
    rid = uuid.uuid4().hex[:12]
    stamp = now.isoformat(timespec="seconds")
    _log(usage_path, ts=stamp, id=rid, status="reserved", credits=budget["cost"], query_hash=ckey, depth=depth)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if not proxy:
        headers["Authorization"] = f"Bearer {key}"
    status, body = post(TAVILY_URL, headers, payload)
    if status == 432:
        _log(usage_path, ts=stamp, id=rid, status="plan_limit_reached", credits=0)
        raise TavilyUnavailable("Tavily plan limit reached (HTTP 432); blocked for the rest of the month")
    if status == 433:
        _log(usage_path, ts=stamp, id=rid, status="payg_limit_reached", credits=0)
        raise TavilyUnavailable("Tavily pay-as-you-go limit reached (HTTP 433); XAHIL never enables paid usage")
    if status >= 400:
        _log(usage_path, ts=stamp, id=rid, status=f"http_{status}", credits=0)
        raise TavilyUnavailable(f"Tavily HTTP {status}: {_redact(body[:200], key)}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise TavilyUnavailable("Tavily returned non-JSON") from None
    actual = (data.get("usage") or {}).get("credits")
    if isinstance(actual, (int, float)) and actual > budget["cost"]:
        _log(usage_path, ts=stamp, id=rid, status="adjustment", credits=int(actual - budget["cost"]))
    cache_put(ckey, data, now, cache_dir)
    return {"payload": payload, "response": data, "retrieved_at": stamp, "cached": False,
            "credits": int(actual) if isinstance(actual, (int, float)) else budget["cost"]}
