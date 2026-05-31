"""Workspace member-defaults policy: config + invite-time effects + endpoint."""

from django.urls import reverse

import pytest

from apps.accounts.adapters import claim_invite_for_user
from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.workspaces.models import WorkspaceInvite, WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def workspace(db):
    return WorkspaceFactory()


class TestMemberDefaultsConfig:

    @pytest.mark.django_db
    def test_default_role_falls_back_to_none(self, workspace):
        cfg = workspace.member_defaults_config()
        assert cfg["default_role"] is None
        assert cfg["auto_add_to_all_projects"] is False

    @pytest.mark.django_db
    def test_unknown_role_dropped(self, workspace):
        workspace.member_defaults = {"default_role": "ceo"}
        workspace.save(update_fields=["member_defaults"])
        assert workspace.member_defaults_config()["default_role"] is None

    @pytest.mark.django_db
    def test_round_trip(self, workspace):
        workspace.member_defaults = {"default_role": "admin", "auto_add_to_all_projects": True}
        workspace.save(update_fields=["member_defaults"])
        cfg = workspace.member_defaults_config()
        assert cfg["default_role"] == "admin"
        assert cfg["auto_add_to_all_projects"] is True


@pytest.mark.django_db
class TestInviteCreationUsesDefaultRole:

    def test_default_role_pre_selected_when_form_omits(self, client, workspace):
        workspace.member_defaults = {"default_role": "admin"}
        workspace.save(update_fields=["member_defaults"])
        client.force_login(workspace.owner)
        resp = client.post(
            reverse("web:create_workspace_invite", args=[workspace.slug]),
            {"email": "newcomer@example.com"},  # role intentionally omitted
        )
        assert resp.status_code == 200
        invite = WorkspaceInvite.objects.get(email="newcomer@example.com")
        assert invite.role == "admin"

    def test_form_role_wins_over_default(self, client, workspace):
        workspace.member_defaults = {"default_role": "admin"}
        workspace.save(update_fields=["member_defaults"])
        client.force_login(workspace.owner)
        client.post(
            reverse("web:create_workspace_invite", args=[workspace.slug]),
            {"email": "newcomer@example.com", "role": "member"},
        )
        invite = WorkspaceInvite.objects.get(email="newcomer@example.com")
        assert invite.role == "member"


@pytest.mark.django_db
class TestAutoAddOnInviteAcceptance:

    def _invite(self, workspace, email="user@example.com"):
        return WorkspaceInvite.generate(
            workspace=workspace,
            email=email,
            role="member",
            created_by=workspace.owner,
        )

    def test_auto_add_enrols_into_every_project(self, rf, workspace):
        p1 = ProjectFactory(workspace=workspace)
        p2 = ProjectFactory(workspace=workspace)
        workspace.member_defaults = {"auto_add_to_all_projects": True}
        workspace.save(update_fields=["member_defaults"])
        invite = self._invite(workspace)
        user = UserFactory()
        request = rf.get("/")
        request.session = {}
        claim_invite_for_user(request, user, invite)
        assert p1.members.filter(pk=user.pk).exists()
        assert p2.members.filter(pk=user.pk).exists()

    def test_auto_add_off_does_not_enrol(self, rf, workspace):
        ProjectFactory(workspace=workspace)
        invite = self._invite(workspace)
        user = UserFactory()
        request = rf.get("/")
        request.session = {}
        claim_invite_for_user(request, user, invite)
        # Membership exists at workspace level…
        assert WorkspaceMember.objects.filter(workspace=workspace, user=user).exists()
        # …but no project auto-add.
        assert not workspace.projects.filter(members=user).exists()


@pytest.mark.django_db
class TestSettingsEndpoint:

    def test_admin_saves_defaults(self, client, workspace):
        client.force_login(workspace.owner)
        resp = client.post(
            reverse("web:set_workspace_member_defaults", args=[workspace.slug]),
            {"default_role": "admin", "auto_add_to_all_projects": "on"},
        )
        assert resp.status_code in (200, 302)
        workspace.refresh_from_db()
        cfg = workspace.member_defaults_config()
        assert cfg["default_role"] == "admin"
        assert cfg["auto_add_to_all_projects"] is True

    def test_unknown_role_falls_back_to_member(self, client, workspace):
        client.force_login(workspace.owner)
        client.post(
            reverse("web:set_workspace_member_defaults", args=[workspace.slug]),
            {"default_role": "ceo"},
        )
        workspace.refresh_from_db()
        # Endpoint coerces unknown values to "member" before save, so the
        # stored policy is a safe role rather than the platform default.
        assert workspace.member_defaults_config()["default_role"] == "member"
