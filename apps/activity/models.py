from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ActivityLog(models.Model):
    """Append-only event log for the workspace.

    The single source of truth for "who did what, when". Backed by a unified
    JSONB ``payload`` per event so new event types do not require schema
    changes. See docs/decisions/0011-activity-log.md for the design
    rationale and the anti-Kaneo rules baked into this model.
    """

    TARGET_TASK = "task"
    TARGET_COMMENT = "comment"
    TARGET_PROJECT = "project"
    TARGET_WORKSPACE = "workspace"
    TARGET_MEMBER = "member"
    TARGET_ATTACHMENT = "attachment"

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="activity",
        help_text="Workspace this event occurred in. Denormalized for fast feed filtering",
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity",
        help_text="Project this event relates to, if applicable",
    )

    target_type = models.CharField(
        max_length=20,
        help_text="Kind of object the event is about: task, comment, project, workspace, member, or attachment",
    )
    target_id = models.PositiveBigIntegerField(
        help_text="ID of the target object. Not a foreign key — the row survives target deletion",
    )

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity",
        help_text=(
            "User who performed the action. Always set from request.user — never inferred. "
            "Null for system-initiated events"
        ),
    )
    event_type = models.CharField(
        max_length=40,
        help_text="Event category, e.g. task.status_changed, comment.created, member.role_changed",
    )
    payload = models.JSONField(
        default=dict,
        blank=True,
        help_text="Event-specific details as JSON: diffs, denormalized snapshots, metadata",
    )
    bulk_id = models.UUIDField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Shared by all events emitted from a single bulk operation. Null for one-off events",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
        help_text="When the event was recorded. Always sort timelines by this column, never by id",
    )

    class Meta:
        verbose_name = _("Activity log entry")
        verbose_name_plural = _("Activity log")
        indexes = [
            models.Index(
                fields=[
                    "workspace",
                    "-created_at",
                ],
            ),
            models.Index(
                fields=[
                    "target_type",
                    "target_id",
                    "-created_at",
                ],
            ),
        ]
        ordering = [
            "-created_at",
        ]

    def __str__(self) -> str:
        """Return ``<event_type> · <target_type>:<target_id> · <date>``."""
        return f"{self.event_type} · {self.target_type}:{self.target_id} · {self.created_at:%Y-%m-%d %H:%M}"
