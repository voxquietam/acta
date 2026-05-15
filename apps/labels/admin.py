from django.contrib import admin

from unfold.admin import ModelAdmin

from .models import Label, LabelGroup


@admin.register(LabelGroup)
class LabelGroupAdmin(ModelAdmin):
    list_display = [
        "name",
        "workspace",
        "is_exclusive",
        "created_at",
    ]
    list_filter = [
        "workspace",
        "is_exclusive",
    ]
    autocomplete_fields = [
        "workspace",
    ]
    search_fields = [
        "name",
    ]


@admin.register(Label)
class LabelAdmin(ModelAdmin):
    list_display = [
        "name",
        "workspace",
        "group",
        "color",
        "created_at",
    ]
    list_filter = [
        "workspace",
        "group",
    ]
    autocomplete_fields = [
        "workspace",
        "group",
    ]
    search_fields = [
        "name",
    ]
