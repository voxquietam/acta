"""Drain queued Telegram notifications into one digest per account.

Runs every ~5 min from the django-q schedule. For each account with at
least one undelivered queued row whose owner is currently OUTSIDE the
quiet window, batches the rows into a single Telegram DM and marks them
delivered. Accounts still inside their window are left alone — the next
tick after the window closes picks them up.

Edge cases handled here (rather than in the queue producer):

* **Dedup** — same-task chatter (status flip-flop) collapses by
  ``(task_id, kind)`` to the latest row; intermediate rows are still
  marked delivered so they don't resurrect on the next tick.
* **Cap** — a long backlog (e.g. weekend silence with 50+ events)
  trims to ``DIGEST_CAP`` rows with a ``… +N more`` footer; the dropped
  rows are also marked delivered so the cap is a hard ceiling.
* **Send failure** — when the bot reply isn't OK, leave the rows
  pending and try again next tick (don't mark delivered).
"""

from collections import OrderedDict

from django.core.management.base import BaseCommand
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import override

from apps.telegram import client
from apps.telegram.models import TelegramAccount, TelegramQueuedNotification
from apps.telegram.services import _account_in_quiet_hours

DIGEST_CAP = 20


class Command(BaseCommand):
    help = "Send a batched digest to each account whose quiet window has just closed."

    def handle(self, *args, **options):
        """Walk eligible accounts and flush their queues."""
        now = timezone.now()
        candidates = (
            TelegramAccount.objects.filter(
                enabled=True,
                queued_notifications__delivered_at__isnull=True,
            )
            .select_related("user")
            .distinct()
        )
        flushed = 0
        for account in candidates:
            if _account_in_quiet_hours(account, now):
                # Still inside the window — leave the queue for the next tick.
                continue
            pending = list(
                account.queued_notifications.filter(delivered_at__isnull=True).order_by("created_at"),
            )
            if not pending:
                continue
            if _send_digest(account, pending):
                TelegramQueuedNotification.objects.filter(pk__in=[row.pk for row in pending]).update(delivered_at=now)
                flushed += 1
        self.stdout.write(f"flushed digests: {flushed}")


def _send_digest(account, pending) -> bool:
    """Render and send one combined message, return whether Telegram accepted it."""
    lang = getattr(account.user, "language", "") or ""
    with override(lang or None):
        body = _render_digest(pending)
    return client.send_message(account.chat_id, body)


def _render_digest(pending) -> str:
    """Build the digest body: header + deduped+capped event lines + footer."""
    deduped = _dedup_by_task_kind(pending)
    total = len(deduped)
    shown = deduped[:DIGEST_CAP]
    overflow = total - len(shown)
    header = _("You missed %(n)d notifications during quiet hours:") % {"n": total}
    lines = [f"<b>{header}</b>", ""]
    lines.extend(row.body for row in shown)
    if overflow > 0:
        lines.append("")
        lines.append(_("… +%(n)d more (open Acta to see them all).") % {"n": overflow})
    return "\n\n".join(lines)


def _dedup_by_task_kind(pending):
    """Collapse same-task chatter, keeping the latest row per ``(task_id, kind)``.

    Rows with no ``task_id`` (workspace announcements etc.) are not
    deduped — each one is independently meaningful. Order is preserved
    by ``OrderedDict`` keyed off insertion, then re-sorted by the kept
    row's ``created_at`` so chronological order survives the collapse.
    """
    keyed = OrderedDict()
    standalone = []
    for row in pending:
        if row.task_id is None:
            standalone.append(row)
            continue
        keyed[(row.task_id, row.kind)] = row
    merged = list(keyed.values()) + standalone
    merged.sort(key=lambda r: r.created_at)
    return merged
