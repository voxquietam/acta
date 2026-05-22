"""Phase 2 — assigning tasks to cycles.

Covers the ``task.cycle_changed`` activity event, the ``set_task_cycle``
web view (assign / clear / reject foreign cycle), and the bulk endpoint's
``cycle`` field.
"""

from django.urls import reverse

import pytest

from apps.cycles.tests.factories import CycleFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.bulk import _run_bulk_update
from apps.tasks.events import build_diff_events, snapshot_task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def workspace(db):
    ws = WorkspaceFactory()
    ws.cycle_settings = {"enabled": True, "length_weeks": 2, "start_date": "2026-05-04"}
    ws.save(update_fields=["cycle_settings"])
    return ws


@pytest.mark.django_db
class TestCycleChangedEvent:

    def test_assigning_cycle_emits_event(self, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        task = TaskFactory(project=project)
        old = snapshot_task(task)
        task.cycle = cycle
        task.save(update_fields=["cycle"])
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        kinds = [e.event_type for e in events]
        assert "task.cycle_changed" in kinds
        ev = next(e for e in events if e.event_type == "task.cycle_changed")
        assert ev.payload["to_cycle_id"] == cycle.id
        assert ev.payload["to_cycle_number"] == 1
        assert ev.payload["from_cycle_id"] is None

    def test_clearing_cycle_emits_event(self, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        task = TaskFactory(project=project, cycle=cycle)
        old = snapshot_task(task)
        task.cycle = None
        task.save(update_fields=["cycle"])
        events = build_diff_events(old_state=old, task=task, actor=task.reporter)
        ev = next(e for e in events if e.event_type == "task.cycle_changed")
        assert ev.payload["to_cycle_id"] is None
        assert ev.payload["from_cycle_id"] == cycle.id


@pytest.mark.django_db
class TestSetTaskCycleView:

    def _url(self, task):
        return reverse(
            "web:set_task_cycle",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )

    def test_assign_cycle(self, client, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        task = TaskFactory(project=project)
        client.force_login(workspace.owner)
        resp = client.post(self._url(task), {"cycle_id": str(cycle.id)})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.cycle_id == cycle.id

    def test_clear_cycle(self, client, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        task = TaskFactory(project=project, cycle=cycle)
        client.force_login(workspace.owner)
        resp = client.post(self._url(task), {"cycle_id": ""})
        assert resp.status_code == 200
        task.refresh_from_db()
        assert task.cycle_id is None

    def test_reject_foreign_cycle(self, client, workspace):
        project = ProjectFactory(workspace=workspace)
        other_ws = WorkspaceFactory()
        foreign = CycleFactory(workspace=other_ws, number=1)
        task = TaskFactory(project=project)
        client.force_login(workspace.owner)
        resp = client.post(self._url(task), {"cycle_id": str(foreign.id)})
        assert resp.status_code == 404
        task.refresh_from_db()
        assert task.cycle_id is None


@pytest.mark.django_db
class TestCycleRendering:

    def test_task_detail_shows_cycle_cell(self, client, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        task = TaskFactory(project=project, cycle=cycle)
        client.force_login(workspace.owner)
        resp = client.get(
            reverse("web:task_detail", kwargs={"slug_prefix": project.slug_prefix, "number": task.number}),
        )
        assert resp.status_code == 200
        assert b"Cycle" in resp.content

    def test_context_menu_renders_cycle_submenu(self, client, workspace):
        project = ProjectFactory(workspace=workspace)
        CycleFactory(workspace=workspace, number=1)
        task = TaskFactory(project=project)
        client.force_login(workspace.owner)
        resp = client.get(
            reverse("web:task_context_menu", kwargs={"slug_prefix": project.slug_prefix, "number": task.number}),
        )
        assert resp.status_code == 200
        assert b"Set cycle" in resp.content


@pytest.mark.django_db
class TestBulkCycle:

    def test_bulk_assign_cycle(self, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        WorkspaceMember.objects.get_or_create(
            user=workspace.owner,
            workspace=workspace,
            defaults={"role": WorkspaceMember.OWNER},
        )
        t1 = TaskFactory(project=project)
        t2 = TaskFactory(project=project)
        _run_bulk_update(user=workspace.owner, ids=[t1.id, t2.id], updates={"cycle": cycle.id})
        t1.refresh_from_db()
        t2.refresh_from_db()
        assert t1.cycle_id == cycle.id
        assert t2.cycle_id == cycle.id

    def test_bulk_clear_cycle(self, workspace):
        project = ProjectFactory(workspace=workspace)
        cycle = CycleFactory(workspace=workspace, number=1)
        t1 = TaskFactory(project=project, cycle=cycle)
        _run_bulk_update(user=workspace.owner, ids=[t1.id], updates={"cycle": None})
        t1.refresh_from_db()
        assert t1.cycle_id is None

    def test_bulk_reject_foreign_cycle(self, workspace):
        from rest_framework import serializers

        project = ProjectFactory(workspace=workspace)
        other_ws = WorkspaceFactory()
        foreign = CycleFactory(workspace=other_ws, number=1)
        t1 = TaskFactory(project=project)
        with pytest.raises(serializers.ValidationError):
            _run_bulk_update(user=workspace.owner, ids=[t1.id], updates={"cycle": foreign.id})
