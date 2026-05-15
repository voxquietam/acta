from django.conf import settings
from django.db import models


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
        (OWNER, "Owner"),
        (ADMIN, "Admin"),
        (MEMBER, "Member"),
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
