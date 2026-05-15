from django.utils.translation import gettext_lazy as _

from rest_framework import serializers

from apps.workspaces.models import WorkspaceMember

from .models import Label, LabelGroup


class LabelGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabelGroup
        fields = [
            "id",
            "workspace",
            "name",
            "is_exclusive",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
        ]

    def validate_workspace(self, workspace):
        """Reject group creation in workspaces the user has no access to.

        Args:
            workspace: The candidate :class:`Workspace` for the group.

        Returns:
            The validated workspace.

        Raises:
            serializers.ValidationError: When the user is not a member.
        """
        user = self.context["request"].user
        if not WorkspaceMember.objects.filter(user=user, workspace=workspace).exists():
            raise serializers.ValidationError(_("You are not a member of this workspace."))
        return workspace


class LabelSerializer(serializers.ModelSerializer):
    class Meta:
        model = Label
        fields = [
            "id",
            "workspace",
            "group",
            "name",
            "color",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "created_at",
        ]

    def validate_workspace(self, workspace):
        """Reject label creation in workspaces the user has no access to.

        Args:
            workspace: The candidate :class:`Workspace` for the label.

        Returns:
            The validated workspace.

        Raises:
            serializers.ValidationError: When the user is not a member.
        """
        user = self.context["request"].user
        if not WorkspaceMember.objects.filter(user=user, workspace=workspace).exists():
            raise serializers.ValidationError(_("You are not a member of this workspace."))
        return workspace

    def validate(self, attrs):
        """Ensure a label and its group belong to the same workspace.

        Args:
            attrs: Pre-validated field values from the serializer.

        Returns:
            The validated attrs dict.

        Raises:
            serializers.ValidationError: When ``group.workspace`` differs
                from ``workspace``.
        """
        workspace = attrs.get("workspace") or getattr(self.instance, "workspace", None)
        group = attrs.get("group") or getattr(self.instance, "group", None)
        if group and workspace and group.workspace_id != workspace.id:
            raise serializers.ValidationError(
                {"group": _("Label group must belong to the same workspace as the label.")},
            )
        return attrs
