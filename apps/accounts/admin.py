from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from unfold.admin import ModelAdmin as UnfoldModelAdmin
from unfold.forms import AdminPasswordChangeForm, UserChangeForm, UserCreationForm

from .models import User


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
