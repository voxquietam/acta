"""My Activity page — personal comments + activity feed."""

from django.urls import reverse

import pytest

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.comments.models import Comment
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.mark.django_db
class TestMyActivity:
    def test_comments_tab_shows_only_my_comments(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        Comment.objects.create(task=task, author=ws.owner, body="my own comment")
        other = WorkspaceMemberFactory(workspace=ws).user
        Comment.objects.create(task=task, author=other, body="someone elses note")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:my_activity"))
        body = resp.content.decode()
        assert resp.status_code == 200
        assert "my own comment" in body
        assert "someone elses note" not in body

    def test_activity_tab_shows_my_events_with_task_link(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        log_event(
            workspace=ws,
            project=project,
            actor=ws.owner,
            event_type="task.status_changed",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={"from": "to-do", "to": "done"},
        )
        client.force_login(ws.owner)
        resp = client.get(reverse("web:my_activity"), {"tab": "activity"})
        assert resp.status_code == 200
        assert resp.context["activity_tab"] == "activity"
        events = resp.context["my_events"]
        assert len(events) == 1
        assert events[0].linked_task.id == task.id

    def test_comment_event_resolves_task_link(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        log_event(
            workspace=ws,
            project=project,
            actor=ws.owner,
            event_type="comment.created",
            target_type=ActivityLog.TARGET_COMMENT,
            target_id=4242,
            payload={"task_id": task.id, "body_preview": "hi"},
        )
        client.force_login(ws.owner)
        resp = client.get(reverse("web:my_activity"), {"tab": "activity"})
        events = resp.context["my_events"]
        assert any(e.linked_task and e.linked_task.id == task.id for e in events)

    def test_counts_in_context(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        Comment.objects.create(task=task, author=ws.owner, body="c1")
        Comment.objects.create(task=task, author=ws.owner, body="c2")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:my_activity"))
        assert resp.context["my_comments_count"] == 2
