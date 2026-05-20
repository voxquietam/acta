from django.utils.translation import gettext_lazy as _

from rest_framework import serializers

from apps.common.markdown import render_markdown
from apps.tasks.models import Task
from apps.workspaces.models import WorkspaceMember

from .models import Comment


class CommentSerializer(serializers.ModelSerializer):
    body_html = serializers.SerializerMethodField()
    # ``Comment.task`` is nullable at the model level (comments can target a
    # project update instead), but this REST API is task-only — project
    # update comments are created via the web composer. Pin ``task`` as
    # required so the write hooks never receive a target-less comment.
    task = serializers.PrimaryKeyRelatedField(
        queryset=Task.objects.all(),
    )

    class Meta:
        model = Comment
        fields = [
            "id",
            "task",
            "author",
            "body",
            "body_html",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "author",
            "created_at",
            "updated_at",
        ]

    def get_body_html(self, obj) -> str:
        """Render the comment body from Markdown to sanitized HTML.

        Args:
            obj: The :class:`Comment` instance.

        Returns:
            Sanitized HTML produced from ``obj.body``.
        """
        return render_markdown(obj.body)

    def validate_task(self, task):
        """Reject comments on tasks the user cannot access.

        Args:
            task: The candidate :class:`Task` to attach the comment to.

        Returns:
            The validated task.

        Raises:
            serializers.ValidationError: When the user is not a member of
                the task's workspace.
        """
        user = self.context["request"].user
        if not WorkspaceMember.objects.filter(
            user=user,
            workspace=task.project.workspace,
        ).exists():
            raise serializers.ValidationError(_("You are not a member of this task's workspace."))
        return task
