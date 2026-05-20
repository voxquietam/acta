"""SSE channel authorization.

Per docs/decisions/0015-real-time.md, only workspace members may
subscribe to the workspace SSE stream. ``django_eventstream`` invokes
:meth:`WorkspaceChannelManager.can_read_channel` both on initial
connect and on every event filter pass, so a revoked membership
terminates the stream on the next poll.
"""

from __future__ import annotations

from django_eventstream.channelmanager import DefaultChannelManager

from .models import WorkspaceMember


class WorkspaceChannelManager(DefaultChannelManager):
    """Restrict SSE channel reads to authenticated workspace members.

    Channels are named ``workspace-<id>``. ``can_read_channel`` parses
    the id, checks ``WorkspaceMember`` for the requesting user, and
    returns False (which ``django_eventstream`` turns into a 403) when
    membership is missing — be it a non-member trying to subscribe,
    an anonymous request, or a previously-valid session whose
    membership was revoked mid-stream.
    """

    def can_read_channel(self, user, channel: str) -> bool:
        """Return True iff ``user`` may read the channel.

        Two channel families are authorized:

        * ``workspace-<id>`` — readable by any member of that workspace.
        * ``user-<id>`` — the private per-user notification stream,
          readable only by the user whose id matches.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return False
        if channel.startswith("user-"):
            try:
                target_user_id = int(channel.split("-", 1)[1])
            except (ValueError, IndexError):
                return False
            return target_user_id == user.id
        if not channel.startswith("workspace-"):
            return False
        try:
            workspace_id = int(channel.split("-", 1)[1])
        except (ValueError, IndexError):
            return False
        return WorkspaceMember.objects.filter(
            user=user,
            workspace_id=workspace_id,
        ).exists()
