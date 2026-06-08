from django.contrib import admin

from .models import RecurringTask


@admin.register(RecurringTask)
class RecurringTaskAdmin(admin.ModelAdmin):
    list_display = [
        "title",
        "human_cadence",
        "project",
        "assignee",
        "next_occurrence_date",
        "occurrences_created",
        "is_active",
    ]
    list_filter = [
        "is_active",
        "freq",
        "workspace",
    ]
    search_fields = [
        "title",
        "project__name",
        "project__slug_prefix",
    ]
    autocomplete_fields = [
        "workspace",
        "project",
        "assignee",
        "created_by",
    ]
    filter_horizontal = [
        "labels",
    ]
    readonly_fields = [
        "occurrences_created",
        "last_spawned_at",
        "created_at",
        "updated_at",
    ]
    fieldsets = [
        (
            "Blueprint",
            {
                "fields": [
                    "workspace",
                    "project",
                    "title",
                    "description",
                    "assignee",
                    "priority",
                    "size",
                    "labels",
                    "created_by",
                ],
            },
        ),
        (
            "Schedule",
            {
                "fields": [
                    "freq",
                    "interval",
                    "weekdays",
                    "day_of_month",
                    "start_date",
                    "lead_time_days",
                    "end_mode",
                    "end_date",
                    "max_occurrences",
                ],
            },
        ),
        (
            "State",
            {
                "fields": [
                    "is_active",
                    "next_occurrence_date",
                    "occurrences_created",
                    "last_spawned_at",
                    "created_at",
                    "updated_at",
                ],
            },
        ),
    ]
