"""Seed the default Telegram message template for each notification kind.

A fresh environment (e.g. prod after a DB wipe) starts with **no**
``TelegramMessageTemplate`` rows — the bot still works on the built-in
English defaults in ``apps.telegram.services._format_notification``, but
the admin sees an empty *Telegram message templates* list with nothing to
tune. This command fills in the agreed default body for every kind that
doesn't already have a row, so each environment converges on the same
editable starting point.

Idempotent: an existing row is left untouched (admin edits are preserved)
unless ``--overwrite`` is passed. Deploy-safe — it never raises, so it can
run unattended from the entrypoint on every container start.

    python manage.py seed_telegram_templates              # create missing only
    python manage.py seed_telegram_templates --overwrite  # reset every row to its default

The bodies mirror docs/operations.md §5 and use the ``{placeholder}``
tokens resolved by ``apps.telegram.services._template_context``.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.notifications.models import Notification
from apps.telegram.models import TelegramMessageTemplate

_K = Notification.Kind

# Default body per kind. Real newlines (not literal ``\n``) — that's what
# the renderer and the admin textarea expect. Announcement is intentionally
# absent: it keeps the built-in ``📣 {title}`` default.
DEFAULT_TEMPLATES: dict[str, str] = {
    _K.MENTION: "💬 <b>{actor}</b> mentioned you\n{task} — {title}\n{quote}",
    _K.ASSIGNED: "📌 <b>{actor}</b> assigned you a task\n{task} — {title}\n{quote}\n{meta}",
    _K.COMMENT: "🗨️ <b>{actor}</b> commented\n{task} — {title}\n{quote}",
    _K.STATUS_CHANGE: "🔄 <b>{actor}</b> moved {status_from} → {status_to}\n{task} — {title}",
    _K.PRIORITY_CHANGE: "🎚️ <b>{actor}</b> changed priority {priority_from} → {priority_to}\n{task} — {title}",
    _K.DUE: "⏰ <b>{actor}</b> changed the due date\n{task} — {title}\n{due_change}",
    _K.PROJECT_UPDATE: "📊 <b>{actor}</b> posted an update · {project}\n{health}\n{quote}",
    _K.CYCLE: "🔁 <b>{cycle}</b>\n{preview}",
}


class Command(BaseCommand):
    """Create the default Telegram template for any kind missing one."""

    help = "Seed default TelegramMessageTemplate rows for kinds that lack one."

    def add_arguments(self, parser):
        """Register the ``--overwrite`` flag."""
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Reset existing rows to their default body (discards admin edits).",
        )

    def handle(self, *args, **options):
        """Create (or optionally overwrite) one template row per kind."""
        overwrite = options["overwrite"]
        created = updated = skipped = 0
        for kind, body in DEFAULT_TEMPLATES.items():
            row, was_created = TelegramMessageTemplate.objects.get_or_create(
                kind=kind,
                defaults={"body": body},
            )
            if was_created:
                created += 1
            elif overwrite and row.body != body:
                row.body = body
                row.save(update_fields=["body"])
                updated += 1
            else:
                skipped += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Telegram templates seeded: {created} created, {updated} overwritten, {skipped} left as-is."
            )
        )
