from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html

from unfold.admin import ModelAdmin, TabularInline

from .models import Workspace, WorkspaceInvite, WorkspaceMember


class WorkspaceMemberInline(TabularInline):
    model = WorkspaceMember
    extra = 0
    autocomplete_fields = [
        "user",
    ]


@admin.register(Workspace)
class WorkspaceAdmin(ModelAdmin):
    list_display = [
        "name",
        "slug",
        "owner",
        "auto_archive_done_after_days",
        "created_at",
    ]
    list_filter = [
        "auto_archive_done_after_days",
    ]
    search_fields = [
        "name",
        "slug",
    ]
    autocomplete_fields = [
        "owner",
    ]
    inlines = [
        WorkspaceMemberInline,
    ]


@admin.register(WorkspaceMember)
class WorkspaceMemberAdmin(ModelAdmin):
    list_display = [
        "user",
        "workspace",
        "role",
        "joined_at",
    ]
    list_filter = [
        "role",
        "workspace",
    ]
    autocomplete_fields = [
        "user",
        "workspace",
    ]


@admin.register(WorkspaceInvite)
class WorkspaceInviteAdmin(ModelAdmin):
    """Admin for workspace invites.

    Until the workspace-settings page grows an invite UI (Phase 2),
    this is the only place to mint one. The admin sees the
    ``signup_url`` ready to paste into an email — the token itself is
    set by ``WorkspaceInvite.generate`` on save so admins never type a
    secret by hand.
    """

    list_display = [
        "email",
        "workspace",
        "role",
        "status",
        "created_at",
        "expires_at",
        "invite_link",
    ]
    list_filter = [
        "role",
        "workspace",
    ]
    search_fields = [
        "email",
    ]
    autocomplete_fields = [
        "workspace",
        "created_by",
    ]
    readonly_fields = [
        "token",
        "created_at",
        "accepted_at",
        "invite_link",
    ]
    fields = [
        "workspace",
        "email",
        "role",
        "created_by",
        "expires_at",
        "token",
        "created_at",
        "accepted_at",
        "invite_link",
    ]

    @admin.display(description="Status")
    def status(self, obj):
        """Human label of the invite's lifecycle state."""
        if obj.is_consumed:
            return "accepted"
        if obj.is_expired:
            return "expired"
        return "pending"

    @admin.display(description="Invite link")
    def invite_link(self, obj):
        """Render the ``/accounts/invite/<token>/`` URL the admin pastes into mail.

        Built relative — the admin can prefix the server's public host
        when copying. Showing the URL clickable in the list view lets
        the admin copy + send without opening the detail page.
        """
        if not obj.token:
            return "(saving…)"
        url = reverse("accounts:invite_accept", args=[obj.token])
        return format_html('<a href="{0}">{0}</a>', url)

    def save_model(self, request, obj, form, change):
        """Mint a token + record the inviting admin on first save.

        ``WorkspaceInvite.generate`` does this for the public API; we
        replay it inside the admin so the admin form doesn't need to
        expose ``token`` as an editable field. Re-saving an existing
        row never rotates the token — that's a separate "resend"
        action surfaced once the workspace-settings UI lands.
        """
        if not change:
            if not obj.token:
                from secrets import token_urlsafe

                obj.token = token_urlsafe(32)
            if obj.created_by_id is None:
                obj.created_by = request.user
            if obj.email:
                obj.email = obj.email.strip().lower()
        super().save_model(request, obj, form, change)
        if not change:
            messages.success(
                request,
                f"Invite link: {reverse('accounts:invite_accept', args=[obj.token])}",
            )
