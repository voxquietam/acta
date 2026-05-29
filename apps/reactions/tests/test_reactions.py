"""Emoji reactions across tasks, comments, and project updates."""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.comments.models import Comment
from apps.projects.tests.factories import ProjectFactory, ProjectUpdateFactory
from apps.reactions.models import Reaction
from apps.reactions.services import attach_reactions, summarize_reactions, toggle_reaction
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory

THUMB = "👍"
HEART = "❤️"


@pytest.mark.django_db
class TestToggleService:
    def test_toggle_adds_then_removes(self):
        ws = WorkspaceFactory()
        task = TaskFactory(project=ProjectFactory(workspace=ws))
        added = toggle_reaction(user=ws.owner, target_field="task", target=task, emoji=THUMB)
        assert added is True
        assert Reaction.objects.filter(task=task, user=ws.owner, emoji=THUMB).count() == 1
        removed = toggle_reaction(user=ws.owner, target_field="task", target=task, emoji=THUMB)
        assert removed is False
        assert Reaction.objects.filter(task=task).count() == 0

    def test_distinct_emoji_coexist(self):
        ws = WorkspaceFactory()
        task = TaskFactory(project=ProjectFactory(workspace=ws))
        toggle_reaction(user=ws.owner, target_field="task", target=task, emoji=THUMB)
        toggle_reaction(user=ws.owner, target_field="task", target=task, emoji=HEART)
        assert Reaction.objects.filter(task=task, user=ws.owner).count() == 2


@pytest.mark.django_db
class TestSummarize:
    def test_counts_mine_and_names(self):
        ws = WorkspaceFactory()
        other = UserFactory()
        WorkspaceMember.objects.create(user=other, workspace=ws, role=WorkspaceMember.MEMBER)
        task = TaskFactory(project=ProjectFactory(workspace=ws))
        Reaction.objects.create(task=task, user=ws.owner, emoji=THUMB)
        Reaction.objects.create(task=task, user=other, emoji=THUMB)
        Reaction.objects.create(task=task, user=other, emoji=HEART)

        summary = summarize_reactions(target_field="task", ids=[task.id], user_id=ws.owner.id)
        buckets = summary[task.id]
        thumb = next(b for b in buckets if b["emoji"] == THUMB)
        heart = next(b for b in buckets if b["emoji"] == HEART)
        assert thumb["count"] == 2
        assert thumb["mine"] is True
        assert set(thumb["names"]) == {ws.owner.display_name, other.display_name}
        assert heart["count"] == 1
        assert heart["mine"] is False

    def test_emoji_keep_first_reacted_order(self):
        ws = WorkspaceFactory()
        task = TaskFactory(project=ProjectFactory(workspace=ws))
        Reaction.objects.create(task=task, user=ws.owner, emoji=HEART)
        Reaction.objects.create(task=task, user=ws.owner, emoji=THUMB)
        summary = summarize_reactions(target_field="task", ids=[task.id], user_id=ws.owner.id)
        assert [b["emoji"] for b in summary[task.id]] == [HEART, THUMB]

    def test_no_n_plus_one(self):
        ws = WorkspaceFactory()
        task = TaskFactory(project=ProjectFactory(workspace=ws))
        comments = [Comment.objects.create(task=task, author=ws.owner, body=f"c{i}") for i in range(8)]
        for c in comments:
            Reaction.objects.create(comment=c, user=ws.owner, emoji=THUMB)

        with CaptureQueriesContext(connection) as ctx:
            attach_reactions(objs=comments, target_field="comment", user_id=ws.owner.id)
        assert len(ctx.captured_queries) == 1
        assert all(c.reaction_summary[0]["emoji"] == THUMB for c in comments)


@pytest.mark.django_db
class TestToggleView:
    def _url(self, target_type, target_id):
        return reverse("web:toggle_reaction", args=[target_type, target_id])

    def test_add_and_remove_on_task(self, client):
        ws = WorkspaceFactory()
        task = TaskFactory(project=ProjectFactory(workspace=ws))
        client.force_login(ws.owner)
        resp = client.post(self._url("task", task.id), {"emoji": THUMB})
        assert resp.status_code == 200
        assert THUMB in resp.content.decode()
        assert Reaction.objects.filter(task=task, user=ws.owner, emoji=THUMB).exists()
        client.post(self._url("task", task.id), {"emoji": THUMB})
        assert not Reaction.objects.filter(task=task).exists()

    def test_reaction_on_comment(self, client):
        ws = WorkspaceFactory()
        task = TaskFactory(project=ProjectFactory(workspace=ws))
        comment = Comment.objects.create(task=task, author=ws.owner, body="hi")
        client.force_login(ws.owner)
        resp = client.post(self._url("comment", comment.id), {"emoji": HEART})
        assert resp.status_code == 200
        assert Reaction.objects.filter(comment=comment, emoji=HEART).exists()

    def test_reaction_on_update(self, client):
        ws = WorkspaceFactory()
        update = ProjectUpdateFactory(project=ProjectFactory(workspace=ws), author=ws.owner)
        client.force_login(ws.owner)
        resp = client.post(self._url("update", update.id), {"emoji": THUMB})
        assert resp.status_code == 200
        assert Reaction.objects.filter(project_update=update, emoji=THUMB).exists()

    def test_foreign_workspace_404(self, client):
        ws = WorkspaceFactory()
        task = TaskFactory(project=ProjectFactory(workspace=ws))
        outsider = UserFactory()
        client.force_login(outsider)
        resp = client.post(self._url("task", task.id), {"emoji": THUMB})
        assert resp.status_code == 404
        assert not Reaction.objects.exists()

    def test_invalid_target_type_400(self, client):
        ws = WorkspaceFactory()
        client.force_login(ws.owner)
        resp = client.post(self._url("widget", 1), {"emoji": THUMB})
        assert resp.status_code == 400

    def test_empty_emoji_400(self, client):
        ws = WorkspaceFactory()
        task = TaskFactory(project=ProjectFactory(workspace=ws))
        client.force_login(ws.owner)
        resp = client.post(self._url("task", task.id), {"emoji": "  "})
        assert resp.status_code == 400


@pytest.mark.django_db
class TestCascadeDeletion:
    """Reactions ride the CASCADE on every polymorphic target FK.

    The model declares ``on_delete=CASCADE`` for ``task`` /
    ``comment`` / ``project_update`` so the row vanishes the moment
    its target does. Wave 2 C5 §F5 flagged this as a gap because the
    audit relies on the invariant holding across all three targets,
    not just the one the existing toggle tests exercise.
    """

    def _build(self, target_field):
        """Return ``(target, reaction)`` for the requested target type."""
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        if target_field == "task":
            target = TaskFactory(project=project)
            reaction = Reaction.objects.create(task=target, user=ws.owner, emoji=THUMB)
        elif target_field == "comment":
            task = TaskFactory(project=project)
            target = Comment.objects.create(task=task, author=ws.owner, body="hi")
            reaction = Reaction.objects.create(comment=target, user=ws.owner, emoji=THUMB)
        elif target_field == "project_update":
            target = ProjectUpdateFactory(project=project, author=ws.owner)
            reaction = Reaction.objects.create(project_update=target, user=ws.owner, emoji=THUMB)
        else:
            raise AssertionError(target_field)
        return target, reaction

    @pytest.mark.parametrize("target_field", ["task", "comment", "project_update"])
    def test_delete_target_removes_reaction(self, target_field):
        target, reaction = self._build(target_field)
        target.delete()
        assert not Reaction.objects.filter(pk=reaction.pk).exists()
