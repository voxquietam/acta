"""Delete SSE events too old for any client to still be catching up on.

``django_eventstream`` persists every broadcast so a browser that
reconnects can replay what it missed, which it asks for by
``Last-Event-ID``. Nothing ever deletes those rows, so the table only
grows — and none of it is read after the window in which a reconnect is
plausible.

Pruning past a client's last id makes the server answer
``stream-reset`` instead of the missed events. The browser handles that
now — it refetches the page rather than trusting a DOM it can no longer
account for — so the week here is comfort, not a load-bearing margin.
It can come down if the table ever becomes a cost.

    docker compose exec -T web python manage.py prune_event_stream --dry-run
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = "Delete persisted SSE events older than the retention window."

    def add_arguments(self, parser):
        """Register the retention window and the dry-run switch.

        Args:
            parser: The command's argument parser.
        """
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=7,
            help="Delete events written more than this many days ago (default 7).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be deleted without deleting it.",
        )

    def handle(self, *args, **options):
        """Delete the aged-out events and report the count.

        Args:
            *args: Unused.
            **options: ``older_than_days`` and ``dry_run``.
        """
        from django_eventstream.models import Event

        days = options["older_than_days"]
        cutoff = timezone.now() - timedelta(days=days)
        stale = Event.objects.filter(created__lt=cutoff)
        count = stale.count()
        if options["dry_run"]:
            self.stdout.write(f"would delete {count} event(s) older than {days} day(s)")
            return
        stale.delete()
        self.stdout.write(f"deleted {count} event(s) older than {days} day(s)")
