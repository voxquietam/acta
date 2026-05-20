from django.contrib import admin

from unfold.admin import ModelAdmin

from .models import Reaction


@admin.register(Reaction)
class ReactionAdmin(ModelAdmin):
    list_display = [
        "id",
        "emoji",
        "user",
        "task",
        "comment",
        "project_update",
        "created_at",
    ]
    list_filter = [
        "emoji",
    ]
    search_fields = [
        "emoji",
    ]
    autocomplete_fields = [
        "user",
        "task",
        "comment",
        "project_update",
    ]
