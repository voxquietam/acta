from django.contrib import admin

from unfold.admin import ModelAdmin, TabularInline

from .models import Workspace, WorkspaceMember


class WorkspaceMemberInline(TabularInline):
    model = WorkspaceMember
    extra = 0
    autocomplete_fields = [
        "user",
    ]


@admin.register(Workspace)
class WorkspaceAdmin(ModelAdmin):
    list_display = [
        "name",
        "slug",
        "owner",
        "created_at",
    ]
    search_fields = [
        "name",
        "slug",
    ]
    autocomplete_fields = [
        "owner",
    ]
    inlines = [
        WorkspaceMemberInline,
    ]


@admin.register(WorkspaceMember)
class WorkspaceMemberAdmin(ModelAdmin):
    list_display = [
        "user",
        "workspace",
        "role",
        "joined_at",
    ]
    list_filter = [
        "role",
        "workspace",
    ]
    autocomplete_fields = [
        "user",
        "workspace",
    ]
