"""Global command palette typeahead endpoint (web:palette_search)."""

from django.urls import reverse

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestPaletteSearch:
    def test_empty_query_returns_all_sections_with_defaults(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, name="Apollo")
        TaskFactory(project=project, title="seed")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:palette_search"))
        assert resp.status_code == 200
        data = resp.json()
        kinds = [s["kind"] for s in data["sections"]]
        assert kinds == ["tasks", "projects", "nav"]
        # Empty query: recent tasks + all projects + every nav target.
        by_kind = {s["kind"]: s for s in data["sections"]}
        assert any(t["title"] == "seed" for t in by_kind["tasks"]["items"])
        assert any(p["name"] == "Apollo" for p in by_kind["projects"]["items"])
        nav_labels = [n["label"] for n in by_kind["nav"]["items"]]
        assert "Dashboard" in nav_labels
        assert "Inbox" in nav_labels

    def test_query_filters_tasks_by_title(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        TaskFactory(project=project, title="Wire up sentry")
        TaskFactory(project=project, title="Untouched")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:palette_search"), {"q": "sentry"})
        titles = [t["title"] for s in resp.json()["sections"] if s["kind"] == "tasks" for t in s["items"]]
        assert titles == ["Wire up sentry"]

    def test_query_matches_task_slug(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, title="unrelated")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:palette_search"), {"q": task.slug})
        slugs = [t["slug"] for s in resp.json()["sections"] if s["kind"] == "tasks" for t in s["items"]]
        assert task.slug in slugs

    def test_query_filters_projects_by_name_and_prefix(self, client):
        ws = WorkspaceFactory()
        ProjectFactory(workspace=ws, name="Apollo", slug_prefix="APO")
        ProjectFactory(workspace=ws, name="Mercury", slug_prefix="MER")
        client.force_login(ws.owner)
        # By name
        resp = client.get(reverse("web:palette_search"), {"q": "apoll"})
        names = [p["name"] for s in resp.json()["sections"] if s["kind"] == "projects" for p in s["items"]]
        assert names == ["Apollo"]
        # By slug_prefix (case-insensitive)
        resp = client.get(reverse("web:palette_search"), {"q": "mer"})
        names = [p["name"] for s in resp.json()["sections"] if s["kind"] == "projects" for p in s["items"]]
        assert names == ["Mercury"]

    def test_query_filters_nav_by_label(self, client):
        ws = WorkspaceFactory()
        client.force_login(ws.owner)
        resp = client.get(reverse("web:palette_search"), {"q": "inb"})
        nav = [n["label"] for s in resp.json()["sections"] if s["kind"] == "nav" for n in s["items"]]
        assert "Inbox" in nav
        assert "Dashboard" not in nav

    def test_scoped_to_active_workspace(self, client):
        ws = WorkspaceFactory()
        other_ws = WorkspaceFactory()
        ProjectFactory(workspace=ws, name="Mine")
        ProjectFactory(workspace=other_ws, name="Foreign")
        # ``ws.owner`` is only a member of ``ws`` — the other workspace
        # belongs to a different user, so projects from there must not
        # leak into the palette.
        client.force_login(ws.owner)
        resp = client.get(reverse("web:palette_search"))
        names = [p["name"] for s in resp.json()["sections"] if s["kind"] == "projects" for p in s["items"]]
        assert "Mine" in names
        assert "Foreign" not in names

    def test_login_required(self, client):
        resp = client.get(reverse("web:palette_search"))
        assert resp.status_code in (302, 401, 403)

    def test_task_url_resolves(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, title="anchor")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:palette_search"), {"q": "anchor"})
        items = [t for s in resp.json()["sections"] if s["kind"] == "tasks" for t in s["items"]]
        assert items[0]["url"] == reverse(
            "web:task_detail",
            kwargs={"slug_prefix": task.project.slug_prefix, "number": task.number},
        )
