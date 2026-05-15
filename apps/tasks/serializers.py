from django.utils.translation import gettext_lazy as _

from rest_framework import serializers

from apps.common.markdown import render_markdown
from apps.workspaces.models import WorkspaceMember

from .models import Task


class TaskSerializer(serializers.ModelSerializer):
    slug = serializers.ReadOnlyField()
    description_html = serializers.SerializerMethodField()

    class Meta:
        model = Task
        fields = [
            "id",
            "project",
            "number",
            "slug",
            "parent",
            "title",
            "description",
            "description_html",
            "status",
            "priority",
            "size",
            "due_date",
            "assignee",
            "reporter",
            "labels",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "number",
            "slug",
            "reporter",
            "created_at",
            "updated_at",
        ]

    def get_description_html(self, obj) -> str:
        """Render the task description from Markdown to sanitized HTML.

        Args:
            obj: The :class:`Task` instance.

        Returns:
            Sanitized HTML produced from ``obj.description``.
        """
        return render_markdown(obj.description)

    def validate_project(self, project):
        """Reject tasks targeted at projects the user cannot access.

        Args:
            project: The candidate :class:`Project` for the task.

        Returns:
            The validated project.

        Raises:
            serializers.ValidationError: When the user is not a member of
                the project's workspace.
        """
        user = self.context["request"].user
        if not WorkspaceMember.objects.filter(user=user, workspace=project.workspace).exists():
            raise serializers.ValidationError(_("You are not a member of this project's workspace."))
        return project

    def validate_status(self, value):
        """Ensure the submitted status is one of the known values.

        Args:
            value: The candidate status string.

        Returns:
            The validated status.

        Raises:
            serializers.ValidationError: When the status is unknown.
        """
        if value not in Task.STATUS_VALUES:
            raise serializers.ValidationError(
                _("Unknown status: %(value)s. Must be one of %(allowed)s.")
                % {"value": value, "allowed": ", ".join(Task.STATUS_VALUES)},
            )
        return value

    def validate(self, attrs):
        """Enforce cross-field invariants for tasks.

        Checks:
            * Parent and child must share a project.
            * Subtask depth is limited to one level.
            * Labels (if any) must belong to the same workspace as the
              task's project.

        Args:
            attrs: Pre-validated field values from the serializer.

        Returns:
            The validated attrs dict.

        Raises:
            serializers.ValidationError: When any invariant is violated.
        """
        parent = attrs.get("parent") or getattr(self.instance, "parent", None)
        project = attrs.get("project") or getattr(self.instance, "project", None)
        labels = attrs.get("labels")

        if parent and project and parent.project_id != project.id:
            raise serializers.ValidationError(
                {"parent": _("Subtask must be in the same project as its parent.")},
            )
        if parent and parent.parent_id is not None:
            raise serializers.ValidationError(
                {"parent": _("Subtasks cannot have their own subtasks (depth limit 1).")},
            )
        if labels and project:
            wrong = [lab.id for lab in labels if lab.workspace_id != project.workspace_id]
            if wrong:
                raise serializers.ValidationError(
                    {
                        "labels": _("Labels %(ids)s are not in this project's workspace.") % {"ids": wrong},
                    },
                )
        return attrs
