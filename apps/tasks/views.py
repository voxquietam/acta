from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.workspaces.permissions import IsWorkspaceMember

from .models import Task
from .serializers import TaskSerializer


class TaskViewSet(viewsets.ModelViewSet):
    """CRUD for :class:`Task` scoped to user-accessible workspaces.

    Writes activity log entries for task creation and deletion via
    :func:`apps.activity.services.log_event`. Diff-based events for
    updates land in the bulk-operations stage.
    """

    serializer_class = TaskSerializer
    permission_classes = [
        IsAuthenticated,
        IsWorkspaceMember,
    ]
    filterset_fields = [
        "project",
        "status",
        "priority",
        "assignee",
        "parent",
    ]
    search_fields = [
        "title",
        "description",
    ]
    ordering_fields = [
        "updated_at",
        "created_at",
        "due_date",
        "priority",
    ]
    ordering = [
        "-updated_at",
    ]

    def get_queryset(self):
        """Return tasks from projects in workspaces the user belongs to.

        Returns:
            A queryset of :class:`Task` instances visible to the user.
        """
        return Task.objects.filter(
            project__workspace__memberships__user=self.request.user,
        ).distinct()

    def perform_create(self, serializer):
        """Save the new task and emit a ``task.created`` activity event.

        Args:
            serializer: The validated :class:`TaskSerializer`.
        """
        task = serializer.save(reporter=self.request.user)
        log_event(
            workspace=task.project.workspace,
            project=task.project,
            actor=self.request.user,
            event_type="task.created",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task.id,
            payload={
                "title": task.title,
                "project_id": task.project_id,
                "parent_id": task.parent_id,
            },
        )

    def perform_destroy(self, instance):
        """Delete the task and emit a ``task.deleted`` activity event.

        The activity event captures a snapshot of key fields so the
        timeline remains readable after the row is gone.

        Args:
            instance: The :class:`Task` to delete.
        """
        snapshot = {
            "title": instance.title,
            "project_id": instance.project_id,
            "number": instance.number,
            "status": instance.status,
        }
        workspace = instance.project.workspace
        project = instance.project
        instance.delete()
        log_event(
            workspace=workspace,
            project=project,
            actor=self.request.user,
            event_type="task.deleted",
            target_type=ActivityLog.TARGET_TASK,
            target_id=instance.id or 0,
            payload={"snapshot": snapshot},
        )
