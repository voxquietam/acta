"""WorkspaceMemberViewSet authorization."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.mark.django_db
class TestWorkspaceMemberCreatePermissions:
    """``POST /api/v1/workspace-members/`` is admin-only.

    Before the ``has_permission`` check on ``IsWorkspaceAdmin`` was
    added, any authenticated user could ``POST`` a row promoting
    themselves to ``owner`` of any workspace. These tests pin that
    door shut.
    """

    def setup_method(self):
        self.client = APIClient()

    def test_non_member_cannot_create_membership_in_foreign_workspace(self):
        attacker = UserFactory()
        target_ws = WorkspaceFactory()  # owned by someone else
        self.client.force_authenticate(attacker)
        resp = self.client.post(
            "/api/v1/workspace-members/",
            {"user": attacker.id, "workspace": target_ws.id, "role": "owner"},
        )
        assert resp.status_code in (403, 400), resp.content
        assert not WorkspaceMember.objects.filter(user=attacker, workspace=target_ws).exists()

    def test_member_but_not_admin_cannot_invite(self):
        owner = UserFactory()
        regular = UserFactory()
        invitee = UserFactory()
        ws = WorkspaceFactory(owner=owner)
        WorkspaceMemberFactory(workspace=ws, user=regular, role=WorkspaceMember.MEMBER)
        self.client.force_authenticate(regular)
        resp = self.client.post(
            "/api/v1/workspace-members/",
            {"user": invitee.id, "workspace": ws.id, "role": "member"},
        )
        assert resp.status_code in (403, 400)
        assert not WorkspaceMember.objects.filter(user=invitee, workspace=ws).exists()

    def test_admin_can_invite_new_member(self):
        owner = UserFactory()
        admin = UserFactory()
        invitee = UserFactory()
        ws = WorkspaceFactory(owner=owner)
        WorkspaceMemberFactory(workspace=ws, user=admin, role=WorkspaceMember.ADMIN)
        self.client.force_authenticate(admin)
        resp = self.client.post(
            "/api/v1/workspace-members/",
            {"user": invitee.id, "workspace": ws.id, "role": "member"},
        )
        assert resp.status_code == 201, resp.content
        assert WorkspaceMember.objects.filter(user=invitee, workspace=ws).exists()

    def test_missing_workspace_field_rejected(self):
        attacker = UserFactory()
        self.client.force_authenticate(attacker)
        resp = self.client.post(
            "/api/v1/workspace-members/",
            {"user": attacker.id, "role": "owner"},
        )
        assert resp.status_code in (400, 403)

    def test_garbage_workspace_id_rejected(self):
        attacker = UserFactory()
        self.client.force_authenticate(attacker)
        resp = self.client.post(
            "/api/v1/workspace-members/",
            {"user": attacker.id, "workspace": "not-a-number", "role": "owner"},
        )
        assert resp.status_code in (400, 403)

    def test_admin_cannot_promote_peer_to_owner(self):
        """Even an admin can't grant the owner role to anyone else.

        Otherwise an admin could promote themselves indirectly: invite
        a sock-puppet account as ``owner``, log in as that account,
        demote the real owner. Only an existing owner can grant
        ``owner``.
        """
        owner = UserFactory()
        admin = UserFactory()
        ws = WorkspaceFactory(owner=owner)
        WorkspaceMemberFactory(workspace=ws, user=admin, role=WorkspaceMember.ADMIN)
        target = UserFactory()
        self.client.force_authenticate(admin)
        resp = self.client.post(
            "/api/v1/workspace-members/",
            {"user": target.id, "workspace": ws.id, "role": "owner"},
        )
        assert resp.status_code in (400, 403), resp.content
        assert not WorkspaceMember.objects.filter(
            user=target,
            workspace=ws,
            role=WorkspaceMember.OWNER,
        ).exists()

    def test_owner_can_grant_owner_role(self):
        owner = UserFactory()
        ws = WorkspaceFactory(owner=owner)
        target = UserFactory()
        self.client.force_authenticate(owner)
        resp = self.client.post(
            "/api/v1/workspace-members/",
            {"user": target.id, "workspace": ws.id, "role": "owner"},
        )
        assert resp.status_code == 201, resp.content
        assert WorkspaceMember.objects.filter(
            user=target,
            workspace=ws,
            role=WorkspaceMember.OWNER,
        ).exists()
