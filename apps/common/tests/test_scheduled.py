"""Scheduled-job wrappers + the ``setup_scheduled_jobs`` seeding command."""

from unittest.mock import patch

from django.core.management import call_command

import pytest

from apps.common import scheduled


class TestScheduledWrappers:
    @pytest.mark.parametrize(
        "func, command",
        [
            (scheduled.archive_stale_done_tasks, "archive_stale_done_tasks"),
            (scheduled.gc_orphan_attachments, "gc_orphan_attachments"),
            (scheduled.notify_cycle_events, "notify_cycle_events"),
        ],
    )
    def test_wrapper_calls_its_command(self, func, command):
        with patch("apps.common.scheduled.call_command") as mock:
            func()
        mock.assert_called_once_with(command)


@pytest.mark.django_db
class TestSetupScheduledJobs:
    def test_seeds_three_daily_schedules_idempotently(self):
        from django_q.models import Schedule

        call_command("setup_scheduled_jobs")
        assert set(Schedule.objects.values_list("func", flat=True)) == {
            "apps.common.scheduled.archive_stale_done_tasks",
            "apps.common.scheduled.gc_orphan_attachments",
            "apps.common.scheduled.notify_cycle_events",
        }
        assert all(s.schedule_type == Schedule.DAILY for s in Schedule.objects.all())
        # re-running must not duplicate
        call_command("setup_scheduled_jobs")
        assert Schedule.objects.count() == 3
