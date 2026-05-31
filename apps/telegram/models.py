from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class TelegramAccount(models.Model):
    """A user's linked Telegram chat — the delivery target for bot DMs.

    Created when the user completes the link flow (opens the bot via a
    signed deep-link token, which the webhook resolves to this user). One
    Telegram chat per user. Outbound notifications send to ``chat_id``;
    ``enabled`` is the per-user on/off the notification fan-out checks.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="telegram",
        help_text="Acta user this Telegram chat is linked to",
    )
    chat_id = models.BigIntegerField(
        unique=True,
        help_text="Telegram chat id the bot sends messages to; unique per linked user",
    )
    username = models.CharField(
        max_length=64,
        blank=True,
        help_text="Telegram @username at link time, for display. Optional (users may have none)",
    )
    enabled = models.BooleanField(
        default=True,
        help_text="Master switch for delivery to this chat; user toggle, defaults on when linked",
    )
    muted_kinds = models.JSONField(
        default=list,
        blank=True,
        help_text="Notification kinds (Notification.Kind values) NOT delivered here; empty = all kinds sent",
    )
    quiet_hours_enabled = models.BooleanField(
        default=False,
        help_text=(
            "When on, notifications fired inside the start/end window are queued instead of "
            "delivered live; one consolidated digest goes out the first scheduler tick after "
            "the window closes"
        ),
    )
    quiet_hours_start = models.TimeField(
        null=True,
        blank=True,
        help_text="Local time the quiet window opens (e.g. 18:00). Server TZ for MVP; per-user TZ deferred",
    )
    quiet_hours_end = models.TimeField(
        null=True,
        blank=True,
        help_text=(
            "Local time the quiet window closes (e.g. 08:00). When end < start the window wraps " "past midnight"
        ),
    )
    linked_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the account was linked",
    )

    class Meta:
        verbose_name = _("Telegram account")
        verbose_name_plural = _("Telegram accounts")

    def __str__(self) -> str:
        """Return the linked user and Telegram handle."""
        handle = f"@{self.username}" if self.username else self.chat_id
        return f"{self.user} ↔ {handle}"


class TelegramQueuedNotification(models.Model):
    """A notification deferred by quiet hours, awaiting the next digest.

    Created by ``notify_via_telegram`` when the recipient's window is open;
    drained by the ``flush_telegram_quiet_digests`` scheduler job that
    runs the first tick after the window closes. Body is rendered + frozen
    at queue time so the digest reflects what the user would have seen
    live — later edits to the source notification don't rewrite history.
    """

    account = models.ForeignKey(
        TelegramAccount,
        on_delete=models.CASCADE,
        related_name="queued_notifications",
        help_text="Account whose quiet hours queued this row",
    )
    kind = models.CharField(
        max_length=20,
        help_text="Notification kind (Notification.Kind value); kept for (task_id, kind) dedupe",
    )
    task_id = models.IntegerField(
        null=True,
        blank=True,
        help_text=(
            "Source task id when the notification was task-scoped; null otherwise. Same-task "
            "chatter is de-duplicated by (task_id, kind), keeping the latest"
        ),
    )
    body = models.TextField(
        help_text="Rendered Telegram message body frozen at queue time (HTML, bot's parse_mode)",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the row was queued; also drives chronological order in the digest",
    )
    delivered_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the digest including this row went out; null while still pending",
    )

    class Meta:
        verbose_name = _("Telegram queued notification")
        verbose_name_plural = _("Telegram queued notifications")
        ordering = [
            "created_at",
        ]
        indexes = [
            models.Index(
                fields=[
                    "account",
                    "delivered_at",
                ],
            ),
        ]

    def __str__(self) -> str:
        """Return the account and queue timestamp for admin readability."""
        return f"{self.account} · queued {self.created_at:%Y-%m-%d %H:%M}"


class TelegramMessageTemplate(models.Model):
    """Admin-editable wording for the Telegram DM of one notification kind.

    When a row exists for a kind, its ``body`` (with ``{placeholder}``
    tokens) replaces the built-in default text for that kind's outbound
    message. Kinds without a row keep the localized default. Lets an admin
    tune phrasing without code. See
    :func:`apps.telegram.services._format_notification`.

    Available placeholders: ``{actor}`` (who triggered it), ``{task}``
    (task ref, linked when a public URL is set), ``{slug}`` (plain task
    ref), ``{title}`` (task title), ``{preview}`` (the short snippet).
    Unknown placeholders are left as-is. Basic HTML is allowed (``<b>``,
    ``<a>``) — Telegram parses it; placeholder values are auto-escaped.
    """

    kind = models.CharField(
        max_length=20,
        unique=True,
        help_text="Notification kind this template applies to (one row per kind)",
    )
    body = models.TextField(
        help_text=(
            "Message text with {placeholder} tokens: {actor} {task} {slug} {title} {preview}. "
            "Empty falls back to the built-in default for this kind"
        ),
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="When the template was last edited",
    )

    class Meta:
        verbose_name = _("Telegram message template")
        verbose_name_plural = _("Telegram message templates")

    def __str__(self) -> str:
        """Return the kind this template customises."""
        return f"telegram template · {self.kind}"

    def clean(self) -> None:
        """Reject ``{tokens}`` not recognised by the renderer.

        Without this, an admin typo (``{statua}`` instead of ``{status}``)
        survives the form and reaches a real DM as the literal text. The
        renderer leaves unknown tokens as-is by design (safer than
        crashing in fan-out), so the only guard is at save time. Compares
        every captured token against
        :data:`apps.telegram.services.KNOWN_PLACEHOLDERS`.

        Raises:
            ValidationError: If ``body`` contains an unknown token.
        """
        from django.core.exceptions import ValidationError

        from .services import _PLACEHOLDER_RE, KNOWN_PLACEHOLDERS

        unknown = sorted(
            {token for token in _PLACEHOLDER_RE.findall(self.body or "") if token not in KNOWN_PLACEHOLDERS},
        )
        if unknown:
            joined = ", ".join("{" + t + "}" for t in unknown)
            raise ValidationError(
                {
                    "body": _("Unknown placeholder(s): %(tokens)s. Available: %(known)s.")
                    % {"tokens": joined, "known": ", ".join("{" + k + "}" for k in sorted(KNOWN_PLACEHOLDERS))},
                },
            )


class TelegramLinkToken(models.Model):
    """A short one-use token that ties a ``/start`` deep link to a user.

    Telegram's ``start`` deep-link parameter is capped at 64 characters
    and only allows ``[A-Za-z0-9_-]`` — too tight for a Django signed
    token (long, contains ``:``). So the token is a short random string
    stored here, resolved + consumed when the bot receives ``/start
    <token>``. Short-lived (see ``LINK_TOKEN_MAX_AGE``) and single-use.
    """

    token = models.CharField(
        max_length=64,
        unique=True,
        help_text="Random URL-safe token embedded in the t.me start deep link",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="telegram_link_tokens",
        help_text="User the token links the next /start to",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the token was minted; resolution rejects it past the TTL",
    )

    class Meta:
        verbose_name = _("Telegram link token")
        verbose_name_plural = _("Telegram link tokens")

    def __str__(self) -> str:
        """Return the target user (token value is a secret, not shown)."""
        return f"link token → {self.user}"
