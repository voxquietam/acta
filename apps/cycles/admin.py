from django.contrib import admin

from .models import Cycle


@admin.register(Cycle)
class CycleAdmin(admin.ModelAdmin):
    list_display = [
        "number",
        "display_name",
        "workspace",
        "status",
        "start_date",
        "end_date",
    ]
    list_filter = [
        "status",
        "workspace",
    ]
    search_fields = [
        "name",
        "workspace__name",
        "workspace__slug",
    ]
    autocomplete_fields = [
        "workspace",
    ]
    readonly_fields = [
        "created_at",
        "completed_at",
    ]
    ordering = [
        "-start_date",
    ]
