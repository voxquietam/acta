"""Phase 3 — cycle filter axis, group-by-cycle, and the active-cycle banner."""

from django.test import RequestFactory
from django.urls import reverse

import pytest

from apps.cycles.models import Cycle
from apps.cycles.tests.factories import CycleFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web.filters import apply_task_filters
from apps.web.grouping import group_tasks
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def workspace(db):
    ws = WorkspaceFactory()
    ws.cycle_settings = {"enabled": True, "length_weeks": 2, "start_date": "2026-05-04"}
    ws.save(update_fields=["cycle_settings"])
    return ws


@pytest.mark.django_db
class TestCycleFilter:

    def _qs(self, project):
        return Task.objects.filter(project=project)

    def test_filter_by_cycle_id(self, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        in_cycle = TaskFactory(project=project, cycle=cycle)
        TaskFactory(project=project)  # backlog
        params = {"cycle": [str(cycle.id)]}
        request = RequestFactory().get("/", params)
        result = apply_task_filters(self._qs(project), request.GET, request_user=workspace.owner)
        assert list(result) == [in_cycle]

    def test_filter_backlog(self, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        TaskFactory(project=project, cycle=cycle)
        backlog = TaskFactory(project=project)
        request = RequestFactory().get("/", {"cycle": ["backlog"]})
        result = apply_task_filters(self._qs(project), request.GET, request_user=workspace.owner)
        assert list(result) == [backlog]

    def test_filter_active(self, workspace):
        project = ProjectFactory(workspace=workspace)
        active = CycleFactory(workspace=workspace, number=1, status=Cycle.ACTIVE)
        planning = CycleFactory(
            workspace=workspace,
            number=2,
            status=Cycle.PLANNING,
        )
        t_active = TaskFactory(project=project, cycle=active)
        TaskFactory(project=project, cycle=planning)
        request = RequestFactory().get("/", {"cycle": ["active"]})
        result = apply_task_filters(self._qs(project), request.GET, request_user=workspace.owner)
        assert list(result) == [t_active]


@pytest.mark.django_db
class TestGroupByCycle:

    def test_groups_active_then_backlog(self, workspace):
        project = ProjectFactory(workspace=workspace)
        active = CycleFactory(workspace=workspace, number=1, status=Cycle.ACTIVE)
        t1 = TaskFactory(project=project, cycle=active)
        t2 = TaskFactory(project=project)  # backlog
        sections = group_tasks([t1, t2], "cycle")
        keys = [s["key"] for s in sections]
        assert keys == [str(active.id), "backlog"]
        assert sections[0]["tasks"] == [t1]
        assert sections[-1]["tasks"] == [t2]


@pytest.mark.django_db
class TestCycleBanner:

    def test_banner_renders_for_single_cycle(self, client, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1, status=Cycle.ACTIVE)
        TaskFactory(project=project, cycle=cycle, status=Task.STATUS_DONE, size=3)
        TaskFactory(project=project, cycle=cycle, status=Task.STATUS_TODO, size=5)
        client.force_login(workspace.owner)
        url = reverse("web:all_tasks") + f"?cycle={cycle.id}"
        resp = client.get(url)
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "1 / 2 done" in body
