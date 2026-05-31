"""Export workspace members as CSV."""

from django.urls import reverse

import pytest

from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def workspace(db):
    return WorkspaceFactory()


@pytest.mark.django_db
class TestExportMembersCSV:

    def test_admin_gets_csv(self, client, workspace):
        WorkspaceMemberFactory(workspace=workspace, role=WorkspaceMember.MEMBER)
        client.force_login(workspace.owner)
        resp = client.get(reverse("web:export_workspace_members_csv", args=[workspace.slug]))
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/csv")
        body = resp.content.decode()
        # Header line + at least the owner.
        assert "display_name,username,email,role,joined_at" in body.splitlines()[0]
        assert workspace.owner.username in body

    def test_member_forbidden(self, client, workspace):
        # Plain member can't export — admin gate.
        other = WorkspaceFactory().owner
        WorkspaceMemberFactory(workspace=workspace, user=other, role=WorkspaceMember.MEMBER)
        client.force_login(other)
        resp = client.get(reverse("web:export_workspace_members_csv", args=[workspace.slug]))
        assert resp.status_code == 403

    def test_filename_includes_workspace_slug(self, client, workspace):
        client.force_login(workspace.owner)
        resp = client.get(reverse("web:export_workspace_members_csv", args=[workspace.slug]))
        assert f'filename="{workspace.slug}-members.csv"' in resp.headers["Content-Disposition"]
