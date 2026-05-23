"""Minimal Telegram Bot API client over the stdlib (no extra dependency).

Only the handful of methods Acta needs: ``sendMessage`` (outbound
notifications + link confirmations), ``setWebhook`` / ``deleteWebhook``
(webhook registration), and ``getUpdates`` (the dev long-poll fallback).
Every call is best-effort: network / API errors are swallowed and logged,
returning ``None`` so a failed Telegram round-trip never breaks the
request that triggered it.
"""

from __future__ import annotations

import json
import logging
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/{method}"


def is_configured() -> bool:
    """Return whether a bot token is set (the integration is usable)."""
    return bool(getattr(settings, "TELEGRAM_BOT_TOKEN", ""))


def _call(method: str, params: dict[str, Any], *, timeout: float = 5.0) -> dict | None:
    """POST ``params`` to a Bot API ``method``; return ``result`` or None.

    Swallows transport + API errors (logs them) so callers never have to
    guard the network. Returns the decoded ``result`` payload on success.
    """
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None
    url = _API_BASE.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        logger.warning("Telegram %s failed: %s", method, exc)
        return None
    if not body.get("ok"):
        logger.warning("Telegram %s returned not-ok: %s", method, body.get("description"))
        return None
    return body.get("result")


def send_message(chat_id: int, text: str) -> bool:
    """Send a plain-HTML message to a chat. Returns success."""
    result = _call(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )
    return result is not None


def set_webhook(url: str, secret_token: str = "") -> bool:
    """Register ``url`` as the bot's webhook. Returns success."""
    params: dict[str, Any] = {"url": url}
    if secret_token:
        params["secret_token"] = secret_token
    return _call("setWebhook", params) is not None


def delete_webhook() -> bool:
    """Remove the bot's webhook (e.g. before switching to polling)."""
    return _call("deleteWebhook", {}) is not None


def get_updates(offset: int | None = None, timeout: int = 25) -> list[dict]:
    """Long-poll updates (dev fallback when no public webhook URL exists)."""
    params: dict[str, Any] = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    result = _call("getUpdates", params, timeout=timeout + 5)
    return result or []
