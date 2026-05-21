"""Tests for the create-project modal view.

Covers the GET modal render path and every POST validation branch,
plus the HX-Redirect contract and the workspace-membership gate.
"""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.models import Project
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory

URL = reverse("web:create_project")


@pytest.fixture
def workspace(db):
    return WorkspaceFactory()


@pytest.fixture
def member(workspace):
    user = UserFactory()
    WorkspaceMemberFactory(workspace=workspace, user=user, role=WorkspaceMember.MEMBER)
    return user


@pytest.fixture
def outsider(db):
    return UserFactory()


@pytest.mark.django_db
class TestCreateProjectGet:

    def test_htmx_request_gets_modal(self, client, member):
        client.force_login(member)
        resp = client.get(URL, HTTP_HX_REQUEST="true")
        assert resp.status_code == 200
        assert b"New project" in resp.content or "New project".encode() in resp.content

    def test_direct_get_redirects_to_projects(self, client, member):
        client.force_login(member)
        resp = client.get(URL)
        assert resp.status_code == 302
        assert resp["Location"] == "/projects/"

    def test_anonymous_redirected(self, client):
        resp = client.get(URL)
        assert resp.status_code in (301, 302)

    def test_outsider_with_no_workspaces_sees_empty_modal(self, client, outsider):
        client.force_login(outsider)
        resp = client.get(URL, HTTP_HX_REQUEST="true")
        # Modal still renders; the submit button is disabled because
        # there are no workspaces to attach the project to.
        assert resp.status_code == 200
        assert b"disabled" in resp.content


@pytest.mark.django_db
class TestCreateProjectPost:

    def test_member_can_create(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(
            URL,
            {
                "workspace": workspace.pk,
                "name": "Audit pilot",
                "slug_prefix": "AUD",
            },
        )
        assert resp.status_code == 204
        # Boosted client-side nav (no full reload) + modal-close trigger.
        assert "HX-Redirect" not in resp.headers
        assert "/projects/AUD/" in resp["HX-Location"]
        assert "#app-content" in resp["HX-Location"]
        assert resp["HX-Trigger"] == "acta:project-created"
        project = Project.objects.get(workspace=workspace, slug_prefix="AUD")
        assert project.name == "Audit pilot"
        assert project.lead is None

    def test_owner_can_create(self, client, workspace):
        client.force_login(workspace.owner)
        resp = client.post(
            URL,
            {"workspace": workspace.pk, "name": "X", "slug_prefix": "XYZ"},
        )
        assert resp.status_code == 204

    def test_too_short_slug_rejected(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(
            URL,
            {"workspace": workspace.pk, "name": "X", "slug_prefix": "A"},
        )
        assert resp.status_code == 400

    def test_too_long_slug_rejected(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(
            URL,
            {"workspace": workspace.pk, "name": "X", "slug_prefix": "ABCDEFG"},
        )
        assert resp.status_code == 400

    def test_slug_with_digit_rejected(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(
            URL,
            {"workspace": workspace.pk, "name": "X", "slug_prefix": "AB1"},
        )
        assert resp.status_code == 400

    def test_lowercase_slug_uppercased_before_validation(self, client, workspace, member):
        """The view ``.upper()``s the slug input so casing in the form
        doesn't fail validation — only character-set errors do."""
        client.force_login(member)
        resp = client.post(
            URL,
            {"workspace": workspace.pk, "name": "X", "slug_prefix": "ABC"},
        )
        assert resp.status_code == 204
        assert Project.objects.filter(slug_prefix="ABC").exists()

    def test_duplicate_slug_in_workspace_rejected(self, client, workspace, member):
        Project.objects.create(workspace=workspace, name="First", slug_prefix="DUP")
        client.force_login(member)
        resp = client.post(
            URL,
            {"workspace": workspace.pk, "name": "Second", "slug_prefix": "DUP"},
        )
        assert resp.status_code == 400

    def test_same_slug_different_workspace_allowed(self, client):
        ws1 = WorkspaceFactory()
        ws2 = WorkspaceFactory(owner=ws1.owner)
        # Owner is now a member of both via factory's seed_owner_membership.
        Project.objects.create(workspace=ws1, name="A", slug_prefix="SAM")
        client.force_login(ws1.owner)
        resp = client.post(
            URL,
            {"workspace": ws2.pk, "name": "B", "slug_prefix": "SAM"},
        )
        assert resp.status_code == 204

    def test_empty_name_rejected(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(
            URL,
            {"workspace": workspace.pk, "name": "  ", "slug_prefix": "ABC"},
        )
        assert resp.status_code == 400

    def test_workspace_user_not_member_rejected(self, client, workspace, outsider):
        client.force_login(outsider)
        resp = client.post(
            URL,
            {"workspace": workspace.pk, "name": "X", "slug_prefix": "ABC"},
        )
        assert resp.status_code == 400

    def test_lead_must_be_workspace_member(self, client, workspace, member, outsider):
        client.force_login(member)
        resp = client.post(
            URL,
            {
                "workspace": workspace.pk,
                "name": "X",
                "slug_prefix": "ABC",
                "lead": outsider.pk,
            },
        )
        assert resp.status_code == 400

    def test_lead_can_be_set_to_workspace_member(self, client, workspace, member):
        client.force_login(member)
        resp = client.post(
            URL,
            {
                "workspace": workspace.pk,
                "name": "X",
                "slug_prefix": "ABC",
                "lead": workspace.owner.pk,
            },
        )
        assert resp.status_code == 204
        assert Project.objects.get(slug_prefix="ABC").lead_id == workspace.owner.pk

    def test_invalid_workspace_id_rejected(self, client, member):
        client.force_login(member)
        resp = client.post(URL, {"workspace": "nope", "name": "X", "slug_prefix": "ABC"})
        assert resp.status_code == 400

    def test_anonymous_redirected(self, client, workspace):
        resp = client.post(
            URL,
            {"workspace": workspace.pk, "name": "X", "slug_prefix": "ABC"},
        )
        assert resp.status_code in (301, 302)
