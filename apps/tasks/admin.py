from django.contrib import admin

from unfold.admin import ModelAdmin

from .models import Task


@admin.register(Task)
class TaskAdmin(ModelAdmin):
    list_display = [
        "slug",
        "title",
        "project",
        "status",
        "priority",
        "assignee",
        "end_date",
        "due_date",
        "updated_at",
    ]
    list_filter = [
        "status",
        "priority",
        "project",
    ]
    search_fields = [
        "title",
        "description",
    ]
    autocomplete_fields = [
        "project",
        "parent",
        "assignee",
        "reporter",
        "labels",
    ]
    readonly_fields = [
        "number",
        "created_at",
        "updated_at",
    ]
    ordering = [
        "-updated_at",
    ]
