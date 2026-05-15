"""``log_event`` writer and ``ActivityLog`` model invariants."""

from uuid import uuid4

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestLogEvent:
    """Basic writer: row shape, defaults, payload."""

    def test_writes_a_row(self):
        ws = WorkspaceFactory()
        actor = ws.owner
        evt = log_event(
            workspace=ws,
            actor=actor,
            event_type="task.created",
            target_type=ActivityLog.TARGET_TASK,
            target_id=42,
            payload={"title": "hello"},
        )
        assert evt.id is not None
        evt.refresh_from_db()
        assert evt.actor_id == actor.id
        assert evt.workspace_id == ws.id
        assert evt.event_type == "task.created"
        assert evt.payload == {"title": "hello"}
        assert evt.bulk_id is None
        assert evt.project_id is None

    def test_payload_defaults_to_empty_dict(self):
        ws = WorkspaceFactory()
        evt = log_event(
            workspace=ws,
            actor=ws.owner,
            event_type="task.created",
            target_type=ActivityLog.TARGET_TASK,
            target_id=1,
        )
        assert evt.payload == {}

    def test_bulk_id_persisted(self):
        ws = WorkspaceFactory()
        bid = uuid4()
        evt = log_event(
            workspace=ws,
            actor=ws.owner,
            event_type="task.status_changed",
            target_type=ActivityLog.TARGET_TASK,
            target_id=1,
            bulk_id=bid,
        )
        evt.refresh_from_db()
        assert evt.bulk_id == bid

    def test_system_event_has_null_actor(self):
        ws = WorkspaceFactory()
        evt = log_event(
            workspace=ws,
            actor=None,
            event_type="system.task.archived",
            target_type=ActivityLog.TARGET_TASK,
            target_id=1,
            payload={"source": "system", "reason": "stale"},
        )
        evt.refresh_from_db()
        assert evt.actor_id is None


@pytest.mark.django_db
class TestActivityLogSurvivesTargetDeletion:
    """Activity entries persist when the target object is deleted.

    Documented in ``ActivityLog.target_id`` help_text: it is a plain int,
    not a foreign key, so the row survives ``Task.delete()`` etc.
    """

    def test_row_survives_task_delete(self):
        task = TaskFactory()
        log_event(
            workspace=task.project.workspace,
            project=task.project,
            actor=task.reporter,
            event_type="task.deleted",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"title": task.title},
        )
        task_id = task.id
        task.delete()
        assert ActivityLog.objects.filter(
            target_type=ActivityLog.TARGET_TASK,
            target_id=task_id,
        ).exists()

    def test_actor_set_null_on_user_delete(self):
        ws = WorkspaceFactory()
        actor = UserFactory()
        evt = log_event(
            workspace=ws,
            actor=actor,
            event_type="task.created",
            target_type=ActivityLog.TARGET_TASK,
            target_id=1,
        )
        actor.delete()
        evt.refresh_from_db()
        assert evt.actor_id is None
        assert evt.event_type == "task.created"


@pytest.mark.django_db
class TestActivityLogOrdering:
    """``Meta.ordering`` is ``-created_at`` — the contract from ADR 0011."""

    def test_default_order_is_newest_first(self):
        ws = WorkspaceFactory()
        for i in range(3):
            log_event(
                workspace=ws,
                actor=ws.owner,
                event_type="task.created",
                target_type=ActivityLog.TARGET_TASK,
                target_id=i,
            )
        ordered = list(ActivityLog.objects.filter(workspace=ws))
        # Newest first.
        assert ordered[0].created_at >= ordered[-1].created_at
