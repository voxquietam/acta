from rest_framework import serializers

from apps.common.markdown import render_markdown
from apps.workspaces.models import WorkspaceMember

from .models import Project, ProjectUpdate


class ProjectSerializer(serializers.ModelSerializer):
    description_html = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "id",
            "workspace",
            "name",
            "description",
            "description_html",
            "slug_prefix",
            "next_task_number",
            "archived",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "next_task_number",
            "created_at",
        ]

    def get_description_html(self, obj) -> str:
        """Render the project description from Markdown to sanitized HTML.

        Args:
            obj: The :class:`Project` instance.

        Returns:
            Sanitized HTML produced from ``obj.description``.
        """
        return render_markdown(obj.description)

    def validate_workspace(self, workspace):
        """Reject project creation in workspaces the user has no access to.

        Args:
            workspace: The candidate :class:`Workspace` for the project.

        Returns:
            The validated workspace.

        Raises:
            serializers.ValidationError: When the request user is not a
                member of the workspace.
        """
        user = self.context["request"].user
        if not WorkspaceMember.objects.filter(user=user, workspace=workspace).exists():
            raise serializers.ValidationError("You are not a member of this workspace.")
        return workspace


class ProjectUpdateSerializer(serializers.ModelSerializer):
    body_html = serializers.SerializerMethodField()

    class Meta:
        model = ProjectUpdate
        fields = [
            "id",
            "project",
            "author",
            "health",
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
        """Render the update body from Markdown to sanitized HTML.

        Args:
            obj: The :class:`ProjectUpdate` instance.

        Returns:
            Sanitized HTML produced from ``obj.body``.
        """
        return render_markdown(obj.body)
