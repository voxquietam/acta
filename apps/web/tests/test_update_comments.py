"""Comments + one-level replies on project updates."""

from django.urls import reverse

import pytest

from apps.comments.models import Comment
from apps.projects.tests.factories import ProjectFactory, ProjectUpdateFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestPostUpdateComment:
    def _url(self, update):
        return reverse("web:post_update_comment", args=[update.pk])

    def test_member_posts_top_level_comment(self, client):
        ws = WorkspaceFactory()
        update = ProjectUpdateFactory(project=ProjectFactory(workspace=ws), author=ws.owner)
        client.force_login(ws.owner)
        resp = client.post(self._url(update), {"body": "nice work"})
        assert resp.status_code == 200
        c = Comment.objects.get(project_update=update)
        assert c.task_id is None
        assert c.parent_id is None
        assert c.author == ws.owner
        assert "nice work" in resp.content.decode()

    def test_reply_to_top_level(self, client):
        ws = WorkspaceFactory()
        update = ProjectUpdateFactory(project=ProjectFactory(workspace=ws), author=ws.owner)
        top = Comment.objects.create(project_update=update, author=ws.owner, body="top")
        client.force_login(ws.owner)
        resp = client.post(self._url(update) + f"?parent={top.id}", {"body": "a reply"})
        assert resp.status_code == 200
        reply = Comment.objects.get(parent=top)
        assert reply.project_update_id == update.id

    def test_reply_to_reply_rejected(self, client):
        ws = WorkspaceFactory()
        update = ProjectUpdateFactory(project=ProjectFactory(workspace=ws), author=ws.owner)
        top = Comment.objects.create(project_update=update, author=ws.owner, body="top")
        reply = Comment.objects.create(project_update=update, author=ws.owner, parent=top, body="r1")
        client.force_login(ws.owner)
        resp = client.post(self._url(update) + f"?parent={reply.id}", {"body": "nested"})
        assert resp.status_code == 400

    def test_empty_body_rejected(self, client):
        ws = WorkspaceFactory()
        update = ProjectUpdateFactory(project=ProjectFactory(workspace=ws), author=ws.owner)
        client.force_login(ws.owner)
        resp = client.post(self._url(update), {"body": "   "})
        assert resp.status_code == 400

    def test_foreign_update_404(self, client):
        update = ProjectUpdateFactory()
        intruder = WorkspaceFactory().owner
        client.force_login(intruder)
        resp = client.post(self._url(update), {"body": "x"})
        assert resp.status_code == 404


@pytest.mark.django_db
class TestCommentTargetConstraint:
    def test_clean_rejects_both_targets(self):
        from django.core.exceptions import ValidationError

        from apps.tasks.tests.factories import TaskFactory

        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        update = ProjectUpdateFactory(project=project, author=ws.owner)
        comment = Comment(task=task, project_update=update, author=ws.owner, body="x")
        with pytest.raises(ValidationError):
            comment.clean()

    def test_db_constraint_blocks_targetless_comment(self):
        from django.db.utils import IntegrityError

        ws = WorkspaceFactory()
        with pytest.raises(IntegrityError):
            Comment.objects.create(author=ws.owner, body="orphan")


@pytest.mark.django_db
class TestEditDeleteUpdateComment:
    """Edit + delete parity for project-update comments (unified routes)."""

    def _setup(self):
        from apps.accounts.tests.factories import UserFactory

        ws = WorkspaceFactory()
        update = ProjectUpdateFactory(project=ProjectFactory(workspace=ws), author=ws.owner)
        comment = Comment.objects.create(project_update=update, author=ws.owner, body="orig")
        return ws, update, comment, UserFactory

    def test_author_edits(self, client):
        ws, update, comment, _ = self._setup()
        client.force_login(ws.owner)
        resp = client.post(reverse("web:edit_comment", kwargs={"comment_id": comment.id}), {"body": "edited"})
        assert resp.status_code == 200
        comment.refresh_from_db()
        assert comment.body == "edited"

    def test_author_deletes(self, client):
        ws, update, comment, _ = self._setup()
        client.force_login(ws.owner)
        resp = client.post(reverse("web:delete_comment", kwargs={"comment_id": comment.id}))
        assert resp.status_code == 200
        assert not Comment.objects.filter(pk=comment.id).exists()

    def test_admin_edits_others(self, client):
        from apps.workspaces.models import WorkspaceMember

        ws, update, comment, UserFactory = self._setup()
        admin = UserFactory()
        WorkspaceMember.objects.create(user=admin, workspace=ws, role=WorkspaceMember.ADMIN)
        client.force_login(admin)
        resp = client.post(reverse("web:edit_comment", kwargs={"comment_id": comment.id}), {"body": "by admin"})
        assert resp.status_code == 200
        comment.refresh_from_db()
        assert comment.body == "by admin"

    def test_other_member_forbidden(self, client):
        from apps.workspaces.models import WorkspaceMember

        ws, update, comment, UserFactory = self._setup()
        member = UserFactory()
        WorkspaceMember.objects.create(user=member, workspace=ws, role=WorkspaceMember.MEMBER)
        client.force_login(member)
        assert (
            client.post(reverse("web:edit_comment", kwargs={"comment_id": comment.id}), {"body": "x"}).status_code
            == 403
        )
        assert client.post(reverse("web:delete_comment", kwargs={"comment_id": comment.id})).status_code == 403
        assert Comment.objects.filter(pk=comment.id).exists()

    def test_edit_form_prefilled(self, client):
        ws, update, comment, _ = self._setup()
        client.force_login(ws.owner)
        resp = client.get(reverse("web:comment_edit_form", kwargs={"comment_id": comment.id}))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "orig" in body
        assert "data-description-editor" in body

    def test_non_member_404(self, client):
        ws, update, comment, UserFactory = self._setup()
        client.force_login(UserFactory())
        assert client.get(reverse("web:comment_edit_form", kwargs={"comment_id": comment.id})).status_code == 404
