"""Telegram notifications with a committed fallback outbox.

The bot token travels in the URL path, so Anthropic network secrets (which
inject headers) cannot carry it; it must be an environment variable.
Never crashes a workflow: on any failure the message goes to the outbox.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

from xahil.config import MEMORY_DIR

TELEGRAM_API = "https://api.telegram.org"
MAX_LEN = 4000  # Telegram hard limit is 4096
OUTBOX = MEMORY_DIR / "NOTIFICATIONS-OUTBOX.md"
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def redact(text: str, token: str = "") -> str:
    text = _TOKEN_RE.sub("bot<redacted>", text)
    if token:
        text = text.replace(token, "<redacted>")
    return text


def truncate(msg: str) -> str:
    return msg if len(msg) <= MAX_LEN else msg[: MAX_LEN - 20] + "\n...[truncated]"


def _post(url: str, payload: dict) -> tuple:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def append_outbox(msg: str, reason: str, outbox: Path = OUTBOX) -> None:
    outbox.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if not outbox.exists():
        outbox.write_text("# Notifications Outbox\n\nMessages that could not be sent to Telegram.\n", encoding="utf-8")
    with outbox.open("a", encoding="utf-8") as fh:
        fh.write(f"\n---\n## {stamp} ({reason})\n{msg}\n")


def send(msg: str, env=None, post=_post, outbox: Path = OUTBOX) -> dict:
    env = os.environ if env is None else env
    msg = truncate(msg.strip())
    if not msg:
        raise ValueError("empty message")
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        append_outbox(msg, "fallback - Telegram not configured", outbox)
        return {"sent": False, "fallback": True, "reason": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"}
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    try:
        status, body = post(url, {"chat_id": chat_id, "text": msg, "disable_web_page_preview": True})
    except Exception as exc:  # network errors must not crash a workflow
        status, body = 0, str(exc)
    if 200 <= status < 300:
        return {"sent": True, "fallback": False}
    reason = redact(f"Telegram HTTP {status}: {body[:200]}", token)
    append_outbox(msg, "fallback - " + reason, outbox)
    return {"sent": False, "fallback": True, "reason": reason}
