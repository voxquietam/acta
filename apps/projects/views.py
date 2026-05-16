from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.workspaces.permissions import IsAuthorOrWorkspaceAdmin, IsWorkspaceMember

from .models import Project, ProjectUpdate
from .serializers import ProjectSerializer, ProjectUpdateSerializer


class ProjectViewSet(viewsets.ModelViewSet):
    """CRUD for :class:`Project` scoped to the user's workspaces."""

    serializer_class = ProjectSerializer
    permission_classes = [
        IsAuthenticated,
        IsWorkspaceMember,
    ]
    filterset_fields = [
        "workspace",
        "archived",
    ]
    search_fields = [
        "name",
        "slug_prefix",
    ]
    ordering_fields = [
        "created_at",
        "name",
    ]

    def get_queryset(self):
        """Return projects whose workspace the request user belongs to.

        Returns:
            A queryset of :class:`Project` instances visible to the user.
        """
        return (
            Project.objects.select_related("workspace")
            .filter(workspace__memberships__user=self.request.user)
            .distinct()
        )


class ProjectUpdateViewSet(viewsets.ModelViewSet):
    """CRUD for :class:`ProjectUpdate`.

    Reads are available to any workspace member; writes require the
    user to be the update's author, or an admin/owner of the workspace.
    """

    serializer_class = ProjectUpdateSerializer
    permission_classes = [
        IsAuthenticated,
        IsAuthorOrWorkspaceAdmin,
    ]
    filterset_fields = [
        "project",
        "health",
    ]
    ordering_fields = [
        "created_at",
    ]

    def get_queryset(self):
        """Return project updates from workspaces the user belongs to.

        Returns:
            A queryset of :class:`ProjectUpdate` instances visible to the
            user.
        """
        return (
            ProjectUpdate.objects.select_related("project__workspace", "author")
            .filter(project__workspace__memberships__user=self.request.user)
            .distinct()
        )

    def perform_create(self, serializer):
        """Save the update with the request user as its author.

        Args:
            serializer: The validated :class:`ProjectUpdateSerializer`.
        """
        serializer.save(author=self.request.user)
