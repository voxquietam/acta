"""@-mention typeahead endpoint (web:mention_search)."""

from django.urls import reverse

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.mark.django_db
class TestMentionSearch:
    def test_returns_members_and_tasks(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        WorkspaceMemberFactory(workspace=ws, user__username="alice")
        TaskFactory(project=project, title="Wire up sentry")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:mention_search", args=[project.slug_prefix]))
        data = resp.json()
        assert any(u["username"] == "alice" for u in data["users"])
        assert any("sentry" in t["title"].lower() for t in data["tasks"])
        # user cards carry the fields the picker/hover render needs
        sample = data["users"][0]
        assert {"id", "username", "name", "avatar_color"} <= set(sample)

    def test_query_filters_users(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        WorkspaceMemberFactory(workspace=ws, user__username="alice")
        WorkspaceMemberFactory(workspace=ws, user__username="bob")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:mention_search", args=[project.slug_prefix]), {"q": "alic"})
        usernames = {u["username"] for u in resp.json()["users"]}
        assert "alice" in usernames
        assert "bob" not in usernames

    def test_task_match_by_slug(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, title="unrelated title")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:mention_search", args=[project.slug_prefix]), {"q": task.slug})
        slugs = {t["slug"] for t in resp.json()["tasks"]}
        assert task.slug in slugs

    def test_id_mode_returns_member_card(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        member = WorkspaceMemberFactory(workspace=ws, user__username="carol").user
        client.force_login(ws.owner)
        resp = client.get(reverse("web:mention_search", args=[project.slug_prefix]), {"id": member.id})
        assert resp.status_code == 200
        assert resp.json()["user"]["username"] == "carol"

    def test_id_mode_foreign_user_404(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        outsider = WorkspaceFactory().owner
        client.force_login(ws.owner)
        resp = client.get(reverse("web:mention_search", args=[project.slug_prefix]), {"id": outsider.id})
        assert resp.status_code == 404

    def test_task_id_mode_returns_task_card(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, title="Wire sentry", priority=2)
        client.force_login(ws.owner)
        resp = client.get(reverse("web:mention_search", args=[project.slug_prefix]), {"task_id": task.id})
        assert resp.status_code == 200
        data = resp.json()["task"]
        assert data["slug"] == task.slug
        assert data["title"] == "Wire sentry"
        assert "status_label" in data
        assert "priority_label" in data
        assert "labels" in data

    def test_task_id_foreign_task_404(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        foreign_task = TaskFactory(project=ProjectFactory(workspace=WorkspaceFactory()))
        client.force_login(ws.owner)
        resp = client.get(reverse("web:mention_search", args=[project.slug_prefix]), {"task_id": foreign_task.id})
        assert resp.status_code == 404

    def test_foreign_project_404(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        intruder = WorkspaceFactory().owner
        client.force_login(intruder)
        resp = client.get(reverse("web:mention_search", args=[project.slug_prefix]))
        assert resp.status_code == 404
