from django.contrib import admin

from unfold.admin import ModelAdmin

from .models import Comment


@admin.register(Comment)
class CommentAdmin(ModelAdmin):
    list_display = [
        "id",
        "task",
        "project_update",
        "parent",
        "author",
        "created_at",
    ]
    list_filter = [
        "task__project",
    ]
    search_fields = [
        "body",
    ]
    autocomplete_fields = [
        "task",
        "author",
        "parent",
    ]
