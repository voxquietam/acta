import datetime

import pytest

from apps.meetings.models import Meeting
from apps.meetings.tests.factories import MeetingFactory
from apps.tasks.tests.factories import TaskFactory


@pytest.mark.django_db
class TestMeetingModel:
    """Model-level behavior for :class:`apps.meetings.models.Meeting`."""

    def test_str_includes_title_and_datetime(self):
        meeting = MeetingFactory(title="Sync with client")
        assert "Sync with client" in str(meeting)
        assert "2026-06-01 14:00" in str(meeting)

    @pytest.mark.parametrize(
        ("minutes", "expected"),
        [
            (45, "45m"),
            (60, "1h"),
            (90, "1h 30m"),
            (125, "2h 5m"),
        ],
    )
    def test_human_duration(self, minutes, expected):
        meeting = MeetingFactory(duration_minutes=minutes)
        assert meeting.human_duration() == expected

    def test_was_edited_false_on_create(self):
        meeting = MeetingFactory()
        assert meeting.was_edited is False

    def test_was_edited_true_after_edit(self):
        meeting = MeetingFactory()
        meeting.created_at = meeting.updated_at - datetime.timedelta(minutes=5)
        assert meeting.was_edited is True

    def test_links_tasks_and_surfaces_on_task(self):
        meeting = MeetingFactory()
        task = TaskFactory(project=meeting.project)
        meeting.tasks.add(task)
        assert task.meetings.get() == meeting

    def test_workspace_minutes_rollup_counts_meeting_rows_not_task_links(self):
        """A meeting linked to N tasks counts once toward workspace totals.

        Guards the design rule: aggregate time is summed over ``Meeting``
        rows, never by summing the rollup across linked tasks (which would
        multiply the duration by the number of links).
        """
        meeting = MeetingFactory(duration_minutes=60)
        tasks = [TaskFactory(project=meeting.project) for _ in range(3)]
        meeting.tasks.add(*tasks)

        per_task_sum = sum(t.meetings.get().duration_minutes for t in tasks)
        workspace_total = sum(m.duration_minutes for m in Meeting.objects.filter(workspace=meeting.workspace))

        assert per_task_sum == 180
        assert workspace_total == 60
