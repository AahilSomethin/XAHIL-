"""Optional cited research through Perplexity, with a hard monthly request cap.

Disabled by default (config/integrations.json). When disabled, unavailable or
over budget, callers get ResearchUnavailable and fall back to Claude's own
WebSearch, noting the fallback in the research log.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from xahil.config import STATE_DIR, load_integrations

PERPLEXITY_URL = "https://api.perplexity.ai/chat/completions"
USAGE_FILE = STATE_DIR / "perplexity_usage.json"
SYSTEM_PROMPT = (
    "You are a precise financial research assistant. Cite every factual claim. "
    "Say 'unknown' rather than guess. Be concise. This is for a paper-trading simulation."
)


class ResearchUnavailable(RuntimeError):
    pass


def _month(today: dt.date) -> str:
    return today.strftime("%Y-%m")


def load_usage(path: Path = USAGE_FILE) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def record_usage(today: dt.date, path: Path = USAGE_FILE) -> int:
    usage = load_usage(path)
    key = _month(today)
    usage[key] = int(usage.get(key, 0)) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(usage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return usage[key]


def check_available(settings: dict, env, today: dt.date, usage_path: Path = USAGE_FILE) -> dict:
    p = settings.get("perplexity", {})
    if not p.get("enabled", False):
        raise ResearchUnavailable("Perplexity disabled in config/integrations.json (needs owner approval: paid API)")
    proxy = env.get("XAHIL_PERPLEXITY_AUTH", "").lower() == "proxy"
    if not proxy and not env.get("PERPLEXITY_API_KEY"):
        raise ResearchUnavailable("PERPLEXITY_API_KEY not set")
    cap = int(p.get("monthly_request_cap", 0))
    used = int(load_usage(usage_path).get(_month(today), 0))
    if used >= cap:
        raise ResearchUnavailable(f"Perplexity monthly cap reached ({used}/{cap})")
    return {"model": p.get("model", "sonar"), "proxy": proxy, "used": used, "cap": cap}


def _post(url: str, headers: dict, payload: dict) -> tuple:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ResearchUnavailable(f"Perplexity network error: {type(exc).__name__}") from None


def ask(query: str, env=None, today: dt.date = None, post=_post, settings: dict = None,
        usage_path: Path = USAGE_FILE) -> dict:
    env = os.environ if env is None else env
    today = today or dt.date.today()
    settings = load_integrations() if settings is None else settings
    info = check_available(settings, env, today, usage_path)
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if not info["proxy"]:
        headers["Authorization"] = f"Bearer {env['PERPLEXITY_API_KEY']}"
    payload = {"model": info["model"], "messages": [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": query},
    ]}
    # Count the request before sending so a crash can never under-count spend.
    record_usage(today, usage_path)
    status, body = post(PERPLEXITY_URL, headers, payload)
    if status >= 400:
        raise ResearchUnavailable(f"Perplexity HTTP {status}: {body[:200]}")
    data = json.loads(body)
    content = data["choices"][0]["message"]["content"]
    citations = data.get("citations") or [r.get("url") for r in data.get("search_results", []) if r.get("url")]
    return {"query": query, "answer": content, "citations": citations, "model": info["model"]}


def to_markdown(result: dict) -> str:
    lines = [f"**Q:** {result['query']}", "", result["answer"], ""]
    if result["citations"]:
        lines.append("Sources:")
        lines += [f"- [{i + 1}] {u}" for i, u in enumerate(result["citations"])]
    else:
        lines.append("Sources: none returned - treat as unverified.")
    return "\n".join(lines)
