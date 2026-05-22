"""Flow metrics computed from the activity log."""

import datetime

from django.utils import timezone

import pytest

from apps.activity.models import ActivityLog
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.metrics import compute_bottlenecks, compute_cfd, compute_flow_metrics
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


def _status_event(ws, project, task, to_status, when):
    """Create a backdated ``task.status_changed`` event (created_at is auto)."""
    e = ActivityLog.objects.create(
        workspace=ws,
        project=project,
        target_type=ActivityLog.TARGET_TASK,
        target_id=task.id,
        event_type="task.status_changed",
        payload={"to": to_status},
    )
    ActivityLog.objects.filter(pk=e.pk).update(created_at=when)


@pytest.mark.django_db
class TestFlowMetrics:
    def test_cycle_and_lead_and_throughput(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, status=Task.STATUS_DONE, reporter=ws.owner)
        now = timezone.now()
        Task.objects.filter(pk=task.pk).update(created_at=now - datetime.timedelta(days=5))
        _status_event(ws, project, task, Task.STATUS_IN_PROGRESS, now - datetime.timedelta(days=3))
        _status_event(ws, project, task, Task.STATUS_DONE, now - datetime.timedelta(days=1))

        m = compute_flow_metrics(project, weeks=8)
        assert m["completed_count"] == 1
        assert 47 <= m["cycle_median"] <= 49  # in-progress(3d ago) → done(1d ago) ≈ 48h
        assert 95 <= m["lead_median"] <= 97  # created(5d ago) → done(1d ago) ≈ 96h
        assert sum(p["count"] for p in m["throughput"]) == 1

    def test_reopened_task_excluded(self):
        """A task currently NOT done (reopened) drops out of the sample."""
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, status=Task.STATUS_TODO, reporter=ws.owner)
        now = timezone.now()
        _status_event(ws, project, task, Task.STATUS_DONE, now - datetime.timedelta(days=2))
        _status_event(ws, project, task, Task.STATUS_TODO, now - datetime.timedelta(days=1))
        m = compute_flow_metrics(project, weeks=8)
        assert m["completed_count"] == 0

    def test_no_data_is_empty(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        m = compute_flow_metrics(project, weeks=8)
        assert m["completed_count"] == 0
        assert m["cycle_median"] is None
        assert m["lead_median"] is None


@pytest.mark.django_db
class TestCfdAndBottlenecks:
    def test_cfd_reconstructs_status_over_time(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS, reporter=ws.owner)
        now = timezone.now()
        Task.objects.filter(pk=task.pk).update(created_at=now - datetime.timedelta(days=10))
        # to-do (initial) → in-progress 2 days ago
        _status_event(ws, project, task, Task.STATUS_IN_PROGRESS, now - datetime.timedelta(days=2))
        # backfill the 'from' so the initial status reconstructs as to-do
        ActivityLog.objects.filter(target_id=task.id).update(payload={"from": "to-do", "to": "in-progress"})

        cfd = compute_cfd(project, weeks=2)
        # last day: the task is in-progress → that band has the task
        assert cfd["series"]["in-progress"][-1] == 1
        assert cfd["series"]["to-do"][-1] == 0

    def test_bottlenecks_time_in_status_and_reopen(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, status=Task.STATUS_TODO, reporter=ws.owner)
        now = timezone.now()
        # to-do → in-progress (3d ago) → done (1d ago) → to-do (12h ago) = reopen
        _status_event(ws, project, task, Task.STATUS_IN_PROGRESS, now - datetime.timedelta(days=3))
        ActivityLog.objects.filter(target_id=task.id).update(payload={"from": "to-do", "to": "in-progress"})
        _status_event(ws, project, task, Task.STATUS_DONE, now - datetime.timedelta(days=1))
        _status_event(ws, project, task, Task.STATUS_TODO, now - datetime.timedelta(hours=12))
        # fix the from on the latter two (update above clobbered all rows — re-set per row)
        rows = list(ActivityLog.objects.filter(target_id=task.id).order_by("created_at"))
        for row, payload in zip(
            rows,
            [
                {"from": "to-do", "to": "in-progress"},
                {"from": "in-progress", "to": "done"},
                {"from": "done", "to": "to-do"},
            ],
        ):
            ActivityLog.objects.filter(pk=row.pk).update(payload=payload)

        b = compute_bottlenecks(project, weeks=8)
        # in-progress segment ≈ 2 days (3d→1d ago) → ~48h
        assert 46 <= b["time_in_status"]["in-progress"] <= 50
        # one done→to-do transition over one completion → 100% reopen
        assert b["reopen_rate"] == 100.0

    def test_reopen_rate_capped_at_100(self):
        """Reopens of pre-window completions must not push the rate >100%.

        Counts distinct tasks completed-then-reopened IN the window, not
        raw done→away transitions (which inflated past 100%).
        """
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        now = timezone.now()

        def _ev(task, frm, to, when):
            e = ActivityLog.objects.create(
                workspace=ws,
                project=project,
                target_type=ActivityLog.TARGET_TASK,
                target_id=task.id,
                event_type="task.status_changed",
                payload={"from": frm, "to": to},
            )
            ActivityLog.objects.filter(pk=e.pk).update(created_at=when)

        # Task A: completed AND reopened inside the window → counts once.
        a = TaskFactory(project=project, status=Task.STATUS_TODO, reporter=ws.owner)
        _ev(a, "in-progress", "done", now - datetime.timedelta(weeks=2))
        _ev(a, "done", "to-do", now - datetime.timedelta(weeks=1))
        # Task B: completed BEFORE the window, only its reopen lands inside.
        # Old logic counted this reopen with no matching completion → >100%.
        b_task = TaskFactory(project=project, status=Task.STATUS_TODO, reporter=ws.owner)
        _ev(b_task, "in-progress", "done", now - datetime.timedelta(weeks=10))
        _ev(b_task, "done", "to-do", now - datetime.timedelta(weeks=1))

        b = compute_bottlenecks(project, weeks=8)
        # Only Task A completed in-window and was reopened → 1/1 = 100%.
        assert b["reopen_rate"] == 100.0
        assert b["reopen_rate"] <= 100.0


@pytest.mark.django_db
def test_insights_page_renders(client):
    from django.urls import reverse

    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    client.force_login(ws.owner)
    resp = client.get(reverse("web:project_insights", kwargs={"slug_prefix": project.slug_prefix}))
    assert resp.status_code == 200
    assert b"Insights" in resp.content
