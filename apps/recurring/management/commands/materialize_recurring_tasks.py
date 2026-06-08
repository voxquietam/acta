"""Spawn tasks from due recurring-task rules.

Meant for a daily cron / django-q schedule (see ``setup_scheduled_jobs``
and docs/operations.md "Recurring jobs"). Idempotent: a re-run on the same
day creates nothing new because each rule's cursor has already advanced and
the ``(recurrence, occurrence_date)`` uniqueness blocks duplicates.
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.recurring.services import materialize_due


class Command(BaseCommand):
    help = "Create tasks for every recurring-task rule whose occurrence is due."

    def add_arguments(self, parser):
        """Register the optional ``--date`` override (defaults to today)."""
        parser.add_argument(
            "--date",
            dest="date",
            default=None,
            help="Reference date (YYYY-MM-DD); defaults to the local current date.",
        )

    def handle(self, *args, **options):
        """Run the materializer and report how many tasks were created."""
        today = None
        if options["date"]:
            today = timezone.datetime.strptime(options["date"], "%Y-%m-%d").date()
        created = materialize_due(today)
        self.stdout.write(f"recurring: created {len(created)} task(s)")
