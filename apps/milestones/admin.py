from django.contrib import admin

from .models import Milestone


@admin.register(Milestone)
class MilestoneAdmin(admin.ModelAdmin):
    """Admin for milestones; the scope is the field worth seeing first."""

    list_display = [
        "name",
        "workspace",
        "target_date",
        "owner",
        "closed_at",
    ]
    list_filter = [
        "workspace",
        "target_date",
    ]
    search_fields = [
        "name",
        "goal",
    ]
    autocomplete_fields = [
        "workspace",
        "owner",
    ]
    filter_horizontal = [
        "projects",
    ]
    date_hierarchy = "target_date"
