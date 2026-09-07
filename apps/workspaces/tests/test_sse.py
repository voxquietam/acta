"""SSE channel authorization."""

from django.urls import resolve

from asgiref.sync import async_to_sync
import pytest

from apps.accounts.tests.factories import UserFactory
from apps.workspaces.sse import WorkspaceChannelManager
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


def _stream_body(response) -> str:
    """Drain a refused SSE response into text.

    The eventstream view is async, so ``streaming_content`` is an async
    iterator. Safe only for responses that end — an authorised stream
    never does.
    """

    async def collect():
        return b"".join([chunk async for chunk in response.streaming_content])

    return async_to_sync(collect)().decode()


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


@pytest.mark.django_db
class TestCombinedStreamEndpoint:
    """``/events/stream`` carries every channel a tab needs on ONE connection.

    The channel list travels in the querystring, so these cover the part
    that matters: an unauthorised name in the URL is refused rather than
    trusted. Only the refusing cases consume the response — an authorised
    stream never ends, so asserting on its body would hang the suite.
    """

    def test_route_carries_no_channel_kwargs(self):
        """``?channel=`` is authoritative only while the route sets no channels.

        django_eventstream resolves ``format-channels`` / ``channels`` view
        kwargs ahead of the querystring; adding either to this route would
        silently pin every tab to one channel again.
        """
        match = resolve("/events/stream")
        assert "format-channels" not in match.kwargs
        assert "channels" not in match.kwargs
        assert "channel" not in match.kwargs

    def test_foreign_workspace_channel_is_refused(self, client):
        user = UserFactory()
        WorkspaceFactory(owner=user)
        stranger_ws = WorkspaceFactory(owner=UserFactory())
        client.force_login(user)
        resp = client.get("/events/stream", {"channel": f"workspace-{stranger_ws.id}"})
        body = _stream_body(resp)
        assert "stream-error" in body
        assert "Permission denied" in body

    def test_another_users_private_channel_is_refused(self, client):
        user = UserFactory()
        other = UserFactory()
        client.force_login(user)
        resp = client.get("/events/stream", {"channel": f"user-{other.id}"})
        body = _stream_body(resp)
        assert "Permission denied" in body

    def test_one_bad_channel_refuses_the_whole_stream(self, client):
        """A tab may not smuggle a channel in alongside legitimate ones."""
        user = UserFactory()
        ws = WorkspaceFactory(owner=user)
        stranger_ws = WorkspaceFactory(owner=UserFactory())
        client.force_login(user)
        resp = client.get(
            "/events/stream",
            {"channel": [f"workspace-{ws.id}", f"workspace-{stranger_ws.id}"]},
        )
        body = _stream_body(resp)
        assert "Permission denied" in body

    def test_anonymous_is_refused(self, client):
        ws = WorkspaceFactory(owner=UserFactory())
        resp = client.get("/events/stream", {"channel": f"workspace-{ws.id}"})
        body = _stream_body(resp)
        assert "Permission denied" in body
