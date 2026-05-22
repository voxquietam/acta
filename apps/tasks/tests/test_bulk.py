"""End-to-end behaviour of the bulk endpoint helpers.

Exercises ``_run_bulk_update`` and ``_run_bulk_delete`` directly because
they encapsulate the actual logic; the DRF view layer is a thin wrapper.
"""

import pytest
from rest_framework import serializers

from apps.activity.models import ActivityLog
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.bulk import _run_bulk_delete, _run_bulk_update
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


def _seed_workspace_with_tasks(count=3, user=None):
    """Create a workspace, a project, ``count`` tasks, and member ``user``.

    Args:
        count: Number of tasks to create in the seeded project.
        user: User to grant member access. A fresh one is created if
            omitted.

    Returns:
        ``(workspace, project, user, [tasks])`` tuple.
    """
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    if user is None:
        user = ws.owner
    else:
        WorkspaceMemberFactory(user=user, workspace=ws, role=WorkspaceMember.MEMBER)
    tasks = [TaskFactory(project=project, reporter=user) for _ in range(count)]
    return ws, project, user, tasks


@pytest.mark.django_db
class TestBulkUpdateScalars:
    """Scalar field updates apply atomically and emit granular events."""

    def test_status_change_on_batch(self):
        ws, project, user, tasks = _seed_workspace_with_tasks(3)
        ids = [t.id for t in tasks]
        bulk_id, count = _run_bulk_update(
            user=user,
            ids=ids,
            updates={"status": Task.STATUS_IN_PROGRESS},
        )
        assert count == 3
        for t in tasks:
            t.refresh_from_db()
            assert t.status == Task.STATUS_IN_PROGRESS
        events = ActivityLog.objects.filter(bulk_id=bulk_id)
        assert events.count() == 3
        assert set(events.values_list("event_type", flat=True)) == {"task.status_changed"}

    def test_bulk_id_shared_across_events(self):
        ws, project, user, tasks = _seed_workspace_with_tasks(2)
        ids = [t.id for t in tasks]
        bulk_id, _ = _run_bulk_update(
            user=user,
            ids=ids,
            updates={"priority": Task.URGENT, "status": Task.STATUS_DONE},
        )
        bids = ActivityLog.objects.filter(bulk_id=bulk_id).values_list("bulk_id", flat=True)
        assert set(bids) == {bulk_id}
        # 2 tasks × 2 changed fields = 4 events.
        assert len(bids) == 4


@pytest.mark.django_db
class TestBulkUpdatePermissions:
    """Inaccessible IDs in the batch reject the whole call without changes."""

    def test_inaccessible_id_raises_permission_error(self):
        _, _, user, my_tasks = _seed_workspace_with_tasks(2)
        _, _, _, foreign_tasks = _seed_workspace_with_tasks(1)
        ids = [my_tasks[0].id, foreign_tasks[0].id]
        with pytest.raises(PermissionError):
            _run_bulk_update(
                user=user,
                ids=ids,
                updates={"status": Task.STATUS_DONE},
            )
        # Nothing changed.
        my_tasks[0].refresh_from_db()
        foreign_tasks[0].refresh_from_db()
        assert my_tasks[0].status == Task.STATUS_TODO
        assert foreign_tasks[0].status == Task.STATUS_TODO

    def test_no_events_on_rejected_batch(self):
        _, _, user, my_tasks = _seed_workspace_with_tasks(1)
        _, _, _, foreign_tasks = _seed_workspace_with_tasks(1)
        before = ActivityLog.objects.count()
        with pytest.raises(PermissionError):
            _run_bulk_update(
                user=user,
                ids=[my_tasks[0].id, foreign_tasks[0].id],
                updates={"priority": Task.HIGH},
            )
        assert ActivityLog.objects.count() == before


@pytest.mark.django_db
class TestBulkUpdateLabels:
    """Labels: add/remove through-table writes, plus workspace check."""

    def test_label_add(self):
        _, project, user, tasks = _seed_workspace_with_tasks(2)
        label = LabelFactory(workspace=project.workspace)
        bulk_id, _ = _run_bulk_update(
            user=user,
            ids=[t.id for t in tasks],
            updates={"labels_add": [label.id]},
        )
        for t in tasks:
            assert label in t.labels.all()
        # One labels_changed event per task.
        evt_types = ActivityLog.objects.filter(bulk_id=bulk_id).values_list("event_type", flat=True)
        assert list(evt_types) == ["task.labels_changed"] * 2

    def test_foreign_workspace_label_rejected(self):
        _, project, user, tasks = _seed_workspace_with_tasks(1)
        other = LabelFactory()
        with pytest.raises(serializers.ValidationError):
            _run_bulk_update(
                user=user,
                ids=[tasks[0].id],
                updates={"labels_add": [other.id]},
            )


@pytest.mark.django_db
class TestBulkProjectMove:
    """``updates.project`` moves tasks, cascades subtasks, clears parent."""

    def test_move_within_workspace_renumbers(self):
        ws = WorkspaceFactory()
        src = ProjectFactory(workspace=ws)
        dst = ProjectFactory(workspace=ws)
        user = ws.owner
        a = TaskFactory(project=src, reporter=user)
        bulk_id, count = _run_bulk_update(
            user=user,
            ids=[a.id],
            updates={"project": dst.id},
        )
        a.refresh_from_db()
        assert a.project_id == dst.id
        assert a.number == 1
        # A dedicated event captures the project move (from/to ids + slugs).
        evt = ActivityLog.objects.get(bulk_id=bulk_id)
        assert evt.event_type == "task.project_changed"
        assert evt.payload["from_project_id"] == src.id
        assert evt.payload["to_project_id"] == dst.id

    def test_top_level_move_cascades_subtasks(self):
        ws = WorkspaceFactory()
        src = ProjectFactory(workspace=ws)
        dst = ProjectFactory(workspace=ws)
        user = ws.owner
        parent = TaskFactory(project=src, reporter=user)
        child = TaskFactory(project=src, parent=parent, reporter=user)
        bulk_id, count = _run_bulk_update(
            user=user,
            ids=[parent.id],
            updates={"project": dst.id},
        )
        assert count == 2  # parent + cascaded child
        parent.refresh_from_db()
        child.refresh_from_db()
        assert parent.project_id == dst.id
        assert child.project_id == dst.id
        assert child.parent_id == parent.id

    def test_subtask_moved_alone_loses_parent(self):
        ws = WorkspaceFactory()
        src = ProjectFactory(workspace=ws)
        dst = ProjectFactory(workspace=ws)
        user = ws.owner
        parent = TaskFactory(project=src, reporter=user)
        child = TaskFactory(project=src, parent=parent, reporter=user)
        _run_bulk_update(
            user=user,
            ids=[child.id],
            updates={"project": dst.id},
        )
        child.refresh_from_db()
        assert child.project_id == dst.id
        assert child.parent_id is None

    def test_cross_workspace_move_rejected(self):
        """When user is in both workspaces, the move still gets rejected.

        Validates the workspace-boundary check itself, not the access
        gate — those are separate concerns and surface as different
        exceptions (``serializers.ValidationError`` vs
        ``PermissionError``).
        """
        ws_a = WorkspaceFactory()
        ws_b = WorkspaceFactory()
        user = ws_a.owner
        # User must be a member of BOTH workspaces to isolate the
        # cross-workspace check from the target-access check.
        WorkspaceMemberFactory(user=user, workspace=ws_b, role=WorkspaceMember.MEMBER)
        src = ProjectFactory(workspace=ws_a)
        dst = ProjectFactory(workspace=ws_b)
        task = TaskFactory(project=src, reporter=user)
        with pytest.raises(serializers.ValidationError):
            _run_bulk_update(
                user=user,
                ids=[task.id],
                updates={"project": dst.id},
            )
        task.refresh_from_db()
        assert task.project_id == src.id

    def test_inaccessible_target_workspace_returns_permission_error(self):
        """Different failure mode: user has no access to the target workspace."""
        ws_a = WorkspaceFactory()
        ws_b = WorkspaceFactory()
        user = ws_a.owner  # not a member of ws_b
        src = ProjectFactory(workspace=ws_a)
        dst = ProjectFactory(workspace=ws_b)
        task = TaskFactory(project=src, reporter=user)
        with pytest.raises(PermissionError):
            _run_bulk_update(
                user=user,
                ids=[task.id],
                updates={"project": dst.id},
            )


@pytest.mark.django_db
class TestBulkArchive:
    """Bulk archive / unarchive via the universal PATCH endpoint."""

    def test_archive_batch_sets_archived_at_and_emits_events(self):
        _, _, user, tasks = _seed_workspace_with_tasks(3)
        ids = [t.id for t in tasks]
        bulk_id, count = _run_bulk_update(user=user, ids=ids, updates={"archived": True})
        assert count == 3
        for t in tasks:
            t.refresh_from_db()
            assert t.archived_at is not None
        events = ActivityLog.objects.filter(bulk_id=bulk_id, event_type="task.archived")
        assert events.count() == 3

    def test_unarchive_batch_clears_archived_at(self):
        from django.utils import timezone as tz

        _, _, user, tasks = _seed_workspace_with_tasks(2)
        Task.objects.filter(id__in=[t.id for t in tasks]).update(archived_at=tz.now())
        bulk_id, _ = _run_bulk_update(
            user=user,
            ids=[t.id for t in tasks],
            updates={"archived": False},
        )
        for t in tasks:
            t.refresh_from_db()
            assert t.archived_at is None
        events = ActivityLog.objects.filter(bulk_id=bulk_id, event_type="task.unarchived")
        assert events.count() == 2

    def test_archive_is_idempotent_no_duplicate_event(self):
        """Re-archiving an already-archived task should be a no-op event-wise."""
        from django.utils import timezone as tz

        _, _, user, tasks = _seed_workspace_with_tasks(1)
        Task.objects.filter(id=tasks[0].id).update(archived_at=tz.now())
        bulk_id, _ = _run_bulk_update(user=user, ids=[tasks[0].id], updates={"archived": True})
        events = ActivityLog.objects.filter(bulk_id=bulk_id, event_type="task.archived")
        # Already archived → archived_at was non-null both before and
        # after, so no diff event should fire. (archived_at is bumped
        # to "now" again, but the boolean-ness — null vs not null —
        # didn't change.)
        assert events.count() == 0

    def test_archived_rejects_non_bool(self):
        _, _, user, tasks = _seed_workspace_with_tasks(1)
        from apps.tasks.bulk import BulkUpdateSerializer

        s = BulkUpdateSerializer(data={"ids": [tasks[0].id], "updates": {"archived": "yes"}})
        assert not s.is_valid()
        assert "archived" in s.errors["updates"]


@pytest.mark.django_db
class TestBulkDelete:
    """Bulk delete keeps the activity trail."""

    def test_deletes_rows_and_emits_events(self):
        _, project, user, tasks = _seed_workspace_with_tasks(3)
        ids = [t.id for t in tasks]
        bulk_id, count = _run_bulk_delete(user=user, ids=ids)
        assert count == 3
        assert not Task.objects.filter(id__in=ids).exists()
        events = ActivityLog.objects.filter(bulk_id=bulk_id)
        assert events.count() == 3
        assert set(events.values_list("event_type", flat=True)) == {"task.deleted"}
        for evt in events:
            assert "snapshot" in evt.payload
            assert "title" in evt.payload["snapshot"]

    def test_inaccessible_batch_rejected(self):
        _, _, user, my_tasks = _seed_workspace_with_tasks(1)
        _, _, _, foreign_tasks = _seed_workspace_with_tasks(1)
        with pytest.raises(PermissionError):
            _run_bulk_delete(user=user, ids=[my_tasks[0].id, foreign_tasks[0].id])
        assert Task.objects.filter(id=my_tasks[0].id).exists()
        assert Task.objects.filter(id=foreign_tasks[0].id).exists()
