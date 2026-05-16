from django import forms
from django.contrib import admin

from unfold.admin import ModelAdmin

from .models import Label, LabelGroup


class LabelAdminForm(forms.ModelForm):
    """Admin form that surfaces the color field as an HTML5 color picker.

    Plain ``CharField`` rendered as a ``<input type="text">`` lets users
    type literal placeholders like ``#RRGGBB`` or shortened ``#aaa``;
    the native color picker forces a valid six-digit hex via the
    browser's own UI, so no malformed values reach the database.
    """

    class Meta:
        model = Label
        fields = "__all__"
        widgets = {
            "color": forms.TextInput(attrs={"type": "color"}),
        }


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
    form = LabelAdminForm
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
