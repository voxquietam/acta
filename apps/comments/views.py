from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.notifications.services import notify_comment_created
from apps.workspaces.permissions import IsAuthorOrWorkspaceAdmin

from .models import Comment
from .serializers import CommentSerializer


class CommentViewSet(viewsets.ModelViewSet):
    """CRUD for task :class:`Comment` rows.

    Any workspace member can read; writes require the user to be the
    comment's author or a workspace admin/owner. **Task-only:** the model
    is polymorphic (a comment can target a task *or* a project update),
    but project-update comments are created through the web composer, not
    this API — the queryset and serializer keep this surface task-scoped
    so the write hooks (which walk ``comment.task.project``) are safe.
    """

    serializer_class = CommentSerializer
    permission_classes = [
        IsAuthenticated,
        IsAuthorOrWorkspaceAdmin,
    ]
    filterset_fields = [
        "task",
    ]

    def get_queryset(self):
        """Return comments from tasks in workspaces the user belongs to.

        ``select_related`` pulls in the FK chain that every write hook
        (``perform_create``/``update``/``destroy``) and permission
        check (``IsAuthorOrWorkspaceAdmin.has_object_permission``)
        walks via ``comment.task.project.workspace`` — without it each
        comment write fires three extra SELECTs.

        Returns:
            A queryset of :class:`Comment` instances visible to the user.
        """
        return (
            Comment.objects.select_related(
                "task__project__workspace",
                "author",
            )
            .filter(task__project__workspace__memberships__user=self.request.user)
            .distinct()
        )

    def perform_create(self, serializer):
        """Save the comment with the request user as its author.

        Emits a ``comment.created`` activity event.

        Args:
            serializer: The validated :class:`CommentSerializer`.
        """
        comment = serializer.save(author=self.request.user)
        log_event(
            workspace=comment.task.project.workspace,
            project=comment.task.project,
            actor=self.request.user,
            event_type="comment.created",
            target_type=ActivityLog.TARGET_COMMENT,
            target_id=comment.id,
            payload={
                "task_id": comment.task_id,
                "body_preview": comment.body[:120],
            },
        )
        notify_comment_created(comment=comment, actor=self.request.user)

    def perform_update(self, serializer):
        """Save the edit and emit a ``comment.edited`` activity event.

        Args:
            serializer: The validated :class:`CommentSerializer`.
        """
        comment = serializer.save()
        log_event(
            workspace=comment.task.project.workspace,
            project=comment.task.project,
            actor=self.request.user,
            event_type="comment.edited",
            target_type=ActivityLog.TARGET_COMMENT,
            target_id=comment.id,
            payload={"task_id": comment.task_id},
        )

    def perform_destroy(self, instance):
        """Delete the comment and emit a ``comment.deleted`` activity event.

        Args:
            instance: The :class:`Comment` to delete.
        """
        workspace = instance.task.project.workspace
        project = instance.task.project
        task_id = instance.task_id
        comment_id = instance.id
        instance.delete()
        log_event(
            workspace=workspace,
            project=project,
            actor=self.request.user,
            event_type="comment.deleted",
            target_type=ActivityLog.TARGET_COMMENT,
            target_id=comment_id or 0,
            payload={"task_id": task_id},
        )
