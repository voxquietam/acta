"""Phase 4 — cycle burndown / velocity metrics + the cycles dashboard page."""

import datetime

from django.urls import reverse

import pytest

from apps.activity.models import ActivityLog
from apps.cycles.models import Cycle
from apps.cycles.services import compute_cycle_burndown, compute_velocity
from apps.cycles.tests.factories import CycleFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def workspace(db):
    ws = WorkspaceFactory()
    ws.cycle_settings = {"enabled": True, "length_weeks": 2, "start_date": "2026-05-04"}
    ws.save(update_fields=["cycle_settings"])
    return ws


@pytest.mark.django_db
class TestBurndown:

    def test_burndown_shape_and_ideal(self, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(
            workspace=workspace,
            number=1,
            status=Cycle.ACTIVE,
            start_date=datetime.date(2026, 5, 4),
            end_date=datetime.date(2026, 5, 10),
        )
        TaskFactory(project=project, cycle=cycle)
        TaskFactory(project=project, cycle=cycle)
        bd = compute_cycle_burndown(cycle, today=datetime.date(2026, 5, 4))
        assert bd["total"] == 2
        assert len(bd["labels"]) == 7  # 7-day span, inclusive
        assert bd["ideal"][0] == 2
        assert bd["ideal"][-1] == 0
        # Remaining is known on day 0, None for future days.
        assert bd["remaining"][0] == 2
        assert bd["remaining"][-1] is None

    def test_burndown_counts_done_via_activity(self, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(
            workspace=workspace,
            number=1,
            status=Cycle.ACTIVE,
            start_date=datetime.date(2026, 5, 4),
            end_date=datetime.date(2026, 5, 10),
        )
        task = TaskFactory(project=project, cycle=cycle, status=Task.STATUS_DONE)
        ev = ActivityLog.objects.create(
            workspace=workspace,
            project=project,
            event_type="task.status_changed",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"from": "to-do", "to": "done"},
        )
        # ``created_at`` is auto_now_add (real now); backdate it into the
        # cycle window so the replay registers the done on day 2 (05-05).
        from django.utils import timezone

        ActivityLog.objects.filter(pk=ev.pk).update(
            created_at=timezone.make_aware(datetime.datetime(2026, 5, 5, 12, 0)),
        )
        bd = compute_cycle_burndown(cycle, today=datetime.date(2026, 5, 6))
        # Not yet done on day 0 (05-04); done by day 2 (05-06 ≥ 05-05).
        assert bd["remaining"][0] == 1
        assert bd["remaining"][2] == 0


@pytest.mark.django_db
class TestVelocity:

    def test_velocity_counts_done_per_cycle(self, workspace):
        project = ProjectFactory(workspace=workspace)
        c1 = CycleFactory(workspace=workspace, number=1, status=Cycle.COMPLETED)
        TaskFactory(project=project, cycle=c1, status=Task.STATUS_DONE, size=3)
        TaskFactory(project=project, cycle=c1, status=Task.STATUS_DONE, size=5)
        TaskFactory(project=project, cycle=c1, status=Task.STATUS_TODO)
        data = compute_velocity(workspace)
        assert data[-1]["count"] == 2
        assert data[-1]["points"] == 8


@pytest.mark.django_db
class TestCyclesDashboardPage:

    def test_page_renders_with_active_cycle(self, client, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1, status=Cycle.ACTIVE)
        TaskFactory(project=project, cycle=cycle)
        client.force_login(workspace.owner)
        resp = client.get(reverse("web:cycles_overview"))
        assert resp.status_code == 200
        assert b"burndownChart" in resp.content

    def test_empty_state_when_cadence_off(self, client, db):
        ws = WorkspaceFactory()
        client.force_login(ws.owner)
        resp = client.get(reverse("web:cycles_overview"))
        assert resp.status_code == 200
        assert b"Cycles are off" in resp.content
