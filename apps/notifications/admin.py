from django.contrib import admin

from unfold.admin import ModelAdmin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(ModelAdmin):
    list_display = [
        "created_at",
        "kind",
        "recipient",
        "actor",
        "is_read",
        "workspace",
    ]
    list_filter = [
        "kind",
        "is_read",
        "workspace",
    ]
    readonly_fields = [
        "recipient",
        "actor",
        "workspace",
        "kind",
        "task",
        "comment",
        "activity",
        "preview",
        "payload",
        "created_at",
    ]
    search_fields = [
        "preview",
    ]
    ordering = [
        "-created_at",
    ]

    def has_add_permission(self, request) -> bool:
        """Disallow manual creation of notifications.

        Notifications are written exclusively through
        :func:`apps.notifications.services.notify` so recipient and
        suppression invariants stay enforceable.

        Args:
            request: The current HTTP request.

        Returns:
            Always ``False``.
        """
        return False
