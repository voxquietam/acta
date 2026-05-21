"""Compose a project status update from the overview composer."""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import Notification
from apps.projects.models import ProjectUpdate
from apps.projects.tests.factories import ProjectFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


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

    def test_post_notifies_other_members(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        other = WorkspaceMemberFactory(workspace=ws).user
        client.force_login(ws.owner)
        client.post(self._url(project), {"health": ProjectUpdate.ON_TRACK, "body": "shipped it"})
        assert Notification.objects.filter(recipient=other, kind=Notification.Kind.PROJECT_UPDATE).exists()
        # author is self-suppressed
        assert not Notification.objects.filter(recipient=ws.owner, kind=Notification.Kind.PROJECT_UPDATE).exists()

    def test_get_not_allowed(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        client.force_login(ws.owner)
        resp = client.get(self._url(project))
        assert resp.status_code == 405


@pytest.mark.django_db
class TestEditDeleteProjectUpdate:
    def _make(self, ws, **kw):
        project = kw.pop("project", None) or ProjectFactory(workspace=ws)
        return ProjectUpdate.objects.create(
            project=project,
            author=kw.pop("author", ws.owner),
            health=kw.pop("health", ProjectUpdate.ON_TRACK),
            body=kw.pop("body", "orig"),
        )

    def test_author_edits_health_and_body(self, client):
        ws = WorkspaceFactory()
        u = self._make(ws)
        client.force_login(ws.owner)
        resp = client.post(
            reverse("web:edit_project_update", args=[u.id]),
            {"health": ProjectUpdate.OFF_TRACK, "body": "rewritten"},
        )
        assert resp.status_code == 200
        u.refresh_from_db()
        assert u.health == ProjectUpdate.OFF_TRACK
        assert u.body == "rewritten"
        assert "rewritten" in resp.content.decode()

    def test_edit_invalid_health_400(self, client):
        ws = WorkspaceFactory()
        u = self._make(ws)
        client.force_login(ws.owner)
        resp = client.post(reverse("web:edit_project_update", args=[u.id]), {"health": "x", "body": "y"})
        assert resp.status_code == 400
        u.refresh_from_db()
        assert u.body == "orig"

    def test_edit_empty_body_400(self, client):
        ws = WorkspaceFactory()
        u = self._make(ws)
        client.force_login(ws.owner)
        resp = client.post(
            reverse("web:edit_project_update", args=[u.id]),
            {"health": ProjectUpdate.ON_TRACK, "body": "  "},
        )
        assert resp.status_code == 400

    def test_non_author_member_cannot_edit(self, client):
        ws = WorkspaceFactory()
        u = self._make(ws)
        member = UserFactory()
        WorkspaceMemberFactory(workspace=ws, user=member)
        client.force_login(member)
        resp = client.post(
            reverse("web:edit_project_update", args=[u.id]),
            {"health": ProjectUpdate.OFF_TRACK, "body": "hax"},
        )
        assert resp.status_code == 403
        u.refresh_from_db()
        assert u.body == "orig"

    def test_workspace_admin_can_edit(self, client):
        ws = WorkspaceFactory()
        u = self._make(ws)
        admin = UserFactory()
        WorkspaceMemberFactory(workspace=ws, user=admin, role=WorkspaceMember.ADMIN)
        client.force_login(admin)
        resp = client.post(
            reverse("web:edit_project_update", args=[u.id]),
            {"health": ProjectUpdate.ON_TRACK, "body": "moderated"},
        )
        assert resp.status_code == 200
        u.refresh_from_db()
        assert u.body == "moderated"

    def test_edit_form_prefilled(self, client):
        ws = WorkspaceFactory()
        u = self._make(ws, health=ProjectUpdate.AT_RISK, body="prefill me")
        client.force_login(ws.owner)
        resp = client.get(reverse("web:update_edit_form", args=[u.id]))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "prefill me" in body
        assert "data-description-editor" in body
        # the current health radio is pre-checked
        assert f'value="{ProjectUpdate.AT_RISK}" checked' in body

    def test_author_deletes_update(self, client):
        ws = WorkspaceFactory()
        u = self._make(ws)
        client.force_login(ws.owner)
        resp = client.post(reverse("web:delete_project_update", args=[u.id]))
        assert resp.status_code == 200
        assert not ProjectUpdate.objects.filter(id=u.id).exists()
        assert "updates-changed" in resp["HX-Trigger"]

    def test_delete_returns_next_latest(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        older = self._make(ws, project=project, body="older one")
        newer = self._make(ws, project=project, body="newer one")
        client.force_login(ws.owner)
        resp = client.post(reverse("web:delete_project_update", args=[newer.id]))
        assert resp.status_code == 200
        assert "older one" in resp.content.decode()
        assert ProjectUpdate.objects.filter(id=older.id).exists()

    def test_non_author_member_cannot_delete(self, client):
        ws = WorkspaceFactory()
        u = self._make(ws)
        member = UserFactory()
        WorkspaceMemberFactory(workspace=ws, user=member)
        client.force_login(member)
        resp = client.post(reverse("web:delete_project_update", args=[u.id]))
        assert resp.status_code == 403
        assert ProjectUpdate.objects.filter(id=u.id).exists()

    def test_foreign_update_404(self, client):
        ws = WorkspaceFactory()
        u = self._make(ws)
        intruder = WorkspaceFactory().owner
        client.force_login(intruder)
        resp = client.post(
            reverse("web:edit_project_update", args=[u.id]),
            {"health": ProjectUpdate.ON_TRACK, "body": "z"},
        )
        assert resp.status_code == 404
