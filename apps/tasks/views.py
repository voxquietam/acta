from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.workspaces.permissions import IsWorkspaceMember

from .events import emit_task_diff_events, snapshot_task
from .models import Task
from .serializers import TaskSerializer


class TaskViewSet(viewsets.ModelViewSet):
    """CRUD for :class:`Task` scoped to user-accessible workspaces.

    Writes activity events on every mutation:
        * ``task.created`` on create.
        * Granular ``task.status_changed`` / ``task.assigned`` /
          ``task.due_changed`` / ``task.priority_changed`` /
          ``task.labels_changed`` / ``task.parent_changed`` plus a
          catch-all ``task.updated`` for text/size edits on update.
        * ``task.deleted`` on destroy.

    See :mod:`apps.tasks.events` for the diff helper and
    docs/decisions/0011-activity-log.md for the event contract.
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

        Eagerly loads project (with workspace), assignee, reporter, parent
        via ``select_related`` and labels via ``prefetch_related`` so list
        rendering and ``perform_*`` hooks stay O(1) in query count
        regardless of row count.

        Returns:
            A queryset of :class:`Task` instances visible to the user.
        """
        return (
            Task.objects.filter(
                project__workspace__memberships__user=self.request.user,
            )
            .select_related(
                "project__workspace",
                "assignee",
                "reporter",
                "parent",
            )
            .prefetch_related(
                "labels",
            )
            .distinct()
        )

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

    def perform_update(self, serializer):
        """Save the edit and emit granular diff events.

        Snapshots the task before mutation, applies the serializer save,
        then walks the diff via
        :func:`apps.tasks.events.emit_task_diff_events`. Each watched
        field that changed produces its own ``ActivityLog`` row.

        Args:
            serializer: The validated :class:`TaskSerializer` already
                bound to an existing instance.
        """
        instance = serializer.instance
        assert instance is not None, "perform_update is called with an instance-bound serializer"
        old_state = snapshot_task(instance)
        task = serializer.save()
        emit_task_diff_events(old_state=old_state, task=task, actor=self.request.user)

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
        task_id = instance.id
        instance.delete()
        log_event(
            workspace=workspace,
            project=project,
            actor=self.request.user,
            event_type="task.deleted",
            target_type=ActivityLog.TARGET_TASK,
            target_id=task_id or 0,
            payload={"snapshot": snapshot},
        )
