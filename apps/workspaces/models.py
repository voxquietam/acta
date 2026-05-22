import datetime
import secrets

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Workspace(models.Model):
    """Top-level tenant. Holds projects, members, and labels.

    See docs/decisions/0003-hierarchy.md and 0010-permissions.md.
    """

    name = models.CharField(
        max_length=120,
        help_text="Display name of the workspace",
    )
    slug = models.SlugField(
        max_length=60,
        unique=True,
        help_text="URL-safe identifier; lowercase letters, digits, hyphens",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_workspaces",
        help_text="User who owns this workspace. Exactly one owner per workspace; transfer to change",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the workspace was created",
    )

    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="WorkspaceMember",
        related_name="workspaces",
        help_text="Users with access to the workspace, with their role",
    )

    auto_archive_done_after_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=30,
        help_text=(
            "Auto-archive policy: done tasks whose updated_at is older than this many days "
            "are archived by the daily archive_stale_done_tasks command. Set to NULL to disable "
            "auto-archive for this workspace. Manual archive/unarchive still works regardless"
        ),
    )

    WIP_OFF = "off"
    WIP_PERSONAL = "personal"
    WIP_COLUMN = "column"
    WIP_MODE_CHOICES = [
        (WIP_OFF, _("Off")),
        (WIP_PERSONAL, _("Per person")),
        (WIP_COLUMN, _("Per column (team)")),
    ]
    wip_limits = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            'Workspace-wide WIP-limit policy as {"mode": off|personal|column, '
            '"limits": {status_key: max}}. personal = each member may hold at most '
            "max tasks in that status across the whole workspace; column = the kanban "
            "column holds at most max cards for the team. Empty / off disables it"
        ),
    )

    cycle_settings = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            'Cadence config for workspace cycles as {"enabled": bool, "length_weeks": int, '
            '"start_date": "YYYY-MM-DD", "auto_rollover": bool}. start_date is the anchor of '
            "cycle 1; subsequent cycles roll automatically every length_weeks. auto_rollover "
            "moves unfinished tasks into the next cycle when one completes. Empty / disabled hides cycles"
        ),
    )

    CYCLE_DEFAULT_LENGTH_WEEKS = 2
    CYCLE_MAX_LENGTH_WEEKS = 8

    def cycle_config(self):
        """Return the normalised cadence config from :attr:`cycle_settings`.

        Returns:
            A dict ``{"enabled": bool, "length_weeks": int, "start_date":
            str | None}``. ``length_weeks`` is clamped to ``1..
            CYCLE_MAX_LENGTH_WEEKS`` and ``start_date`` is the ISO anchor
            string (``None`` when unset). A disabled config still reports
            its stored length / anchor so the settings form round-trips.
        """
        raw = self.cycle_settings or {}
        try:
            length = int(raw.get("length_weeks") or self.CYCLE_DEFAULT_LENGTH_WEEKS)
        except (TypeError, ValueError):
            length = self.CYCLE_DEFAULT_LENGTH_WEEKS
        length = max(1, min(length, self.CYCLE_MAX_LENGTH_WEEKS))
        start = raw.get("start_date") or None
        return {
            "enabled": bool(raw.get("enabled")) and start is not None,
            "length_weeks": length,
            "start_date": start,
            "auto_rollover": bool(raw.get("auto_rollover")),
        }

    def wip_config(self):
        """Return ``(mode, limits)`` from :attr:`wip_limits`, normalised.

        Returns:
            A ``(mode, limits)`` tuple where ``mode`` is one of the
            ``WIP_*`` constants (``off`` when unset / unknown) and
            ``limits`` is a ``{status_key: int}`` dict (only positive
            limits kept). ``off`` mode always yields an empty ``limits``.
        """
        raw = self.wip_limits or {}
        mode = raw.get("mode", self.WIP_OFF)
        if mode not in {self.WIP_PERSONAL, self.WIP_COLUMN}:
            return self.WIP_OFF, {}
        limits = {}
        for key, value in (raw.get("limits") or {}).items():
            try:
                n = int(value)
            except (TypeError, ValueError):
                continue
            if n > 0:
                limits[key] = n
        return mode, limits

    class Meta:
        verbose_name = _("Workspace")
        verbose_name_plural = _("Workspaces")

    def __str__(self) -> str:
        """Return the workspace name."""
        return self.name


class WorkspaceMember(models.Model):
    """Association between a user and a workspace, carrying the role.

    Roles: owner, admin, member. See docs/decisions/0010-permissions.md
    for the permission matrix.
    """

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    ROLE_CHOICES = [
        (OWNER, _("Owner")),
        (ADMIN, _("Admin")),
        (MEMBER, _("Member")),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
        help_text="User being granted access to the workspace",
    )
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="memberships",
        help_text="Workspace this membership belongs to",
    )
    role = models.CharField(
        max_length=10,
        choices=ROLE_CHOICES,
        default=MEMBER,
        help_text="Role within the workspace; controls what the user can do",
    )
    joined_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the user was added to the workspace",
    )

    class Meta:
        verbose_name = _("Workspace member")
        verbose_name_plural = _("Workspace members")
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "user",
                    "workspace",
                ],
                name="workspaces_member_unique_user_workspace",
            ),
        ]

    def __str__(self) -> str:
        """Return a human-readable summary of the membership."""
        return f"{self.user} in {self.workspace} ({self.role})"


INVITE_DEFAULT_TTL_DAYS = 7


def _default_invite_expiry():
    """Return the default ``expires_at`` for new invites.

    Defined as a module-level function (not a lambda) so Django's
    migration autodetector can serialise it without freezing the
    current ``timezone.now()`` into the migration file.
    """
    return timezone.now() + datetime.timedelta(days=INVITE_DEFAULT_TTL_DAYS)


class WorkspaceInvite(models.Model):
    """One-use email invitation to join a workspace.

    Signup is otherwise closed (``NoSignupAccountAdapter``) — an
    invite is the *only* path for a new account to land in a
    workspace. The token is a capability-style credential: anyone
    holding the URL ``/accounts/signup/?invite=<token>`` can complete
    signup as the invited email, get a membership row with the
    pre-baked role, and start using Acta.

    Tokens are single-use (``accepted_at`` flips at signup) and time-
    boxed (default 7-day TTL). Admins can revoke a pending invite at
    any time by deleting the row, or re-send by minting a new one.
    """

    email = models.EmailField(
        help_text="Email address of the invitee; pre-filled and locked at signup",
    )
    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name="invites",
        help_text="Workspace the invitee joins on accepting",
    )
    role = models.CharField(
        max_length=10,
        choices=[
            (WorkspaceMember.ADMIN, _("Admin")),
            (WorkspaceMember.MEMBER, _("Member")),
        ],
        default=WorkspaceMember.MEMBER,
        help_text="Workspace role granted to the new member; never grants Owner",
    )
    token = models.CharField(
        max_length=64,
        unique=True,
        help_text="URL-safe one-use token included in the invite link",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_workspace_invites",
        help_text="Admin who sent the invite; SET_NULL preserves the audit trail if they leave",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the invite was generated",
    )
    expires_at = models.DateTimeField(
        default=_default_invite_expiry,
        help_text="When the token stops accepting signups; defaults to created_at + 7 days",
    )
    accepted_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the invite was consumed; non-null means the token can no longer be used",
    )

    class Meta:
        verbose_name = _("Workspace invite")
        verbose_name_plural = _("Workspace invites")
        ordering = [
            "-created_at",
        ]

    def __str__(self) -> str:
        """Return a compact summary for the admin list view."""
        return f"{self.email} → {self.workspace} ({self.role})"

    @classmethod
    def generate(cls, *, workspace, email: str, role: str, created_by=None) -> "WorkspaceInvite":
        """Mint a fresh invite with a random token and the default TTL.

        The admin-facing UI calls this; ``email`` is normalised lower-
        case so case differences don't let the same address eat two
        tokens. ``role`` must be ``admin`` or ``member`` — Owner is
        reserved for the workspace creator.

        Args:
            workspace: The :class:`Workspace` the invitee will join.
            email: Invitee's email address.
            role: One of ``admin`` / ``member``.
            created_by: Admin issuing the invite (optional but
                strongly recommended for audit).

        Returns:
            The persisted :class:`WorkspaceInvite` row, ready to be
            embedded into an outgoing email.
        """
        return cls.objects.create(
            workspace=workspace,
            email=email.strip().lower(),
            role=role,
            created_by=created_by,
            token=secrets.token_urlsafe(32),
        )

    @property
    def is_expired(self) -> bool:
        """``True`` if the invite's TTL has lapsed."""
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self) -> bool:
        """``True`` once the invite has been used to complete a signup."""
        return self.accepted_at is not None

    @property
    def is_active(self) -> bool:
        """``True`` while the invite still accepts a signup."""
        return not self.is_expired and not self.is_consumed

    @property
    def signup_url(self) -> str:
        """Relative URL pointing at the invite landing view.

        The admin pastes this into the outgoing email. The landing
        view validates the token, stashes it in the session, then
        forwards to the allauth signup form — that two-hop dance is
        what survives allauth's POST-without-querystring quirk. Use
        ``request.build_absolute_uri(invite.signup_url)`` to render
        the full ``https://acta.../...`` form for the email body.
        """
        return reverse("accounts:invite_accept", args=[self.token])
