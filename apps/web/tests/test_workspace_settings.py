"""Tests for the workspace settings page + member-management endpoints.

Covers GET access control, the add / set-role / remove flows, and the
admin-only / owner-protected invariants from ADR 0010.
"""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def workspace(db):
    return WorkspaceFactory()


@pytest.fixture
def owner(workspace):
    return workspace.owner


@pytest.fixture
def admin_user(workspace):
    user = UserFactory()
    WorkspaceMemberFactory(workspace=workspace, user=user, role=WorkspaceMember.ADMIN)
    return user


@pytest.fixture
def regular_member(workspace):
    user = UserFactory()
    WorkspaceMemberFactory(workspace=workspace, user=user, role=WorkspaceMember.MEMBER)
    return user


@pytest.fixture
def outsider(db):
    return UserFactory()


def _settings_url(workspace):
    return reverse("web:workspace_settings", kwargs={"slug": workspace.slug})


def _add_url(workspace):
    return reverse("web:add_workspace_member", kwargs={"slug": workspace.slug})


def _role_url(workspace, user_id):
    return reverse(
        "web:set_workspace_member_role",
        kwargs={"slug": workspace.slug, "user_id": user_id},
    )


def _remove_url(workspace, user_id):
    return reverse(
        "web:remove_workspace_member",
        kwargs={"slug": workspace.slug, "user_id": user_id},
    )


@pytest.mark.django_db
class TestSettingsPage:

    def test_owner_can_view(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.get(_settings_url(workspace))
        assert resp.status_code == 200
        assert b"Members" in resp.content or "Members".encode() in resp.content

    def test_member_can_view(self, client, workspace, regular_member):
        client.force_login(regular_member)
        resp = client.get(_settings_url(workspace))
        assert resp.status_code == 200

    def test_outsider_gets_404(self, client, workspace, outsider):
        client.force_login(outsider)
        resp = client.get(_settings_url(workspace))
        assert resp.status_code == 404

    def test_anonymous_redirects_to_login(self, client, workspace):
        resp = client.get(_settings_url(workspace))
        assert resp.status_code in (301, 302)

    def test_unknown_workspace_404(self, client, owner):
        client.force_login(owner)
        resp = client.get(reverse("web:workspace_settings", kwargs={"slug": "nope"}))
        assert resp.status_code == 404


@pytest.mark.django_db
class TestAddMember:

    def test_admin_can_add_member(self, client, workspace, admin_user, outsider):
        client.force_login(admin_user)
        resp = client.post(
            _add_url(workspace),
            {"user_id": outsider.id, "role": WorkspaceMember.MEMBER},
        )
        assert resp.status_code == 200
        assert WorkspaceMember.objects.filter(
            workspace=workspace,
            user=outsider,
            role=WorkspaceMember.MEMBER,
        ).exists()

    def test_owner_can_add_admin(self, client, workspace, owner, outsider):
        client.force_login(owner)
        resp = client.post(
            _add_url(workspace),
            {"user_id": outsider.id, "role": WorkspaceMember.ADMIN},
        )
        assert resp.status_code == 200
        assert (
            WorkspaceMember.objects.get(
                workspace=workspace,
                user=outsider,
            ).role
            == WorkspaceMember.ADMIN
        )

    def test_regular_member_cannot_add(self, client, workspace, regular_member, outsider):
        client.force_login(regular_member)
        resp = client.post(_add_url(workspace), {"user_id": outsider.id})
        assert resp.status_code == 400
        assert not WorkspaceMember.objects.filter(
            workspace=workspace,
            user=outsider,
        ).exists()

    def test_cannot_add_second_owner(self, client, workspace, owner, outsider):
        client.force_login(owner)
        resp = client.post(
            _add_url(workspace),
            {"user_id": outsider.id, "role": WorkspaceMember.OWNER},
        )
        assert resp.status_code == 400

    def test_cannot_add_invalid_role(self, client, workspace, owner, outsider):
        client.force_login(owner)
        resp = client.post(_add_url(workspace), {"user_id": outsider.id, "role": "queen"})
        assert resp.status_code == 400

    def test_cannot_add_unknown_user(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.post(_add_url(workspace), {"user_id": 999999})
        assert resp.status_code == 400

    def test_cannot_re_add_existing_member(self, client, workspace, owner, regular_member):
        client.force_login(owner)
        resp = client.post(_add_url(workspace), {"user_id": regular_member.id})
        assert resp.status_code == 400

    def test_get_not_allowed(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.get(_add_url(workspace))
        assert resp.status_code == 405

    def test_outsider_admin_check_first(self, client, workspace, outsider):
        client.force_login(outsider)
        resp = client.post(_add_url(workspace), {"user_id": outsider.id})
        # Outsider isn't a workspace member at all → 404 from the
        # ``_get_user_workspace_or_404`` guard, not 400.
        assert resp.status_code == 404


@pytest.mark.django_db
class TestSetRole:

    def test_admin_can_promote_member_to_admin(self, client, workspace, owner, regular_member):
        client.force_login(owner)
        resp = client.post(
            _role_url(workspace, regular_member.id),
            {"role": WorkspaceMember.ADMIN},
        )
        assert resp.status_code == 200
        assert (
            WorkspaceMember.objects.get(
                workspace=workspace,
                user=regular_member,
            ).role
            == WorkspaceMember.ADMIN
        )

    def test_admin_can_demote_admin_to_member(self, client, workspace, owner, admin_user):
        client.force_login(owner)
        resp = client.post(
            _role_url(workspace, admin_user.id),
            {"role": WorkspaceMember.MEMBER},
        )
        assert resp.status_code == 200
        assert (
            WorkspaceMember.objects.get(
                workspace=workspace,
                user=admin_user,
            ).role
            == WorkspaceMember.MEMBER
        )

    def test_cannot_set_owner_role(self, client, workspace, owner, regular_member):
        client.force_login(owner)
        resp = client.post(
            _role_url(workspace, regular_member.id),
            {"role": WorkspaceMember.OWNER},
        )
        assert resp.status_code == 400

    def test_cannot_demote_owner(self, client, workspace, owner):
        # Owner tries to demote themselves → blocked, must transfer first.
        client.force_login(owner)
        resp = client.post(
            _role_url(workspace, owner.id),
            {"role": WorkspaceMember.ADMIN},
        )
        assert resp.status_code == 400
        assert (
            WorkspaceMember.objects.get(
                workspace=workspace,
                user=owner,
            ).role
            == WorkspaceMember.OWNER
        )

    def test_regular_member_cannot_change_role(self, client, workspace, regular_member, admin_user):
        client.force_login(regular_member)
        resp = client.post(
            _role_url(workspace, admin_user.id),
            {"role": WorkspaceMember.MEMBER},
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestRemoveMember:

    def test_admin_can_remove_member(self, client, workspace, owner, regular_member):
        client.force_login(owner)
        resp = client.post(_remove_url(workspace, regular_member.id))
        assert resp.status_code == 200
        assert not WorkspaceMember.objects.filter(
            workspace=workspace,
            user=regular_member,
        ).exists()

    def test_cannot_remove_owner(self, client, workspace, owner, admin_user):
        client.force_login(admin_user)
        resp = client.post(_remove_url(workspace, owner.id))
        assert resp.status_code == 400
        assert WorkspaceMember.objects.filter(
            workspace=workspace,
            user=owner,
        ).exists()

    def test_regular_member_cannot_remove(self, client, workspace, regular_member, admin_user):
        client.force_login(regular_member)
        resp = client.post(_remove_url(workspace, admin_user.id))
        assert resp.status_code == 400
        assert WorkspaceMember.objects.filter(
            workspace=workspace,
            user=admin_user,
        ).exists()

    def test_remove_unknown_user(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.post(_remove_url(workspace, 999999))
        assert resp.status_code == 400

    def test_outsider_blocked(self, client, workspace, outsider, regular_member):
        client.force_login(outsider)
        resp = client.post(_remove_url(workspace, regular_member.id))
        assert resp.status_code == 404


@pytest.mark.django_db
class TestPartialResponse:
    """The mutation endpoints return the members-panel partial so HTMX
    can swap it in place. Verify the swap target id is in the response.
    """

    def test_add_returns_members_partial(self, client, workspace, owner, outsider):
        client.force_login(owner)
        resp = client.post(_add_url(workspace), {"user_id": outsider.id})
        assert resp.status_code == 200
        assert b'id="workspace-members"' in resp.content

    def test_role_change_returns_members_partial(self, client, workspace, owner, regular_member):
        client.force_login(owner)
        resp = client.post(
            _role_url(workspace, regular_member.id),
            {"role": WorkspaceMember.ADMIN},
        )
        assert resp.status_code == 200
        assert b'id="workspace-members"' in resp.content

    def test_remove_returns_members_partial(self, client, workspace, owner, regular_member):
        client.force_login(owner)
        resp = client.post(_remove_url(workspace, regular_member.id))
        assert resp.status_code == 200
        assert b'id="workspace-members"' in resp.content


def _general_url(workspace):
    return reverse("web:set_workspace_general", kwargs={"slug": workspace.slug})


@pytest.mark.django_db
class TestSetWorkspaceGeneral:
    """The General settings panel: name + auto-archive + announcements."""

    def test_admin_saves_name_and_policy(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.post(
            _general_url(workspace),
            {"name": "Renamed WS", "auto_archive_done_after_days": "14", "allow_member_announcements": "on"},
        )
        assert resp.status_code == 302
        workspace.refresh_from_db()
        assert workspace.name == "Renamed WS"
        assert workspace.auto_archive_done_after_days == 14
        assert workspace.allow_member_announcements is True

    def test_blank_archive_disables_and_unchecked_toggle_off(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.post(_general_url(workspace), {"name": "X", "auto_archive_done_after_days": ""})
        assert resp.status_code == 302
        workspace.refresh_from_db()
        assert workspace.auto_archive_done_after_days is None
        assert workspace.allow_member_announcements is False

    def test_zero_archive_means_never(self, client, workspace, owner):
        client.force_login(owner)
        client.post(_general_url(workspace), {"name": "X", "auto_archive_done_after_days": "0"})
        workspace.refresh_from_db()
        assert workspace.auto_archive_done_after_days is None

    def test_empty_name_rejected(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.post(_general_url(workspace), {"name": "   "})
        assert resp.status_code == 400

    def test_non_admin_forbidden(self, client, workspace, regular_member):
        client.force_login(regular_member)
        resp = client.post(_general_url(workspace), {"name": "Nope"})
        assert resp.status_code == 403
        workspace.refresh_from_db()
        assert workspace.name != "Nope"

    def test_htmx_swaps_card_in_place(self, client, workspace, owner):
        client.force_login(owner)
        resp = client.post(
            _general_url(workspace),
            {"name": "HX WS", "auto_archive_done_after_days": "30"},
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 200
        assert b'id="workspace-general"' in resp.content
        assert "acta:toast" in resp.headers.get("HX-Trigger", "")
