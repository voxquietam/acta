from django.contrib import admin

from .models import Workspace, WorkspaceMember


class WorkspaceMemberInline(admin.TabularInline):
    model = WorkspaceMember
    extra = 0
    autocomplete_fields = [
        "user",
    ]


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
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
class WorkspaceMemberAdmin(admin.ModelAdmin):
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
