"""Ready buffer status: a groomed backlog stage that carries no cycle."""

from django.urls import reverse

import pytest

from apps.cycles.services import apply_cycle_policy, current_cycle, ensure_cycles
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


def test_ready_is_a_kanban_column_after_planned():
    assert Task.STATUS_READY in Task.KANBAN_STATUS_VALUES
    order = list(Task.KANBAN_STATUS_VALUES)
    assert order.index(Task.STATUS_READY) == order.index(Task.STATUS_PLANNED) + 1
    assert order.index(Task.STATUS_READY) < order.index(Task.STATUS_TODO)


@pytest.mark.django_db
class TestReadyCarriesNoCycle:

    def test_policy_clears_cycle_on_ready(self, workspace):
        project = ProjectFactory(workspace=workspace)
        ensure_cycles(workspace)
        active = current_cycle(workspace)
        task = TaskFactory(project=project, status=Task.STATUS_TODO, cycle=active)
        task.status = Task.STATUS_READY
        changed = apply_cycle_policy(task)
        assert changed is True
        assert task.cycle_id is None

    def test_ready_does_not_auto_assign_cycle(self, workspace):
        project = ProjectFactory(workspace=workspace)
        ensure_cycles(workspace)
        task = TaskFactory(project=project, status=Task.STATUS_PLANNED)
        task.status = Task.STATUS_READY
        assert apply_cycle_policy(task) is False
        assert task.cycle_id is None

    def test_set_task_cycle_rejects_ready(self, client, workspace):
        from apps.cycles.tests.factories import CycleFactory

        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        task = TaskFactory(project=project, status=Task.STATUS_READY)
        client.force_login(workspace.owner)
        resp = client.post(
            reverse("web:set_task_cycle", kwargs={"slug_prefix": project.slug_prefix, "number": task.number}),
            {"cycle_id": str(cycle.id)},
        )
        assert resp.status_code == 400
        task.refresh_from_db()
        assert task.cycle_id is None

    def test_moving_ready_to_todo_assigns_cycle(self, client, workspace):
        project = ProjectFactory(workspace=workspace)
        ensure_cycles(workspace)
        active = current_cycle(workspace)
        task = TaskFactory(project=project, status=Task.STATUS_READY)
        client.force_login(workspace.owner)
        client.post(
            reverse("web:set_task_status", kwargs={"slug_prefix": project.slug_prefix, "number": task.number}),
            {"status": Task.STATUS_TODO},
        )
        task.refresh_from_db()
        assert task.cycle_id == active.id
