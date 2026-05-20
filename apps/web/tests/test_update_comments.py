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
