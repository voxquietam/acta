"""Daily job: notify workspace members when cycles start / are ending.

For every workspace with cadence enabled it (1) materializes the rolling
cycles (``ensure_cycles`` — same call the web pages make, so this also
performs auto roll-over), then fans out two kinds of inbox notification:

* **Cycle started** — once per cycle, when it first appears as active and
  hasn't been start-notified yet.
* **Cycle ending soon** — once per cycle, when an active cycle is within
  ``CYCLE_ENDING_SOON_DAYS`` of its end and hasn't been end-notified yet.

Idempotency lives on the ``Cycle`` row (``start_notified_at`` /
``end_notified_at``), so re-running the command the same day is a no-op.
Meant for a daily cron / container scheduler (see
docs/operations.md "Recurring jobs"); the schedule is slated to move into
an admin-manageable scheduler later.

Usage::

    python manage.py notify_cycle_events
    python manage.py notify_cycle_events --dry-run
    python manage.py notify_cycle_events --workspace acme
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.cycles.models import Cycle
from apps.cycles.services import CYCLE_ENDING_SOON_DAYS, ensure_cycles, notify_cycle_ending, notify_cycle_started
from apps.workspaces.models import Workspace


class Command(BaseCommand):
    """Fan out cycle start / ending-soon notifications for active cadences."""

    help = "Notify workspace members of cycle starts and approaching ends"

    def add_arguments(self, parser):
        """Register CLI flags."""
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be sent without writing notifications or stamps",
        )
        parser.add_argument(
            "--workspace",
            type=str,
            default=None,
            help="Limit to a single workspace by slug (otherwise: all cadence-enabled workspaces)",
        )

    def handle(self, *args, **options):
        """Drive the per-workspace notification loop and report totals."""
        dry_run = options["dry_run"]
        today = timezone.localdate()
        workspaces = Workspace.objects.all()
        if options["workspace"]:
            workspaces = workspaces.filter(slug=options["workspace"])

        started_total = ending_total = 0
        for workspace in workspaces:
            if not workspace.cycle_config()["enabled"]:
                continue
            if not dry_run:
                ensure_cycles(workspace, today)

            # Cycle started: an active cycle never start-notified yet.
            for cycle in workspace.cycles.filter(status=Cycle.ACTIVE, start_notified_at__isnull=True):
                if dry_run:
                    self.stdout.write(f"[dry-run] start: {cycle}")
                    started_total += 1
                    continue
                notify_cycle_started(cycle)
                cycle.start_notified_at = timezone.now()
                cycle.save(update_fields=["start_notified_at"])
                started_total += 1

            # Cycle ending soon: active, not yet end-notified, within window.
            for cycle in workspace.cycles.filter(status=Cycle.ACTIVE, end_notified_at__isnull=True):
                if (cycle.end_date - today).days > CYCLE_ENDING_SOON_DAYS:
                    continue
                if dry_run:
                    self.stdout.write(f"[dry-run] ending: {cycle}")
                    ending_total += 1
                    continue
                notify_cycle_ending(cycle, today)
                cycle.end_notified_at = timezone.now()
                cycle.save(update_fields=["end_notified_at"])
                ending_total += 1

        self.stdout.write(
            self.style.SUCCESS(f"cycle notifications — started: {started_total}, ending: {ending_total}"),
        )
