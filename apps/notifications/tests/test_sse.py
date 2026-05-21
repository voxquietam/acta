"""Live notification broadcast over the per-user SSE channel."""

from unittest.mock import patch

import pytest

from apps.notifications.models import Notification
from apps.notifications.services import notify
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.mark.django_db
class TestNotificationBroadcast:
    def test_notify_broadcasts_to_recipient_channel(self, django_capture_on_commit_callbacks):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        recipient = ws.owner
        # The unread count is scoped to the recipient's active workspace.
        recipient.active_workspace = ws
        recipient.save(update_fields=["active_workspace"])
        actor = WorkspaceMemberFactory(workspace=ws).user
        task = TaskFactory(project=project, assignee=recipient, reporter=recipient)
        with patch("django_eventstream.send_event") as send:
            with django_capture_on_commit_callbacks(execute=True):
                notify(
                    recipient_id=recipient.id,
                    actor=actor,
                    kind=Notification.Kind.ASSIGNED,
                    workspace_id=ws.id,
                    task=task,
                    preview=task.title,
                )
        assert send.called
        channel, event_type, payload = send.call_args[0]
        assert channel == f"user-{recipient.id}"
        assert event_type == "notification.created"
        assert payload["kind"] == Notification.Kind.ASSIGNED
        assert payload["unread"] >= 1
        assert "row_html" in payload
        assert "badge_html" in payload

    def test_self_notification_is_not_broadcast(self, django_capture_on_commit_callbacks):
        ws = WorkspaceFactory()
        actor = ws.owner
        with patch("django_eventstream.send_event") as send:
            with django_capture_on_commit_callbacks(execute=True):
                result = notify(
                    recipient_id=actor.id,
                    actor=actor,
                    kind=Notification.Kind.COMMENT,
                    workspace_id=ws.id,
                )
        assert result is None
        assert not send.called
