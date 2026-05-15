from django.contrib import admin

from unfold.admin import ModelAdmin

from .models import Project, ProjectUpdate


@admin.register(Project)
class ProjectAdmin(ModelAdmin):
    list_display = [
        "slug_prefix",
        "name",
        "workspace",
        "next_task_number",
        "archived",
        "created_at",
    ]
    list_filter = [
        "workspace",
        "archived",
    ]
    search_fields = [
        "name",
        "slug_prefix",
    ]
    autocomplete_fields = [
        "workspace",
    ]
    readonly_fields = [
        "next_task_number",
    ]


@admin.register(ProjectUpdate)
class ProjectUpdateAdmin(ModelAdmin):
    list_display = [
        "project",
        "health",
        "author",
        "created_at",
    ]
    list_filter = [
        "health",
        "project",
    ]
    autocomplete_fields = [
        "project",
        "author",
    ]
