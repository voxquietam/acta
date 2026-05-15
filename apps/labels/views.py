from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.workspaces.permissions import IsWorkspaceMember

from .models import Label, LabelGroup
from .serializers import LabelGroupSerializer, LabelSerializer


class LabelGroupViewSet(viewsets.ModelViewSet):
    """CRUD for :class:`LabelGroup` scoped to user-accessible workspaces."""

    serializer_class = LabelGroupSerializer
    permission_classes = [
        IsAuthenticated,
        IsWorkspaceMember,
    ]
    filterset_fields = [
        "workspace",
        "is_exclusive",
    ]
    search_fields = [
        "name",
    ]

    def get_queryset(self):
        """Return label groups from workspaces the user belongs to.

        Returns:
            A queryset of :class:`LabelGroup` instances visible to the
            user.
        """
        return LabelGroup.objects.filter(
            workspace__memberships__user=self.request.user,
        ).distinct()


class LabelViewSet(viewsets.ModelViewSet):
    """CRUD for :class:`Label` scoped to user-accessible workspaces."""

    serializer_class = LabelSerializer
    permission_classes = [
        IsAuthenticated,
        IsWorkspaceMember,
    ]
    filterset_fields = [
        "workspace",
        "group",
    ]
    search_fields = [
        "name",
    ]

    def get_queryset(self):
        """Return labels from workspaces the user belongs to.

        Returns:
            A queryset of :class:`Label` instances visible to the user.
        """
        return Label.objects.filter(
            workspace__memberships__user=self.request.user,
        ).distinct()
