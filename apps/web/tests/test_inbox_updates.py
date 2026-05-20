"""Inbox Updates tab — project-update feed + preview."""

from django.urls import reverse

import pytest

from apps.projects.models import ProjectUpdate
from apps.projects.tests.factories import ProjectFactory, ProjectUpdateFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestInboxUpdatesTab:
    def test_updates_tab_lists_my_workspace_updates(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        ProjectUpdateFactory(
            project=project, author=ws.owner, body="weekly progress here", health=ProjectUpdate.ON_TRACK
        )
        client.force_login(ws.owner)
        resp = client.get(reverse("web:inbox"), {"tab": "updates"})
        assert resp.status_code == 200
        assert resp.context["active_tab"] == "updates"
        assert "weekly progress here" in resp.content.decode()

    def test_updates_tab_excludes_foreign_workspace(self, client):
        ws = WorkspaceFactory()
        ProjectFactory(workspace=ws)
        foreign_update = ProjectUpdateFactory(body="not your update")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:inbox"), {"tab": "updates"})
        assert "not your update" not in resp.content.decode()
        assert foreign_update.pk not in [u.pk for u in resp.context["updates"]]

    def test_health_filter(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        ProjectUpdateFactory(project=project, author=ws.owner, health=ProjectUpdate.ON_TRACK, body="ontrack one")
        ProjectUpdateFactory(project=project, author=ws.owner, health=ProjectUpdate.AT_RISK, body="atrisk one")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:inbox"), {"tab": "updates", "health": ProjectUpdate.AT_RISK})
        healths = {u.health for u in resp.context["updates"]}
        assert healths == {ProjectUpdate.AT_RISK}

    def test_project_filter(self, client):
        """The strip's ``?project=<id>`` narrows updates to that project."""
        ws = WorkspaceFactory()
        p1 = ProjectFactory(workspace=ws)
        p2 = ProjectFactory(workspace=ws)
        ProjectUpdateFactory(project=p1, author=ws.owner, body="p1 update")
        ProjectUpdateFactory(project=p2, author=ws.owner, body="p2 update")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:inbox"), {"tab": "updates", "project": p1.id})
        slugs = {u.project.slug_prefix for u in resp.context["updates"]}
        assert slugs == {p1.slug_prefix}
        assert resp.context["selected_projects"] == {p1.id}

    def test_project_exclude_filter(self, client):
        """The strip's ``?xproject=<id>`` drops that project from updates."""
        ws = WorkspaceFactory()
        p1 = ProjectFactory(workspace=ws)
        p2 = ProjectFactory(workspace=ws)
        ProjectUpdateFactory(project=p1, author=ws.owner, body="p1 update")
        ProjectUpdateFactory(project=p2, author=ws.owner, body="p2 update")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:inbox"), {"tab": "updates", "xproject": p1.id})
        slugs = {u.project.slug_prefix for u in resp.context["updates"]}
        assert slugs == {p2.slug_prefix}
        assert resp.context["excluded_projects"] == {p1.id}

    def test_strip_projects_limited_to_those_with_updates(self, client):
        """The project strip only offers projects that actually have updates."""
        ws = WorkspaceFactory()
        with_update = ProjectFactory(workspace=ws)
        without_update = ProjectFactory(workspace=ws)
        ProjectUpdateFactory(project=with_update, author=ws.owner, body="x")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:inbox"), {"tab": "updates"})
        ids = {p.id for p in resp.context["available_projects"]}
        assert with_update.id in ids
        assert without_update.id not in ids

    def test_updates_filter_htmx_returns_list_partial(self, client):
        """An HTMX filter request returns the updates-list fragment, not the
        notifications list or an empty shell."""
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        ProjectUpdateFactory(project=project, author=ws.owner, body="partial body", health=ProjectUpdate.ON_TRACK)
        client.force_login(ws.owner)
        resp = client.get(
            reverse("web:inbox"),
            {"tab": "updates", "health": ProjectUpdate.ON_TRACK},
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 200
        body = resp.content.decode()
        assert 'id="inbox-updates-list"' in body
        assert "partial body" in body

    def test_update_preview_endpoint(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        update = ProjectUpdateFactory(project=project, author=ws.owner, body="full detail body")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:inbox_update_preview", args=[update.pk]))
        assert resp.status_code == 200
        assert "full detail body" in resp.content.decode()

    def test_update_preview_foreign_404(self, client):
        update = ProjectUpdateFactory()
        intruder = WorkspaceFactory().owner
        client.force_login(intruder)
        resp = client.get(reverse("web:inbox_update_preview", args=[update.pk]))
        assert resp.status_code == 404
