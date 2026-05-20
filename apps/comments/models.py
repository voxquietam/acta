from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Comment(models.Model):
    """A Markdown comment attached to a task or a project update.

    Targets exactly one of ``task`` / ``project_update`` (enforced by a
    DB check constraint). Task comments contribute to the activity log
    via ``comment.created`` / ``comment.edited`` / ``comment.deleted``
    events (see docs/decisions/0011-activity-log.md); project-update
    comments do not — updates are intentionally off the activity log.
    One level of threading is supported via ``parent``.
    """

    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="comments",
        help_text="Task this comment is attached to. Null when it targets a project update",
    )
    project_update = models.ForeignKey(
        "projects.ProjectUpdate",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="comments",
        help_text="Project update this comment is attached to. Null when it targets a task",
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="replies",
        help_text="Parent comment when this is a one-level reply; null for top-level comments",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="comments",
        help_text="User who wrote the comment",
    )
    body = models.TextField(
        help_text="Comment body in Markdown",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the comment was posted",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="When the comment was last edited",
    )

    class Meta:
        verbose_name = _("Comment")
        verbose_name_plural = _("Comments")
        ordering = [
            "created_at",
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(task__isnull=False, project_update__isnull=True)
                    | models.Q(task__isnull=True, project_update__isnull=False)
                ),
                name="comment_exactly_one_target",
            ),
        ]

    def __str__(self) -> str:
        """Return author, target, and a preview of the comment body."""
        preview = self.body[:60].replace("\n", " ")
        return f"{self.author} on {self.task or self.project_update}: {preview}"

    def clean(self) -> None:
        """Validate the comment target and one-level reply threading.

        Raises:
            ValidationError: If neither or both targets are set, or a
                reply points at another reply / a comment on a different
                target.
        """
        from django.core.exceptions import ValidationError

        if bool(self.task_id) == bool(self.project_update_id):
            raise ValidationError(_("A comment must target exactly one of a task or a project update."))
        if self.parent_id is not None:
            if self.parent.parent_id is not None:
                raise ValidationError(_("Replies cannot have their own replies (depth limit 1)."))
            if self.parent.task_id != self.task_id or self.parent.project_update_id != self.project_update_id:
                raise ValidationError(_("A reply must belong to the same target as its parent."))

    @property
    def was_edited(self) -> bool:
        """Return True if the comment was edited after it was posted.

        ``auto_now_add`` and ``auto_now`` issue separate ``timezone.now()``
        calls during the initial INSERT, so the two timestamps can differ
        by a few microseconds even when the comment was never edited.
        Comparing the raw timestamps would mark every fresh comment as
        ``(edited)`` in the UI. We use a one-second tolerance to make
        ``was_edited`` reflect actual user edits.

        Returns:
            True iff ``updated_at`` is more than one second after
            ``created_at``.
        """
        if self.created_at is None or self.updated_at is None:
            return False
        return (self.updated_at - self.created_at).total_seconds() > 1
