"""Integration tests for :class:`CommentViewSet`.

Covers the author-or-admin write matrix, workspace scoping, the
task-required guard, and the activity events emitted on create / edit /
delete.
"""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.comments.models import Comment
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def member():
    return UserFactory()


@pytest.fixture
def workspace(member):
    return WorkspaceFactory(owner=member)


@pytest.fixture
def task(workspace):
    return TaskFactory(project=ProjectFactory(workspace=workspace))


@pytest.fixture
def client(member):
    c = APIClient()
    c.force_authenticate(member)
    return c


@pytest.mark.django_db
class TestCommentCrud:
    def test_create_sets_author_and_event(self, client, member, task):
        resp = client.post("/api/v1/comments/", {"task": task.id, "body": "Looks good"})
        assert resp.status_code == 201, resp.content
        comment = Comment.objects.get(id=resp.data["id"])
        assert comment.author == member
        assert ActivityLog.objects.filter(
            event_type="comment.created",
            target_id=comment.id,
        ).exists()

    def test_task_is_required(self, client):
        resp = client.post("/api/v1/comments/", {"body": "orphan"})
        assert resp.status_code == 400
        assert "task" in resp.data

    def test_cannot_comment_on_foreign_task(self, client):
        foreign = TaskFactory()
        resp = client.post("/api/v1/comments/", {"task": foreign.id, "body": "x"})
        assert resp.status_code == 400
        assert "task" in resp.data

    def test_list_scoped_to_membership(self, client, task):
        mine = client.post("/api/v1/comments/", {"task": task.id, "body": "x"})
        foreign_task = TaskFactory()
        Comment.objects.create(task=foreign_task, author=UserFactory(), body="hidden")
        resp = client.get("/api/v1/comments/")
        ids = {row["id"] for row in resp.data["results"]}
        assert ids == {mine.data["id"]}

    def test_edit_emits_event(self, client, member, task):
        comment = Comment.objects.create(task=task, author=member, body="old")
        resp = client.patch(f"/api/v1/comments/{comment.id}/", {"body": "new"})
        assert resp.status_code == 200, resp.content
        assert ActivityLog.objects.filter(event_type="comment.edited", target_id=comment.id).exists()

    def test_delete_emits_event(self, client, member, task):
        comment = Comment.objects.create(task=task, author=member, body="bye")
        resp = client.delete(f"/api/v1/comments/{comment.id}/")
        assert resp.status_code == 204
        assert ActivityLog.objects.filter(event_type="comment.deleted", target_id=comment.id).exists()


@pytest.mark.django_db
class TestCommentPermissionMatrix:
    """``IsAuthorOrWorkspaceAdmin``: author edits own, admin edits any."""

    def test_member_cannot_edit_others_comment(self, workspace, task):
        author = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=author, role=WorkspaceMember.MEMBER)
        regular = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=regular, role=WorkspaceMember.MEMBER)
        comment = Comment.objects.create(task=task, author=author, body="orig")
        client = APIClient()
        client.force_authenticate(regular)
        resp = client.patch(f"/api/v1/comments/{comment.id}/", {"body": "hijack"})
        assert resp.status_code == 403
        comment.refresh_from_db()
        assert comment.body != "hijack"

    def test_admin_can_delete_others_comment(self, workspace, task):
        author = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=author, role=WorkspaceMember.MEMBER)
        admin = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=admin, role=WorkspaceMember.ADMIN)
        comment = Comment.objects.create(task=task, author=author, body="spam")
        client = APIClient()
        client.force_authenticate(admin)
        resp = client.delete(f"/api/v1/comments/{comment.id}/")
        assert resp.status_code == 204
        assert not Comment.objects.filter(id=comment.id).exists()

    def test_author_can_edit_own_comment(self, workspace, task):
        author = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=author, role=WorkspaceMember.MEMBER)
        comment = Comment.objects.create(task=task, author=author, body="orig")
        client = APIClient()
        client.force_authenticate(author)
        resp = client.patch(f"/api/v1/comments/{comment.id}/", {"body": "fixed"})
        assert resp.status_code == 200, resp.content
        comment.refresh_from_db()
        assert comment.body == "fixed"
