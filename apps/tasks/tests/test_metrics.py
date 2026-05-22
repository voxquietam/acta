"""Flow metrics computed from the activity log."""

import datetime

from django.utils import timezone

import pytest

from apps.activity.models import ActivityLog
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.metrics import compute_flow_metrics
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
def test_insights_page_renders(client):
    from django.urls import reverse

    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    client.force_login(ws.owner)
    resp = client.get(reverse("web:project_insights", kwargs={"slug_prefix": project.slug_prefix}))
    assert resp.status_code == 200
    assert b"Insights" in resp.content
