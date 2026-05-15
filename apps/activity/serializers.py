from rest_framework import serializers

from .models import ActivityLog


class ActivityLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActivityLog
        fields = [
            "id",
            "workspace",
            "project",
            "target_type",
            "target_id",
            "actor",
            "event_type",
            "payload",
            "bulk_id",
            "created_at",
        ]
        read_only_fields = fields
