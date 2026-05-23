from django.contrib import admin

from .models import TelegramAccount


@admin.register(TelegramAccount)
class TelegramAccountAdmin(admin.ModelAdmin):
    list_display = [
        "user",
        "username",
        "chat_id",
        "enabled",
        "linked_at",
    ]
    list_filter = [
        "enabled",
    ]
    search_fields = [
        "user__username",
        "username",
        "chat_id",
    ]
    autocomplete_fields = [
        "user",
    ]
    readonly_fields = [
        "linked_at",
    ]
