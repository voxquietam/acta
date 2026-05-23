from django import forms
from django.contrib import admin

from apps.notifications.models import Notification

from .models import TelegramAccount, TelegramMessageTemplate


class TelegramMessageTemplateForm(forms.ModelForm):
    """Admin form rendering ``kind`` as a dropdown of notification kinds."""

    class Meta:
        model = TelegramMessageTemplate
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"] = forms.ChoiceField(
            choices=Notification.Kind.choices,
            help_text="Notification kind this template applies to (one row per kind)",
        )


@admin.register(TelegramMessageTemplate)
class TelegramMessageTemplateAdmin(admin.ModelAdmin):
    form = TelegramMessageTemplateForm
    list_display = [
        "kind",
        "updated_at",
    ]
    readonly_fields = [
        "updated_at",
    ]


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
