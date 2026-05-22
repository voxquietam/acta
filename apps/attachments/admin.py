from django.contrib import admin

from unfold.admin import ModelAdmin

from .models import Attachment


@admin.register(Attachment)
class AttachmentAdmin(ModelAdmin):
    list_display = [
        "id",
        "original_name",
        "kind",
        "content_type",
        "size",
        "uploader",
        "task",
        "comment",
        "project",
        "created_at",
    ]
    list_filter = [
        "kind",
        "content_type",
    ]
    search_fields = [
        "original_name",
    ]
    autocomplete_fields = [
        "workspace",
        "task",
        "comment",
        "project",
        "uploader",
    ]
    readonly_fields = [
        "size",
        "content_type",
        "created_at",
    ]
