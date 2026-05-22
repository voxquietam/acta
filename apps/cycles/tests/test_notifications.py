"""Cycle start / ending-soon inbox notifications + the daily command."""

import datetime

from django.core.management import call_command

import pytest

from apps.cycles.models import Cycle
from apps.cycles.services import notify_cycle_ending, notify_cycle_started
from apps.cycles.tests.factories import CycleFactory
from apps.notifications.models import Notification
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


def _enabled_ws(anchor):
    ws = WorkspaceFactory()
    ws.cycle_settings = {"enabled": True, "length_weeks": 2, "start_date": anchor}
    ws.save(update_fields=["cycle_settings"])
    return ws


@pytest.mark.django_db
class TestNotifyHelpers:

    def test_started_fans_out_to_all_members(self):
        ws = _enabled_ws("2026-05-04")
        WorkspaceMemberFactory(workspace=ws, role=WorkspaceMember.MEMBER)  # +1 besides owner
        cycle = CycleFactory(workspace=ws, number=1, status=Cycle.ACTIVE)
        created = notify_cycle_started(cycle)
        assert created == 2
        notes = Notification.objects.filter(kind=Notification.Kind.CYCLE)
        assert notes.count() == 2
        assert all(n.payload.get("event") == "started" for n in notes)
        assert all("started" in n.payload.get("title", "") for n in notes)

    def test_ending_reports_open_task_count(self):
        ws = _enabled_ws("2026-05-04")
        project = ProjectFactory(workspace=ws)
        today = datetime.date(2026, 5, 16)
        cycle = CycleFactory(
            workspace=ws,
            number=1,
            status=Cycle.ACTIVE,
            start_date=datetime.date(2026, 5, 4),
            end_date=datetime.date(2026, 5, 17),  # ends tomorrow relative to today
        )
        TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS, cycle=cycle)
        TaskFactory(project=project, status=Task.STATUS_DONE, cycle=cycle)
        notify_cycle_ending(cycle, today)
        note = Notification.objects.filter(kind=Notification.Kind.CYCLE).first()
        assert note.payload["event"] == "ending"
        assert "tomorrow" in note.payload["title"]
        assert "1" in note.preview  # one open task


@pytest.mark.django_db
class TestNotifyCommand:

    def test_start_notification_fires_once(self):
        ws = _enabled_ws(datetime.date.today().isoformat())  # cycle 1 active today
        WorkspaceMemberFactory(workspace=ws, role=WorkspaceMember.MEMBER)
        call_command("notify_cycle_events")
        active = ws.cycles.filter(status=Cycle.ACTIVE).first()
        assert active is not None
        assert active.start_notified_at is not None
        first = Notification.objects.filter(kind=Notification.Kind.CYCLE, payload__event="started").count()
        assert first == 2
        # Idempotent: a second run sends nothing new.
        call_command("notify_cycle_events")
        again = Notification.objects.filter(kind=Notification.Kind.CYCLE, payload__event="started").count()
        assert again == first

    def test_disabled_workspace_skipped(self):
        WorkspaceFactory()  # cadence off
        call_command("notify_cycle_events")
        assert Notification.objects.filter(kind=Notification.Kind.CYCLE).count() == 0
