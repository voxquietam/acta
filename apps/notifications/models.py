from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Notification(models.Model):
    """A per-user inbox entry derived from a workspace event.

    Unlike :class:`apps.activity.models.ActivityLog` — a single global,
    append-only event stream — a ``Notification`` is the *personal*
    fan-out of an event to one recipient who cares about it. These rows
    populate the user's Inbox. See
    docs/decisions/0021-notification-inbox.md for the rationale and the
    way this supersedes the "no persistence" decision of ADR 0017.

    Fields are denormalized (``preview``, ``payload``) so the inbox list
    renders without re-walking the target object graph, and the row
    survives deletion of the task / comment it points at.
    """

    class Kind(models.TextChoices):
        MENTION = "mention", _("Mention")
        ASSIGNED = "assigned", _("Assigned")
        DUE = "due", _("Due soon")
        COMMENT = "comment", _("Comment")
        STATUS_CHANGE = "status_change", _("Status change")
        PRIORITY_CHANGE = "priority_change", _("Priority change")
        PROJECT_UPDATE = "project_update", _("Project update")
        CYCLE = "cycle", _("Cycle")
        ANNOUNCEMENT = "announcement", _("Announcement")
        SYSTEM = "system", _("System")

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
        help_text="User this notification is delivered to",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications_sent",
        help_text="User whose action triggered the notification. Null for system events",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="notifications",
        help_text="Workspace the notification belongs to. Denormalized for fast per-user filtering",
    )
    kind = models.CharField(
        max_length=20,
        choices=Kind.choices,
        help_text="Notification category, drives the type-icon and the inbox filter chips",
    )
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        help_text="Task the notification is about, if any. Null-safe so the row survives task deletion",
    )
    comment = models.ForeignKey(
        "comments.Comment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        help_text="Comment that triggered the notification, for comment and mention previews",
    )
    activity = models.ForeignKey(
        "activity.ActivityLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        help_text="Activity-log row this notification was fanned out from, if any",
    )
    project_update = models.ForeignKey(
        "projects.ProjectUpdate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        help_text="Project update this notification is about, for project_update kind",
    )
    preview = models.TextField(
        blank=True,
        default="",
        help_text="Denormalized snippet shown in the inbox list (comment body or task title)",
    )
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Event-specific details as JSON: status diff, priority tint, and so on",
    )
    is_read = models.BooleanField(
        default=False,
        help_text="Whether the recipient has read the notification",
    )
    read_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the recipient marked the notification read",
    )
    archived_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the recipient archived the notification out of the inbox",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="When the notification was created. Always sort the inbox by this column",
    )

    class Meta:
        verbose_name = _("Notification")
        verbose_name_plural = _("Notifications")
        indexes = [
            models.Index(
                fields=[
                    "recipient",
                    "archived_at",
                    "is_read",
                    "-created_at",
                ],
            ),
            models.Index(
                fields=[
                    "recipient",
                    "-created_at",
                ],
            ),
        ]
        ordering = [
            "-created_at",
        ]

    def __str__(self) -> str:
        """Return ``<kind> → <recipient> · <date>``."""
        return f"{self.kind} → {self.recipient} · {self.created_at:%Y-%m-%d %H:%M}"

    def mark_read(self) -> None:
        """Mark the notification read and stamp ``read_at``, idempotently.

        Persists only the two affected columns. A no-op if already read.
        """
        if self.is_read:
            return
        self.is_read = True
        self.read_at = timezone.now()
        self.save(
            update_fields=[
                "is_read",
                "read_at",
            ],
        )

    def mark_unread(self) -> None:
        """Mark the notification unread and clear ``read_at``, idempotently.

        Persists only the two affected columns. A no-op if already unread.
        """
        if not self.is_read:
            return
        self.is_read = False
        self.read_at = None
        self.save(
            update_fields=[
                "is_read",
                "read_at",
            ],
        )
