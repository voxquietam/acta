"""SSE channel authorization."""

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.workspaces.sse import WorkspaceChannelManager
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.mark.django_db
class TestWorkspaceChannelManager:
    """``can_read_channel`` is the only authorization hook for the SSE stream."""

    def setup_method(self):
        self.manager = WorkspaceChannelManager()

    def test_member_can_read_own_workspace(self):
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)
        assert self.manager.can_read_channel(user, f"workspace-{ws.id}") is True

    def test_non_member_cannot_read(self):
        user = UserFactory()
        foreign_ws = WorkspaceFactory()  # owned by another user
        assert self.manager.can_read_channel(user, f"workspace-{foreign_ws.id}") is False

    def test_anonymous_cannot_read(self):
        ws = WorkspaceFactory()

        class _Anon:
            is_authenticated = False

        assert self.manager.can_read_channel(_Anon(), f"workspace-{ws.id}") is False

    def test_none_user_cannot_read(self):
        ws = WorkspaceFactory()
        assert self.manager.can_read_channel(None, f"workspace-{ws.id}") is False

    def test_unknown_workspace_id_rejected(self):
        user = UserFactory()
        assert self.manager.can_read_channel(user, "workspace-99999999") is False

    def test_non_workspace_channel_rejected(self):
        user = UserFactory()
        assert self.manager.can_read_channel(user, "random-channel") is False

    def test_malformed_channel_rejected(self):
        user = UserFactory()
        assert self.manager.can_read_channel(user, "workspace-abc") is False
        assert self.manager.can_read_channel(user, "workspace-") is False

    def test_added_member_can_read(self):
        """Non-owner added via membership row can also read."""
        owner = UserFactory()
        member = UserFactory()
        ws = WorkspaceFactory(owner=owner)
        WorkspaceMemberFactory(workspace=ws, user=member)
        assert self.manager.can_read_channel(member, f"workspace-{ws.id}") is True

    def test_user_can_read_own_channel(self):
        """The private ``user-<id>`` notification channel is self-only."""
        user = UserFactory()
        assert self.manager.can_read_channel(user, f"user-{user.id}") is True

    def test_user_cannot_read_others_channel(self):
        user = UserFactory()
        other = UserFactory()
        assert self.manager.can_read_channel(user, f"user-{other.id}") is False

    def test_anonymous_cannot_read_user_channel(self):
        class _Anon:
            is_authenticated = False
            id = 1

        assert self.manager.can_read_channel(_Anon(), "user-1") is False

    def test_malformed_user_channel_rejected(self):
        user = UserFactory()
        assert self.manager.can_read_channel(user, "user-abc") is False
