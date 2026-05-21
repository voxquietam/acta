"""Active-workspace resolver, the switcher endpoint, and project auto-switch.

Acta scopes All Tasks / Projects / My Work / Inbox / My Activity to a single
active workspace. These cover how it's resolved (stored value vs fallback),
how the sidebar switcher changes it, and how viewing a project pulls its
workspace into focus.
"""

from django.test import RequestFactory
from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.web.nav import resolve_active_workspace
from apps.workspaces.tests.factories import WorkspaceFactory


def _request_for(user):
    req = RequestFactory().get("/")
    req.user = user
    return req


@pytest.mark.django_db
class TestResolveActiveWorkspace:
    def test_none_when_no_workspaces(self):
        user = UserFactory()
        assert resolve_active_workspace(_request_for(user)) is None

    def test_falls_back_to_first_by_name_and_persists(self):
        user = UserFactory()
        WorkspaceFactory(owner=user, name="Beta")
        WorkspaceFactory(owner=user, name="Alpha")
        active = resolve_active_workspace(_request_for(user))
        assert active.name == "Alpha"
        user.refresh_from_db()
        assert user.active_workspace_id == active.id

    def test_honours_stored_active_when_member(self):
        user = UserFactory()
        WorkspaceFactory(owner=user, name="Alpha")
        beta = WorkspaceFactory(owner=user, name="Beta")
        user.active_workspace = beta
        user.save(update_fields=["active_workspace"])
        assert resolve_active_workspace(_request_for(user)).id == beta.id

    def test_falls_back_when_no_longer_member(self):
        user = UserFactory()
        home = WorkspaceFactory(owner=user, name="Home")
        foreign = WorkspaceFactory(name="Zeta")  # owned by someone else
        user.active_workspace = foreign
        user.save(update_fields=["active_workspace"])
        active = resolve_active_workspace(_request_for(user))
        assert active.id == home.id
        user.refresh_from_db()
        assert user.active_workspace_id == home.id

    def test_memoised_on_request(self):
        user = UserFactory()
        WorkspaceFactory(owner=user)
        req = _request_for(user)
        assert resolve_active_workspace(req) is resolve_active_workspace(req)


@pytest.mark.django_db
class TestSwitchWorkspace:
    def test_switch_sets_active_and_hx_redirects(self, client):
        user = UserFactory()
        a = WorkspaceFactory(owner=user, name="Alpha")
        b = WorkspaceFactory(owner=user, name="Beta")
        user.active_workspace = a
        user.save(update_fields=["active_workspace"])
        client.force_login(user)
        resp = client.post(
            reverse("web:switch_workspace", kwargs={"workspace_id": b.id}),
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 204
        assert resp["HX-Redirect"] == reverse("web:project_list")
        user.refresh_from_db()
        assert user.active_workspace_id == b.id

    def test_switch_non_member_404(self, client):
        user = UserFactory()
        WorkspaceFactory(owner=user)
        foreign = WorkspaceFactory()
        client.force_login(user)
        resp = client.post(reverse("web:switch_workspace", kwargs={"workspace_id": foreign.id}))
        assert resp.status_code == 404
        user.refresh_from_db()
        assert user.active_workspace_id != foreign.id

    def test_switch_get_not_allowed(self, client):
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)
        client.force_login(user)
        resp = client.get(reverse("web:switch_workspace", kwargs={"workspace_id": ws.id}))
        assert resp.status_code == 405


@pytest.mark.django_db
class TestProjectDetailAutoSwitch:
    def test_viewing_project_switches_active_workspace(self, client):
        """Opening a project in another workspace makes it the active one,
        so the sidebar and scoped views follow what's on screen."""
        user = UserFactory()
        home = WorkspaceFactory(owner=user, name="Home")
        other = WorkspaceFactory(owner=user, name="Other")
        user.active_workspace = home
        user.save(update_fields=["active_workspace"])
        project = ProjectFactory(workspace=other)
        client.force_login(user)
        resp = client.get(reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}))
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.active_workspace_id == other.id
