"""Integration tests for ``WorkspaceViewSet`` and the read / update /
delete actions of ``WorkspaceMemberViewSet``.

Membership *create* authorization is covered separately in
``test_permissions.py``; this module covers workspace CRUD scoping plus
the role-change (PATCH) and removal (DELETE) write paths.
"""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.workspaces.models import Workspace, WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def owner():
    return UserFactory()


@pytest.fixture
def workspace(owner):
    return WorkspaceFactory(owner=owner)


@pytest.fixture
def client(owner):
    c = APIClient()
    c.force_authenticate(owner)
    return c


@pytest.mark.django_db
class TestWorkspaceCrud:
    def test_create_makes_creator_owner(self, client, owner):
        resp = client.post("/api/v1/workspaces/", {"name": "Acme", "slug": "acme"})
        assert resp.status_code == 201, resp.content
        ws = Workspace.objects.get(id=resp.data["id"])
        assert ws.owner == owner
        assert WorkspaceMember.objects.filter(
            workspace=ws,
            user=owner,
            role=WorkspaceMember.OWNER,
        ).exists()

    def test_owner_is_read_only_from_payload(self, client, owner):
        other = UserFactory()
        resp = client.post(
            "/api/v1/workspaces/",
            {"name": "x", "slug": "xx", "owner": other.id},
        )
        assert resp.status_code == 201
        assert Workspace.objects.get(id=resp.data["id"]).owner == owner

    def test_list_only_own_workspaces(self, client, workspace):
        WorkspaceFactory()  # foreign
        resp = client.get("/api/v1/workspaces/")
        ids = {row["id"] for row in resp.data["results"]}
        assert ids == {workspace.id}

    def test_retrieve_foreign_workspace_404(self, client):
        foreign = WorkspaceFactory()
        resp = client.get(f"/api/v1/workspaces/{foreign.id}/")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestWorkspaceMemberWrites:
    def test_list_scoped_to_own_workspaces(self, client, workspace, owner):
        WorkspaceMemberFactory()  # membership in a foreign workspace
        resp = client.get("/api/v1/workspace-members/")
        ws_ids = {row["workspace"] for row in resp.data["results"]}
        assert ws_ids == {workspace.id}

    def test_admin_can_change_member_role(self, workspace):
        admin = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=admin, role=WorkspaceMember.ADMIN)
        target = WorkspaceMemberFactory(workspace=workspace, role=WorkspaceMember.MEMBER)
        client = APIClient()
        client.force_authenticate(admin)
        resp = client.patch(
            f"/api/v1/workspace-members/{target.id}/",
            {"role": WorkspaceMember.ADMIN},
        )
        assert resp.status_code == 200, resp.content
        target.refresh_from_db()
        assert target.role == WorkspaceMember.ADMIN

    def test_member_cannot_change_roles(self, workspace):
        regular = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=regular, role=WorkspaceMember.MEMBER)
        target = WorkspaceMemberFactory(workspace=workspace, role=WorkspaceMember.MEMBER)
        client = APIClient()
        client.force_authenticate(regular)
        resp = client.patch(
            f"/api/v1/workspace-members/{target.id}/",
            {"role": WorkspaceMember.ADMIN},
        )
        assert resp.status_code == 403
        target.refresh_from_db()
        assert target.role == WorkspaceMember.MEMBER

    def test_admin_cannot_promote_to_owner(self, workspace):
        admin = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=admin, role=WorkspaceMember.ADMIN)
        target = WorkspaceMemberFactory(workspace=workspace, role=WorkspaceMember.MEMBER)
        client = APIClient()
        client.force_authenticate(admin)
        resp = client.patch(
            f"/api/v1/workspace-members/{target.id}/",
            {"role": WorkspaceMember.OWNER},
        )
        assert resp.status_code in (400, 403)
        target.refresh_from_db()
        assert target.role != WorkspaceMember.OWNER

    def test_admin_can_remove_member(self, workspace):
        admin = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=admin, role=WorkspaceMember.ADMIN)
        target = WorkspaceMemberFactory(workspace=workspace, role=WorkspaceMember.MEMBER)
        client = APIClient()
        client.force_authenticate(admin)
        resp = client.delete(f"/api/v1/workspace-members/{target.id}/")
        assert resp.status_code == 204
        assert not WorkspaceMember.objects.filter(id=target.id).exists()

    def test_member_cannot_remove_others(self, workspace):
        regular = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=regular, role=WorkspaceMember.MEMBER)
        target = WorkspaceMemberFactory(workspace=workspace, role=WorkspaceMember.MEMBER)
        client = APIClient()
        client.force_authenticate(regular)
        resp = client.delete(f"/api/v1/workspace-members/{target.id}/")
        assert resp.status_code == 403
        assert WorkspaceMember.objects.filter(id=target.id).exists()
