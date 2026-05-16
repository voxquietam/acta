from django.db import transaction

from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

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
    """Serializer for ``WorkspaceMember`` with role-tier enforcement.

    The ``validate`` hook prevents a non-owner from assigning the
    ``owner`` role to anyone — without it an admin could promote a
    peer to owner and then be removed by them. ``IsWorkspaceAdmin``
    blocks non-admins from even reaching this layer; the role-tier
    check here is the second wall.
    """

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

    def validate(self, attrs):
        """Refuse owner-role assignments from non-owners.

        Only the workspace's current owner can grant or transfer the
        ``owner`` role. Admins can manage ``member`` / ``admin`` rows
        — that's enough to invite, demote, or remove peers without
        risking accidental ownership transfer.
        """
        role = attrs.get("role")
        workspace = attrs.get("workspace") or getattr(self.instance, "workspace", None)
        request = self.context.get("request")
        if role == WorkspaceMember.OWNER and request is not None and workspace is not None:
            acting = WorkspaceMember.objects.filter(
                user=request.user,
                workspace=workspace,
            ).first()
            if acting is None or acting.role != WorkspaceMember.OWNER:
                raise PermissionDenied("Only the workspace owner can grant the owner role.")
        return attrs
