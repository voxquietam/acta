from django.contrib import admin

from unfold.admin import ModelAdmin

from .models import ActivityLog


@admin.register(ActivityLog)
class ActivityLogAdmin(ModelAdmin):
    list_display = [
        "created_at",
        "event_type",
        "target_type",
        "target_id",
        "actor",
        "workspace",
    ]
    list_filter = [
        "event_type",
        "target_type",
        "workspace",
    ]
    readonly_fields = [
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
    search_fields = [
        "event_type",
    ]
    ordering = [
        "-created_at",
    ]

    def has_add_permission(self, request) -> bool:
        """Disallow manual creation of activity rows.

        Activity entries are written exclusively through
        :func:`apps.activity.services.log_event` so the actor and payload
        invariants stay enforceable.

        Args:
            request: The current HTTP request.

        Returns:
            Always ``False``.
        """
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        """Disallow editing of activity rows.

        The activity log is append-only — historical events are immutable.

        Args:
            request: The current HTTP request.
            obj: The :class:`ActivityLog` instance being inspected, or
                ``None`` for the changelist view.

        Returns:
            Always ``False``.
        """
        return False
