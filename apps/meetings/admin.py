from django.contrib import admin

from .models import Meeting


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "happened_at",
        "human_duration",
        "project",
        "created_by",
    ]
    list_filter = [
        "workspace",
        "happened_at",
    ]
    search_fields = [
        "title",
        "notes",
        "project__name",
        "project__slug_prefix",
    ]
    autocomplete_fields = [
        "workspace",
        "project",
        "created_by",
    ]
    filter_horizontal = [
        "participants",
        "tasks",
    ]
    readonly_fields = [
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "happened_at"
    fieldsets = [
        (
            "Meeting",
            {
                "fields": [
                    "workspace",
                    "project",
                    "title",
                    "happened_at",
                    "duration_minutes",
                    "participants",
                    "notes",
                    "created_by",
                ],
            },
        ),
        (
            "Links",
            {
                "fields": [
                    "tasks",
                ],
            },
        ),
        (
            "Timestamps",
            {
                "fields": [
                    "created_at",
                    "updated_at",
                ],
            },
        ),
    ]
