"""Tests for task links — blocks / blocked-by / related."""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def member_setup(db):
    """Workspace + member user logged in + a project with two tasks."""
    workspace = WorkspaceFactory()
    user = UserFactory()
    WorkspaceMember.objects.create(workspace=workspace, user=user, role=WorkspaceMember.MEMBER)
    project = ProjectFactory(workspace=workspace, slug_prefix="TST")
    a = TaskFactory(project=project, title="Task A")
    b = TaskFactory(project=project, title="Task B")
    return workspace, user, project, a, b


def _add(client, task, kind, target_slug):
    return client.post(
        reverse("web:add_task_link", args=[task.project.slug_prefix, task.number]),
        data={"kind": kind, "target": target_slug},
    )


def _remove(client, task, kind, target_id):
    return client.post(
        reverse("web:remove_task_link", args=[task.project.slug_prefix, task.number]),
        data={"kind": kind, "target_id": target_id},
    )


@pytest.mark.django_db
class TestModelLinks:
    def test_is_blocked_true_for_open_blocker(self):
        project = ProjectFactory()
        a = TaskFactory(project=project, status=Task.STATUS_TODO)
        b = TaskFactory(project=project, status=Task.STATUS_TODO)
        # b is blocked by a (a is still open)
        a.blocks.add(b)
        assert b.is_blocked is True
        assert a.is_blocked is False

    def test_done_blocker_does_not_block(self):
        project = ProjectFactory()
        a = TaskFactory(project=project, status=Task.STATUS_DONE)
        b = TaskFactory(project=project, status=Task.STATUS_TODO)
        a.blocks.add(b)
        assert b.is_blocked is False

    def test_related_is_symmetric(self):
        project = ProjectFactory()
        a = TaskFactory(project=project)
        b = TaskFactory(project=project)
        a.related.add(b)
        assert b in a.related.all()
        assert a in b.related.all()


@pytest.mark.django_db
class TestAddLinkEndpoint:
    def test_add_blocks(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        resp = _add(client, a, "blocks", b.slug)
        assert resp.status_code == 200
        assert b in a.blocks.all()
        assert a in b.blocked_by.all()

    def test_add_blocked_by_reverses_direction(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        # "a is blocked by b" → b.blocks.add(a)
        resp = _add(client, a, "blocked_by", b.slug)
        assert resp.status_code == 200
        assert a in b.blocks.all()
        assert b in a.blocked_by.all()

    def test_add_related(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        resp = _add(client, a, "related", b.slug)
        assert resp.status_code == 200
        assert b in a.related.all()

    def test_self_link_rejected(self, client, member_setup):
        _, user, _, a, _b = member_setup
        client.force_login(user)
        resp = _add(client, a, "blocks", a.slug)
        assert resp.status_code == 400

    def test_cross_workspace_rejected(self, client, member_setup):
        _, user, _, a, _b = member_setup
        client.force_login(user)
        # Task in a different workspace the user isn't even a member of
        other = TaskFactory()
        resp = _add(client, a, "blocks", other.slug)
        # Either 400 (different workspace) or 400 (not found — _user_task_qs
        # excludes it). Both are acceptable rejections.
        assert resp.status_code == 400
        assert other not in a.blocks.all()

    def test_circular_block_rejected(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        # a blocks b, then try b blocks a → 2-cycle, must reject
        _add(client, a, "blocks", b.slug)
        resp = _add(client, b, "blocks", a.slug)
        assert resp.status_code == 400
        assert a not in b.blocks.all()

    def test_link_emits_activity_event(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        _add(client, a, "blocks", b.slug)
        ev = ActivityLog.objects.filter(event_type="task.link_added", target_id=a.id).first()
        assert ev is not None
        assert ev.payload["kind"] == "blocks"
        assert ev.payload["target_slug"] == b.slug
        assert ev.actor_id == user.id


@pytest.mark.django_db
class TestRemoveLinkEndpoint:
    def test_remove_blocks(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        a.blocks.add(b)
        resp = _remove(client, a, "blocks", b.id)
        assert resp.status_code == 200
        assert b not in a.blocks.all()

    def test_remove_related_symmetric(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        a.related.add(b)
        resp = _remove(client, a, "related", b.id)
        assert resp.status_code == 200
        assert b not in a.related.all()
        assert a not in b.related.all()

    def test_remove_emits_activity_event(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        a.blocks.add(b)
        _remove(client, a, "blocks", b.id)
        ev = ActivityLog.objects.filter(event_type="task.link_removed", target_id=a.id).first()
        assert ev is not None
        assert ev.payload["target_slug"] == b.slug


@pytest.mark.django_db
class TestLinkSearch:
    def test_search_by_title(self, client, member_setup):
        _, user, project, a, b = member_setup
        client.force_login(user)
        # b.title == "Task B"
        resp = client.get(
            reverse("web:task_link_search", args=[a.project.slug_prefix, a.number]),
            data={"q": "Task B"},
        )
        assert resp.status_code == 200
        slugs = [r["slug"] for r in resp.json()["results"]]
        assert b.slug in slugs
        assert a.slug not in slugs  # self excluded

    def test_search_by_slug(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        resp = client.get(
            reverse("web:task_link_search", args=[a.project.slug_prefix, a.number]),
            data={"q": b.slug},
        )
        assert resp.status_code == 200
        slugs = [r["slug"] for r in resp.json()["results"]]
        assert b.slug in slugs

    def test_search_excludes_already_linked(self, client, member_setup):
        _, user, _, a, b = member_setup
        client.force_login(user)
        a.blocks.add(b)
        resp = client.get(
            reverse("web:task_link_search", args=[a.project.slug_prefix, a.number]),
            data={"q": "Task"},
        )
        slugs = [r["slug"] for r in resp.json()["results"]]
        assert b.slug not in slugs  # already linked

    def test_search_status_filter(self, client, member_setup):
        _, user, project, a, b = member_setup
        client.force_login(user)
        b.status = Task.STATUS_DONE
        b.save(update_fields=["status"])
        c = TaskFactory(project=project, title="Task C", status=Task.STATUS_TODO)
        resp = client.get(
            reverse("web:task_link_search", args=[a.project.slug_prefix, a.number]),
            data={"q": "Task", "status": "done"},
        )
        slugs = [r["slug"] for r in resp.json()["results"]]
        assert b.slug in slugs
        assert c.slug not in slugs  # filtered out by status
