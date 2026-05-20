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

    def test_activity_tab_no_n_plus_one(self, client, django_assert_max_num_queries):
        """The activity feed must batch task / user / label resolution —
        query count stays bounded regardless of how many events there are."""
        from apps.labels.tests.factories import LabelFactory

        ws = WorkspaceFactory()
        other = WorkspaceMemberFactory(workspace=ws).user
        label = LabelFactory(workspace=ws)
        for _ in range(20):
            project = ProjectFactory(workspace=ws)
            task = TaskFactory(project=project)
            log_event(
                workspace=ws,
                project=project,
                actor=ws.owner,
                event_type="task.assigned",
                target_type=ActivityLog.TARGET_TASK,
                target_id=task.id,
                payload={"from_user_id": None, "to_user_id": other.id},
            )
            log_event(
                workspace=ws,
                project=project,
                actor=ws.owner,
                event_type="task.labels_changed",
                target_type=ActivityLog.TARGET_TASK,
                target_id=task.id,
                payload={"added_ids": [label.id], "removed_ids": []},
            )
        client.force_login(ws.owner)
        with django_assert_max_num_queries(20):
            resp = client.get(reverse("web:my_activity"), {"tab": "activity"})
            assert resp.status_code == 200

    def test_comments_load_more_pagination(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        Comment.objects.bulk_create([Comment(task=task, author=ws.owner, body=f"c{i}") for i in range(55)])
        client.force_login(ws.owner)
        first = client.get(reverse("web:my_activity"))
        assert first.context["has_more"] is True
        assert len(first.context["my_comments"]) == 50
        second = client.get(reverse("web:my_activity"), {"tab": "comments", "offset": 50, "items": 1})
        assert second.status_code == 200
        assert len(second.context["my_comments"]) == 5
        assert second.context["has_more"] is False

    def test_counts_in_context(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        Comment.objects.create(task=task, author=ws.owner, body="c1")
        Comment.objects.create(task=task, author=ws.owner, body="c2")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:my_activity"))
        assert resp.context["my_comments_count"] == 2
