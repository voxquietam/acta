from django.db import transaction

from rest_framework import serializers

from .models import Workspace, WorkspaceMember


class WorkspaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Workspace
        fields = [
            "id",
            "name",
            "slug",
            "owner",
            "created_at",
        ]
        read_only_fields = [
            "id",
            "owner",
            "created_at",
        ]

    def create(self, validated_data):
        """Create a workspace and seed the creator as its owner-member.

        Args:
            validated_data: Serializer-validated data; ``owner`` is
                injected from the view layer.

        Returns:
            The newly created :class:`Workspace`.
        """
        with transaction.atomic():
            workspace = Workspace.objects.create(**validated_data)
            WorkspaceMember.objects.create(
                user=workspace.owner,
                workspace=workspace,
                role=WorkspaceMember.OWNER,
            )
        return workspace


class WorkspaceMemberSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceMember
        fields = [
            "id",
            "user",
            "workspace",
            "role",
            "joined_at",
        ]
        read_only_fields = [
            "id",
            "joined_at",
        ]
