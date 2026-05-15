from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.workspaces.permissions import IsWorkspaceMember

from .models import ActivityLog
from .serializers import ActivityLogSerializer


class ActivityLogViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only feed of :class:`ActivityLog` entries.

    The activity log is append-only and immutable; mutations happen only
    through :func:`apps.activity.services.log_event`. Exposes list and
    retrieve actions filterable by workspace, project, target_type, and
    event_type.
    """

    serializer_class = ActivityLogSerializer
    permission_classes = [
        IsAuthenticated,
        IsWorkspaceMember,
    ]
    filterset_fields = [
        "workspace",
        "project",
        "target_type",
        "event_type",
        "actor",
        "bulk_id",
    ]
    ordering_fields = [
        "created_at",
    ]
    ordering = [
        "-created_at",
    ]

    def get_queryset(self):
        """Return activity events from workspaces the user belongs to.

        Returns:
            A queryset of :class:`ActivityLog` rows visible to the user.
        """
        return ActivityLog.objects.filter(
            workspace__memberships__user=self.request.user,
        ).distinct()
