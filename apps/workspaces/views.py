from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from .models import Workspace, WorkspaceMember
from .permissions import IsWorkspaceAdmin, IsWorkspaceMember
from .serializers import WorkspaceMemberSerializer, WorkspaceSerializer


class WorkspaceViewSet(viewsets.ModelViewSet):
    """CRUD for :class:`Workspace`.

    Lists only workspaces the request user is a member of. Creating a
    workspace makes the creator its owner and seeds a corresponding
    :class:`WorkspaceMember` row.
    """

    serializer_class = WorkspaceSerializer
    permission_classes = [
        IsAuthenticated,
        IsWorkspaceMember,
    ]

    def get_queryset(self):
        """Return workspaces where the request user has a membership.

        Returns:
            A queryset of :class:`Workspace` instances accessible to the
            authenticated user.
        """
        return Workspace.objects.filter(memberships__user=self.request.user).distinct()

    def perform_create(self, serializer):
        """Save the new workspace with the request user as its owner.

        Args:
            serializer: The validated :class:`WorkspaceSerializer`.
        """
        serializer.save(owner=self.request.user)


class WorkspaceMemberViewSet(viewsets.ModelViewSet):
    """CRUD for :class:`WorkspaceMember`.

    Reads are available to any member of the workspace; writes
    (invite / remove / role change) require admin or owner role.
    """

    serializer_class = WorkspaceMemberSerializer
    permission_classes = [
        IsAuthenticated,
        IsWorkspaceMember,
    ]

    def get_permissions(self):
        """Promote permission requirements for write methods.

        Returns:
            The list of :class:`BasePermission` instances applicable to
            the current action.
        """
        if self.action in {"create", "update", "partial_update", "destroy"}:
            return [IsAuthenticated(), IsWorkspaceAdmin()]
        return [IsAuthenticated(), IsWorkspaceMember()]

    def get_queryset(self):
        """Return memberships of workspaces the request user belongs to.

        Returns:
            A queryset of :class:`WorkspaceMember` instances visible to
            the authenticated user.
        """
        return WorkspaceMember.objects.filter(
            workspace__memberships__user=self.request.user,
        ).distinct()
