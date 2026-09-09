"""Tests for renaming a project from the overview header.

The interesting surface is who may rename and what a rename leaves
alone: ``slug_prefix`` carries every URL and task identifier, so it must
survive untouched.
"""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.web.url_scoping import project_path
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def setup(db):
    """Workspace owner, a plain member, and a project in that workspace."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws, name="Old name")
    member = UserFactory()
    WorkspaceMemberFactory(user=member, workspace=ws, role=WorkspaceMember.MEMBER)
    return ws, project, member


def _url(project):
    """Return the rename endpoint for a project."""
    return reverse("web:set_project_name", kwargs={"slug_prefix": project.slug_prefix})


@pytest.mark.django_db
class TestSetProjectName:

    def test_workspace_admin_renames(self, client, setup):
        ws, project, _ = setup
        client.force_login(ws.owner)
        resp = client.post(_url(project), {"name": "New name"})
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.name == "New name"
        assert b"project-name-cell" in resp.content

    def test_project_lead_renames(self, client, setup):
        ws, project, member = setup
        project.lead = member
        project.save(update_fields=["lead"])
        client.force_login(member)
        resp = client.post(_url(project), {"name": "Lead renamed it"})
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.name == "Lead renamed it"

    def test_plain_member_is_refused(self, client, setup):
        _, project, member = setup
        client.force_login(member)
        resp = client.post(_url(project), {"name": "Nope"})
        assert resp.status_code == 403
        project.refresh_from_db()
        assert project.name == "Old name"

    def test_outsider_gets_404(self, client, setup):
        _, project, _ = setup
        client.force_login(UserFactory())
        assert client.post(_url(project), {"name": "Nope"}).status_code == 404
        project.refresh_from_db()
        assert project.name == "Old name"

    def test_blank_name_is_restored_silently(self, client, setup):
        ws, project, _ = setup
        client.force_login(ws.owner)
        resp = client.post(_url(project), {"name": "   "})
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.name == "Old name"

    def test_rename_leaves_slug_prefix_alone(self, client, setup):
        ws, project, _ = setup
        original_prefix = project.slug_prefix
        client.force_login(ws.owner)
        client.post(_url(project), {"name": "Something else entirely"})
        project.refresh_from_db()
        assert project.slug_prefix == original_prefix

    def test_response_carries_the_sidebar_oob_swap(self, client, setup):
        ws, project, _ = setup
        client.force_login(ws.owner)
        resp = client.post(_url(project), {"name": "Renamed"})
        body = resp.content.decode()
        assert f'data-project-name-for="{project.slug_prefix}"' in body
        assert "hx-swap-oob" in body

    def test_overview_hides_the_editor_from_a_plain_member(self, client, setup):
        ws, project, member = setup
        project.members.add(member)
        client.force_login(member)
        resp = client.get(project_path(project))
        body = resp.content.decode()
        assert "project-name-cell" in body
        assert "set_project_name" not in body
        assert reverse("web:set_project_name", kwargs={"slug_prefix": project.slug_prefix}) not in body
