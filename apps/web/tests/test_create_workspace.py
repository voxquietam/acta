"""Tests for the create-workspace modal view.

Covers GET render, slug auto-generation, manual slug override,
duplicate-slug rejection, name validation, and the owner-membership
seeding side effect.
"""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.workspaces.models import Workspace, WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory

URL = reverse("web:create_workspace")


@pytest.fixture
def user(db):
    return UserFactory()


@pytest.mark.django_db
class TestCreateWorkspaceGet:

    def test_htmx_request_gets_modal(self, client, user):
        client.force_login(user)
        resp = client.get(URL, HTTP_HX_REQUEST="true")
        assert resp.status_code == 200
        assert b"New workspace" in resp.content

    def test_direct_get_redirects_to_projects(self, client, user):
        """Direct browser GET (no HX-Request header) → 302 to /projects/.
        The bare modal fragment shouldn't render as a standalone page."""
        client.force_login(user)
        resp = client.get(URL)
        assert resp.status_code == 302
        assert resp["Location"] == "/projects/"

    def test_anonymous_redirected(self, client):
        resp = client.get(URL)
        assert resp.status_code in (301, 302)


@pytest.mark.django_db
class TestCreateWorkspacePost:

    def test_create_with_auto_slug(self, client, user):
        client.force_login(user)
        resp = client.post(URL, {"name": "Acta Team"})
        assert resp.status_code == 204
        ws = Workspace.objects.get(name="Acta Team")
        assert ws.slug == "acta-team"
        assert ws.owner_id == user.id
        assert resp["HX-Redirect"] == f"/workspaces/{ws.slug}/settings/"

    def test_create_seeds_owner_membership(self, client, user):
        client.force_login(user)
        client.post(URL, {"name": "Solo"})
        ws = Workspace.objects.get(slug="solo")
        m = WorkspaceMember.objects.get(workspace=ws, user=user)
        assert m.role == WorkspaceMember.OWNER

    def test_manual_slug_used_as_is(self, client, user):
        client.force_login(user)
        resp = client.post(URL, {"name": "Anything", "slug": "my-team"})
        assert resp.status_code == 204
        assert Workspace.objects.filter(slug="my-team").exists()

    def test_manual_slug_lowercased(self, client, user):
        client.force_login(user)
        resp = client.post(URL, {"name": "Anything", "slug": "TEAM-X"})
        assert resp.status_code == 204
        assert Workspace.objects.filter(slug="team-x").exists()

    def test_duplicate_manual_slug_rejected(self, client, user):
        WorkspaceFactory(slug="taken")
        client.force_login(user)
        resp = client.post(URL, {"name": "X", "slug": "taken"})
        assert resp.status_code == 400
        assert not Workspace.objects.filter(name="X").exists()

    def test_duplicate_auto_slug_gets_suffix(self, client, user):
        WorkspaceFactory(slug="acme")
        client.force_login(user)
        resp = client.post(URL, {"name": "Acme"})
        assert resp.status_code == 204
        ws = Workspace.objects.get(name="Acme")
        assert ws.slug == "acme-2"

    def test_empty_name_rejected(self, client, user):
        client.force_login(user)
        resp = client.post(URL, {"name": "  "})
        assert resp.status_code == 400

    def test_invalid_slug_only_punctuation_rejected(self, client, user):
        client.force_login(user)
        resp = client.post(URL, {"name": "X", "slug": "----"})
        # slugify("----") → "" which the view treats as invalid since
        # a manual slug was provided. Auto-gen would have used name.
        assert resp.status_code == 400

    def test_name_too_long_rejected(self, client, user):
        client.force_login(user)
        resp = client.post(URL, {"name": "x" * 121})
        assert resp.status_code == 400

    def test_anonymous_post_redirected(self, client):
        resp = client.post(URL, {"name": "X"})
        assert resp.status_code in (301, 302)
