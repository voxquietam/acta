"""Tests for the JSON export endpoints (export the current filtered view)."""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.comments.models import Comment
from apps.projects.tests.factories import ProjectFactory, ProjectUpdateFactory
from apps.reactions.models import Reaction
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def owner_ws_project(db):
    """An owner user with one workspace + project, ready to export."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws.owner, ws, project


def _attachment(response):
    """Assert the response is a JSON file download and return its parsed body."""
    assert response.status_code == 200
    assert response["Content-Type"].startswith("application/json")
    assert "attachment" in response["Content-Disposition"]
    return response.json()


@pytest.mark.django_db
class TestExportAuth:
    def test_anonymous_redirected(self, client):
        resp = client.get(reverse("web:export_all_tasks_json"))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url


@pytest.mark.django_db
class TestExportAllTasks:
    def test_exports_workspace_tasks(self, client, owner_ws_project):
        user, ws, project = owner_ws_project
        TaskFactory(project=project, title="Alpha", status=Task.STATUS_TODO)
        TaskFactory(project=project, title="Beta", status=Task.STATUS_IN_PROGRESS)
        client.force_login(user)

        body = _attachment(client.get(reverse("web:export_all_tasks_json")))
        assert body["scope"] == ws.name
        assert body["count"] == 2
        titles = {t["title"] for t in body["tasks"]}
        assert titles == {"Alpha", "Beta"}
        # Full shape: each task carries the readable project ref + slug.
        assert body["tasks"][0]["project"]["slug_prefix"] == project.slug_prefix

    def test_respects_status_filter(self, client, owner_ws_project):
        user, ws, project = owner_ws_project
        TaskFactory(project=project, title="Alpha", status=Task.STATUS_TODO)
        TaskFactory(project=project, title="Beta", status=Task.STATUS_IN_PROGRESS)
        client.force_login(user)

        body = _attachment(client.get(reverse("web:export_all_tasks_json") + "?status=to-do"))
        assert body["count"] == 1
        assert body["tasks"][0]["title"] == "Alpha"

    def test_excludes_foreign_workspace(self, client, owner_ws_project):
        user, ws, project = owner_ws_project
        TaskFactory(project=project, title="Mine")
        # A task in a workspace the user has no membership in.
        other_project = ProjectFactory()
        TaskFactory(project=other_project, title="Theirs")
        client.force_login(user)

        body = _attachment(client.get(reverse("web:export_all_tasks_json")))
        titles = {t["title"] for t in body["tasks"]}
        assert titles == {"Mine"}


@pytest.mark.django_db
class TestExportMyWork:
    def test_exports_only_assigned(self, client, owner_ws_project):
        user, ws, project = owner_ws_project
        TaskFactory(project=project, title="Mine", assignee=user, status=Task.STATUS_TODO)
        TaskFactory(project=project, title="Someone else", assignee=UserFactory(), status=Task.STATUS_TODO)
        client.force_login(user)

        body = _attachment(client.get(reverse("web:export_my_work_json")))
        titles = {t["title"] for t in body["tasks"]}
        assert titles == {"Mine"}


@pytest.mark.django_db
class TestExportProjectTasks:
    def test_exports_project_tasks(self, client, owner_ws_project):
        user, ws, project = owner_ws_project
        TaskFactory(project=project, title="In project")
        client.force_login(user)

        url = reverse("web:export_project_tasks_json", args=[project.slug_prefix])
        body = _attachment(client.get(url))
        assert body["scope"] == project.name
        assert {t["title"] for t in body["tasks"]} == {"In project"}

    def test_task_comments_included_with_replies(self, client, owner_ws_project):
        user, ws, project = owner_ws_project
        task = TaskFactory(project=project, title="Has thread")
        top = Comment.objects.create(task=task, author=user, body="First comment")
        Comment.objects.create(task=task, parent=top, author=user, body="A reply")
        client.force_login(user)

        url = reverse("web:export_project_tasks_json", args=[project.slug_prefix])
        body = _attachment(client.get(url))
        exported = body["tasks"][0]
        assert [c["body"] for c in exported["comments"]] == ["First comment"]
        assert exported["comments"][0]["replies"][0]["body"] == "A reply"
        # Task comments carry no reactions key (overview-only).
        assert "reactions" not in exported["comments"][0]

    def test_foreign_project_404(self, client, owner_ws_project):
        user, ws, project = owner_ws_project
        foreign = ProjectFactory()
        client.force_login(user)

        url = reverse("web:export_project_tasks_json", args=[foreign.slug_prefix])
        assert client.get(url).status_code == 404


@pytest.mark.django_db
class TestExportProjectOverview:
    def test_exports_updates_comments_replies_reactions(self, client, owner_ws_project):
        user, ws, project = owner_ws_project
        update = ProjectUpdateFactory(project=project, author=user, body="Status update")
        top = Comment.objects.create(project_update=update, author=user, body="Top comment")
        Comment.objects.create(project_update=update, parent=top, author=user, body="A reply")
        Reaction.objects.create(project_update=update, user=user, emoji="👍")
        client.force_login(user)

        url = reverse("web:export_project_overview_json", args=[project.slug_prefix])
        body = _attachment(client.get(url))

        assert body["project"]["slug_prefix"] == project.slug_prefix
        assert len(body["updates"]) == 1
        exported_update = body["updates"][0]
        assert exported_update["body"] == "Status update"
        assert exported_update["reactions"][0]["emoji"] == "👍"
        assert len(exported_update["comments"]) == 1
        exported_comment = exported_update["comments"][0]
        assert exported_comment["body"] == "Top comment"
        assert exported_comment["replies"][0]["body"] == "A reply"

    def test_foreign_project_404(self, client, owner_ws_project):
        user, ws, project = owner_ws_project
        foreign = ProjectFactory()
        client.force_login(user)
        url = reverse("web:export_project_overview_json", args=[foreign.slug_prefix])
        assert client.get(url).status_code == 404


@pytest.mark.django_db
class TestExportQueryCounts:
    """The task + overview exports must not fan out per row (no N+1)."""

    def test_all_tasks_export_query_count_constant(self, client, owner_ws_project, django_assert_max_num_queries):
        user, ws, project = owner_ws_project
        labels_user = UserFactory()
        WorkspaceMember.objects.get_or_create(user=labels_user, workspace=ws, defaults={"role": WorkspaceMember.MEMBER})
        for i in range(10):
            TaskFactory(project=project, title=f"T{i}", assignee=user, reporter=labels_user)
        client.force_login(user)

        # A handful of queries (auth/session/ws-resolve/tasks/labels) — not
        # one-per-task. The bound is generous but well below 10+ growth.
        with django_assert_max_num_queries(15):
            _attachment(client.get(reverse("web:export_all_tasks_json")))
