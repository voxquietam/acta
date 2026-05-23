"""Schedulable wrappers around the recurring management commands.

django-q2 schedules target an importable callable by dotted path, so each
recurring maintenance command gets a thin wrapper here. Seed the schedules
with ``manage.py setup_scheduled_jobs``; edit their timing in the admin
(Django Q → Scheduled tasks). See docs/operations.md.
"""

from django.core.management import call_command


def archive_stale_done_tasks() -> None:
    """Archive done tasks past their workspace's auto-archive threshold."""
    call_command("archive_stale_done_tasks")


def gc_orphan_attachments() -> None:
    """Delete attachment files no longer referenced by any record."""
    call_command("gc_orphan_attachments")


def notify_cycle_events() -> None:
    """Fan out cycle start / ending-soon notifications for the day."""
    call_command("notify_cycle_events")
