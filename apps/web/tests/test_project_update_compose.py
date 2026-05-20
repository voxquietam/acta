"""Compose a project status update from the overview composer."""

from django.urls import reverse

import pytest

from apps.projects.models import ProjectUpdate
from apps.projects.tests.factories import ProjectFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestPostProjectUpdate:
    def _url(self, project):
        return reverse("web:post_project_update", args=[project.slug_prefix])

    def test_member_posts_update_and_gets_card(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        client.force_login(ws.owner)
        resp = client.post(
            self._url(project),
            {"health": ProjectUpdate.AT_RISK, "body": "staging is blocked"},
        )
        assert resp.status_code == 200
        update = ProjectUpdate.objects.get(project=project)
        assert update.author == ws.owner
        assert update.health == ProjectUpdate.AT_RISK
        assert update.body == "staging is blocked"
        assert "staging is blocked" in resp.content.decode()

    def test_invalid_health_is_rejected(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        client.force_login(ws.owner)
        resp = client.post(self._url(project), {"health": "bogus", "body": "x"})
        assert resp.status_code == 400
        assert not ProjectUpdate.objects.filter(project=project).exists()

    def test_empty_body_is_rejected(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        client.force_login(ws.owner)
        resp = client.post(self._url(project), {"health": ProjectUpdate.ON_TRACK, "body": "   "})
        assert resp.status_code == 400
        assert not ProjectUpdate.objects.filter(project=project).exists()

    def test_foreign_project_is_404(self, client):
        project = ProjectFactory()
        intruder = WorkspaceFactory().owner
        client.force_login(intruder)
        resp = client.post(self._url(project), {"health": ProjectUpdate.ON_TRACK, "body": "x"})
        assert resp.status_code == 404
        assert not ProjectUpdate.objects.filter(project=project).exists()

    def test_get_not_allowed(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        client.force_login(ws.owner)
        resp = client.get(self._url(project))
        assert resp.status_code == 405
