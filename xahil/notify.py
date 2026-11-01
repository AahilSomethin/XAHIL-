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


def config_problem(env=None) -> str:
    """Detect common Telegram misconfigurations without contacting Telegram. Empty string = looks fine."""
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
    if not re.match(r"^\d+:[A-Za-z0-9_-]{30,}$", token):
        return "TELEGRAM_BOT_TOKEN is not in the digits:secret format from @BotFather"
    if not re.match(r"^-?\d+$", chat_id):
        return "TELEGRAM_CHAT_ID must be numeric (run `python -m xahil telegram-setup`)"
    if chat_id == token.split(":", 1)[0]:
        return ("TELEGRAM_CHAT_ID is the bot's own id; a bot cannot message itself. Message the bot from your "
                "Telegram account, then run `python -m xahil telegram-setup` to find your chat id")
    return ""


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover_chats(env=None, get=_get) -> dict:
    """Read-only: who has messaged the bot recently? Never prints the token."""
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set"}
    base = f"{TELEGRAM_API}/bot{token}"
    try:
        me = get(f"{base}/getMe")
        updates = get(f"{base}/getUpdates")
    except Exception as exc:  # report, never raise; redact the token
        return {"ok": False, "error": redact(str(exc), token)}
    chats = {}
    for u in updates.get("result", []):
        for key in ("message", "edited_message", "my_chat_member"):
            c = (u.get(key) or {}).get("chat") or {}
            if c.get("type") == "private":
                chats[str(c["id"])] = c.get("first_name") or c.get("username") or ""
    bot_id = token.split(":", 1)[0]
    return {"ok": True, "bot_username": (me.get("result") or {}).get("username"),
            "private_chats": [{"chat_id": cid, "first_name": n} for cid, n in chats.items()],
            "configured_chat_id_is_bot_id": env.get("TELEGRAM_CHAT_ID", "").strip() == bot_id,
            "next_step": ("put your chat_id into .env as TELEGRAM_CHAT_ID, then `python -m xahil notify \"test\"`"
                          if chats else "open Telegram, search for the bot, press Start or send any message, then rerun")}
