"""Account-linking + inbound-update handling for the Telegram bot.

Linking is a deep-link dance: Acta mints a short-lived token (a
``TelegramLinkToken`` row — short + URL-safe to fit Telegram's 64-char,
``[A-Za-z0-9_-]`` ``start`` parameter), embeds it in
``t.me/<bot>?start=<token>``, and the user tapping it makes Telegram send
the bot ``/start <token>``. The bot backend (webhook in prod, the
``telegram_poll`` command in dev) routes that update through
:func:`process_update`, which resolves + consumes the token and binds the
chat. Outbound notifications go the other way via :func:`notify_via_telegram`.
"""

from __future__ import annotations

import datetime
import logging
import re
import secrets

from django.conf import settings
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.html import escape
from django.utils.translation import gettext as _

from . import client
from .models import TelegramAccount, TelegramLinkToken, TelegramMessageTemplate

_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")

logger = logging.getLogger(__name__)

LINK_TOKEN_MAX_AGE = 900  # 15 minutes


def make_link_token(user) -> str:
    """Return a stable short link token for ``user`` (reused until expiry).

    Stored in the DB (not a signed string): Telegram's ``start`` deep-link
    parameter caps at 64 chars and only allows ``[A-Za-z0-9_-]``, which a
    Django signed token violates. ``token_urlsafe(16)`` is 22 chars in the
    allowed set.

    Crucially this **reuses** the user's current non-expired token instead
    of minting a fresh one each call — the settings page re-renders the
    deep link on every status poll, and regenerating would invalidate the
    very token the user is about to tap. Only mints a new one when none is
    live.
    """
    cutoff = timezone.now() - datetime.timedelta(seconds=LINK_TOKEN_MAX_AGE)
    existing = TelegramLinkToken.objects.filter(user=user, created_at__gte=cutoff).first()
    if existing is not None:
        return existing.token
    TelegramLinkToken.objects.filter(user=user).delete()  # clear any expired rows
    token = secrets.token_urlsafe(16)
    TelegramLinkToken.objects.create(token=token, user=user)
    return token


def resolve_link_token(token: str):
    """Return the :class:`User` for a valid token and consume it, or ``None``.

    ``None`` covers an unknown or expired token (older than
    :data:`LINK_TOKEN_MAX_AGE`). On success the user's tokens are deleted
    so the link is single-use.
    """
    if not token:
        return None
    cutoff = timezone.now() - datetime.timedelta(seconds=LINK_TOKEN_MAX_AGE)
    row = TelegramLinkToken.objects.filter(token=token, created_at__gte=cutoff).select_related("user").first()
    if row is None:
        return None
    user = row.user
    TelegramLinkToken.objects.filter(user=user).delete()
    return user


def link_deep_link(user) -> str | None:
    """Return the ``t.me`` deep link for ``user`` to start the bot.

    ``None`` when the bot username isn't configured (integration off), so
    the settings template can show a "not configured" state instead.
    """
    bot = getattr(settings, "TELEGRAM_BOT_USERNAME", "")
    if not bot:
        return None
    return f"https://t.me/{bot}?start={make_link_token(user)}"


def _link_user(user, chat_id: int, username: str) -> None:
    """Bind ``chat_id`` to ``user`` (token already resolved by the caller).

    A chat maps to exactly one account: steal the chat_id from any stale
    link and (re)create this user's account. ``enabled`` resets to True so
    re-linking re-enables delivery.
    """
    TelegramAccount.objects.filter(chat_id=chat_id).exclude(user=user).delete()
    TelegramAccount.objects.update_or_create(
        user=user,
        defaults={"chat_id": chat_id, "username": username or "", "enabled": True},
    )


def _chat_language(chat_id, user=None) -> str:
    """Best-guess UI language for replies to a chat.

    Prefers the just-resolved ``user``, then any account already linked to
    this chat, then the project default. Lets bot replies match the
    member's Acta language even though the webhook has no session.
    """
    if user is not None:
        return getattr(user, "language", "") or settings.LANGUAGE_CODE
    account = TelegramAccount.objects.filter(chat_id=chat_id).select_related("user").first()
    if account is not None:
        return getattr(account.user, "language", "") or settings.LANGUAGE_CODE
    return settings.LANGUAGE_CODE


def process_update(update: dict) -> None:
    """Handle one inbound Telegram update (link / unlink commands).

    Recognises ``/start <token>`` (link this chat to the token's Acta
    user) and ``/stop`` (unlink). Everything else gets a short hint.
    Replies are rendered in the linked member's language when known, and
    sent best-effort via :mod:`apps.telegram.client`.
    """
    message = (update or {}).get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    text = (message.get("text") or "").strip()
    username = (message.get("from") or {}).get("username") or ""

    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        token = parts[1].strip() if len(parts) > 1 else ""
        linked_user = resolve_link_token(token) if token else None
        if linked_user is not None:
            _link_user(linked_user, chat_id, username)
        with translation.override(_chat_language(chat_id, linked_user)):
            if linked_user is not None:
                client.send_message(chat_id, _("✅ Linked to Acta. You'll get notifications here."))
            else:
                client.send_message(
                    chat_id,
                    _("This link is invalid or expired. Open Acta settings and start the connection again."),
                )
        return

    if text.startswith("/stop"):
        with translation.override(_chat_language(chat_id)):
            deleted, _count = TelegramAccount.objects.filter(chat_id=chat_id).delete()
            if deleted:
                client.send_message(chat_id, _("Disconnected. You won't get Acta notifications here anymore."))
        return

    with translation.override(_chat_language(chat_id)):
        client.send_message(chat_id, _("Connect this chat from Acta → Settings → Telegram."))


def _task_url(task) -> str | None:
    """Absolute URL to a task, or ``None`` when no public base URL is set."""
    base = getattr(settings, "ACTA_PUBLIC_BASE_URL", "")
    if not base or task is None:
        return None
    path = reverse("web:task_detail", kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number})
    return base.rstrip("/") + path


def _template_context(notification) -> dict:
    """Build the escaped ``{placeholder}`` values for a notification."""
    actor = notification.actor.display_name if notification.actor else _("Someone")
    task = notification.task
    slug = task.slug if task is not None else ""
    url = _task_url(task) if task is not None else None
    task_ref = (f'<a href="{url}">{escape(slug)}</a>' if url else escape(slug)) if slug else ""
    return {
        "actor": escape(actor),
        "slug": escape(slug),
        "task": task_ref,
        "title": escape(task.title) if task is not None else "",
        "preview": escape(notification.preview[:200]) if notification.preview else "",
    }


def _render_template(body: str, context: dict) -> str:
    """Substitute ``{key}`` tokens from ``context``; leave unknown ones as-is.

    Regex-based (not ``str.format``) so a stray brace or unknown placeholder
    in admin-entered text never raises.
    """
    return _PLACEHOLDER_RE.sub(lambda m: context.get(m.group(1), m.group(0)), body)


def _format_notification(notification) -> str:
    """Render a notification as a compact HTML message for Telegram.

    Uses an admin-edited :class:`TelegramMessageTemplate` for the kind when
    one exists; otherwise the built-in localized default (a bold headline,
    the task linked when a public base URL is set, and the preview).
    Assumes the caller has activated the recipient's language.
    """
    from apps.notifications.models import Notification

    custom = TelegramMessageTemplate.objects.filter(kind=notification.kind).first()
    if custom is not None and custom.body.strip():
        return _render_template(custom.body, _template_context(notification))

    actor = notification.actor.display_name if notification.actor else _("Someone")
    kind = notification.kind
    K = Notification.Kind
    if kind == K.MENTION:
        head = _("%(actor)s mentioned you") % {"actor": actor}
    elif kind == K.ASSIGNED:
        head = _("%(actor)s assigned you a task") % {"actor": actor}
    elif kind == K.COMMENT:
        head = _("%(actor)s commented") % {"actor": actor}
    elif kind == K.STATUS_CHANGE:
        head = _("%(actor)s changed a task's status") % {"actor": actor}
    elif kind == K.PRIORITY_CHANGE:
        head = _("%(actor)s changed a task's priority") % {"actor": actor}
    elif kind == K.DUE:
        head = _("Task due soon")
    elif kind == K.PROJECT_UPDATE:
        head = _("%(actor)s posted a project update") % {"actor": actor}
    elif kind == K.CYCLE:
        head = notification.payload.get("title") or _("Cycle update")
    else:
        head = _("Update in Acta")

    lines = [f"<b>{escape(head)}</b>"]
    task = notification.task
    if task is not None:
        url = _task_url(task)
        slug = escape(task.slug)
        slug_html = f'<a href="{url}">{slug}</a>' if url else slug
        lines.append(f"{slug_html} {escape(task.title)}")
    if notification.preview:
        lines.append(escape(notification.preview[:200]))
    return "\n".join(lines)


def notify_via_telegram(notification) -> bool:
    """Deliver a notification to the recipient's Telegram, if linked + enabled.

    Best-effort and silent when the recipient has no linked chat or has
    muted delivery. Renders the message in the recipient's language.
    Returns whether a message was sent.
    """
    account = (
        TelegramAccount.objects.filter(user_id=notification.recipient_id, enabled=True).select_related("user").first()
    )
    if account is None:
        return False
    if notification.kind in (account.muted_kinds or []):
        return False
    lang = getattr(account.user, "language", "") or settings.LANGUAGE_CODE
    with translation.override(lang):
        text = _format_notification(notification)
    return client.send_message(account.chat_id, text)
