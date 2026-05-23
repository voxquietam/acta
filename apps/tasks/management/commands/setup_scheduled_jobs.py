"""Seed the recurring django-q schedules (idempotent).

Run once per environment after ``migrate`` (it's in the deploy checklist).
Creates a daily schedule for each maintenance job if it doesn't already
exist; existing schedules are left untouched, so re-running on every deploy
never clobbers timings an admin has since edited in the UI.
"""

import datetime

from django.core.management.base import BaseCommand
from django.utils import timezone

# (schedule name, dotted callable, (hour, minute) of the daily run).
_JOBS = [
    ("archive stale done tasks", "apps.common.scheduled.archive_stale_done_tasks", (3, 30)),
    ("gc orphan attachments", "apps.common.scheduled.gc_orphan_attachments", (4, 0)),
    ("notify cycle events", "apps.common.scheduled.notify_cycle_events", (6, 0)),
]


class Command(BaseCommand):
    help = "Create the daily Django-Q schedules for recurring jobs (idempotent)."

    def handle(self, *args, **options):
        """Create any missing daily schedule, reporting what was added."""
        from django_q.models import Schedule

        now = timezone.localtime()
        for name, func, (hour, minute) in _JOBS:
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += datetime.timedelta(days=1)
            _schedule, created = Schedule.objects.get_or_create(
                name=name,
                defaults={
                    "func": func,
                    "schedule_type": Schedule.DAILY,
                    "repeats": -1,
                    "next_run": next_run,
                },
            )
            verb = "created" if created else "exists"
            self.stdout.write(f"{verb}: {name} (daily ~{hour:02d}:{minute:02d})")
