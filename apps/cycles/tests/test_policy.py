"""Status-driven cycle policy: planned = backlog, committed = active cycle."""

from django.urls import reverse

import pytest

from apps.cycles.models import Cycle
from apps.cycles.services import apply_cycle_policy, current_cycle, ensure_cycles
from apps.cycles.tests.factories import CycleFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.bulk import _run_bulk_update
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def workspace(db):
    ws = WorkspaceFactory()
    ws.cycle_settings = {"enabled": True, "length_weeks": 2, "start_date": "2026-05-04"}
    ws.save(update_fields=["cycle_settings"])
    return ws


@pytest.fixture
def active_cycle(workspace):
    """The workspace's REAL active cycle for today (materialized).

    Using the rolling logic (not a fixed-date factory row) so it matches
    what ``apply_cycle_policy`` resolves via ``current_cycle``.
    """
    ensure_cycles(workspace)
    return current_cycle(workspace)


@pytest.mark.django_db
class TestApplyCyclePolicy:

    def test_committed_status_assigns_active_cycle(self, workspace, active_cycle):
        project = ProjectFactory(workspace=workspace)
        task = TaskFactory(project=project, status=Task.STATUS_PLANNED)
        task.status = Task.STATUS_TODO
        changed = apply_cycle_policy(task)
        assert changed is True
        assert task.cycle_id == active_cycle.id

    def test_planned_clears_cycle(self, workspace, active_cycle):
        project = ProjectFactory(workspace=workspace)
        task = TaskFactory(project=project, status=Task.STATUS_TODO, cycle=active_cycle)
        task.status = Task.STATUS_PLANNED
        changed = apply_cycle_policy(task)
        assert changed is True
        assert task.cycle_id is None

    def test_does_not_override_existing_cycle(self, workspace, active_cycle):
        other = CycleFactory(workspace=workspace, number=9, status=Cycle.PLANNING)
        project = ProjectFactory(workspace=workspace)
        task = TaskFactory(project=project, status=Task.STATUS_TODO, cycle=other)
        task.status = Task.STATUS_IN_PROGRESS
        changed = apply_cycle_policy(task)
        assert changed is False
        assert task.cycle_id == other.id

    def test_noop_when_cadence_off(self, db):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, status=Task.STATUS_TODO)
        assert apply_cycle_policy(task) is False
        assert task.cycle_id is None


@pytest.mark.django_db
class TestStatusViewPolicy:

    def _status_url(self, task):
        return reverse(
            "web:set_task_status",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )

    def test_move_to_todo_assigns_cycle(self, client, workspace, active_cycle):
        project = ProjectFactory(workspace=workspace)
        task = TaskFactory(project=project, status=Task.STATUS_PLANNED)
        client.force_login(workspace.owner)
        client.post(self._status_url(task), {"status": Task.STATUS_TODO})
        task.refresh_from_db()
        assert task.cycle_id == active_cycle.id

    def test_move_to_planned_clears_cycle(self, client, workspace, active_cycle):
        project = ProjectFactory(workspace=workspace)
        task = TaskFactory(project=project, status=Task.STATUS_TODO, cycle=active_cycle)
        client.force_login(workspace.owner)
        client.post(self._status_url(task), {"status": Task.STATUS_PLANNED})
        task.refresh_from_db()
        assert task.cycle_id is None

    def test_cannot_assign_cycle_to_planned(self, client, workspace, active_cycle):
        project = ProjectFactory(workspace=workspace)
        task = TaskFactory(project=project, status=Task.STATUS_PLANNED)
        client.force_login(workspace.owner)
        url = reverse(
            "web:set_task_cycle",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )
        resp = client.post(url, {"cycle_id": str(active_cycle.id)})
        assert resp.status_code == 400
        task.refresh_from_db()
        assert task.cycle_id is None


@pytest.mark.django_db
class TestBulkPolicy:

    def test_bulk_to_todo_assigns_cycle(self, workspace, active_cycle):
        project = ProjectFactory(workspace=workspace)
        t1 = TaskFactory(project=project, status=Task.STATUS_PLANNED)
        t2 = TaskFactory(project=project, status=Task.STATUS_PLANNED)
        _run_bulk_update(user=workspace.owner, ids=[t1.id, t2.id], updates={"status": Task.STATUS_TODO})
        t1.refresh_from_db()
        t2.refresh_from_db()
        assert t1.cycle_id == active_cycle.id
        assert t2.cycle_id == active_cycle.id

    def test_bulk_to_planned_clears_cycle(self, workspace, active_cycle):
        project = ProjectFactory(workspace=workspace)
        t1 = TaskFactory(project=project, status=Task.STATUS_TODO, cycle=active_cycle)
        _run_bulk_update(user=workspace.owner, ids=[t1.id], updates={"status": Task.STATUS_PLANNED})
        t1.refresh_from_db()
        assert t1.cycle_id is None

    def test_bulk_cycle_set_skips_planned(self, workspace, active_cycle):
        project = ProjectFactory(workspace=workspace)
        planned = TaskFactory(project=project, status=Task.STATUS_PLANNED)
        todo = TaskFactory(project=project, status=Task.STATUS_TODO)
        _run_bulk_update(
            user=workspace.owner,
            ids=[planned.id, todo.id],
            updates={"cycle": active_cycle.id},
        )
        planned.refresh_from_db()
        todo.refresh_from_db()
        assert planned.cycle_id is None
        assert todo.cycle_id == active_cycle.id
