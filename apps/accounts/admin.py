from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from .models import ApiToken, User


@admin.register(User)
class UserAdmin(BaseUserAdmin, UnfoldModelAdmin):
    """User admin styled by django-unfold.

    Inherits Django's standard :class:`auth.UserAdmin` for permissions and
    password handling, mixed with :class:`unfold.admin.ModelAdmin` to pick
    up the unfold layout, widgets, and templates.
    """

    form = UserChangeForm
    add_form = UserCreationForm
    change_password_form = AdminPasswordChangeForm


@admin.register(ApiToken)
class ApiTokenAdmin(UnfoldModelAdmin):
    """Read-mostly admin for API tokens.

    The plain secret is never stored — only the hash and an 8-char
    prefix — so admins can identify tokens (``Claude Desktop (a1b2c3d4…)``)
    and revoke them, but cannot retrieve the secret. To rotate, the
    user revokes and creates a new one.
    """

    list_display = [
        "name",
        "user",
        "prefix",
        "created_at",
        "last_used_at",
        "revoked_at",
    ]
    list_filter = [
        "revoked_at",
    ]
    search_fields = [
        "name",
        "user__username",
        "prefix",
    ]
    readonly_fields = [
        "token_hash",
        "prefix",
        "created_at",
        "last_used_at",
    ]
    autocomplete_fields = [
        "user",
    ]
