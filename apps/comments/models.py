from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Comment(models.Model):
    """A Markdown comment attached to a task.

    Comments contribute to the activity log via ``comment.created`` /
    ``comment.edited`` / ``comment.deleted`` events (see
    docs/decisions/0011-activity-log.md).
    """

    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        related_name="comments",
        help_text="Task this comment is attached to",
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

    def __str__(self) -> str:
        """Return author, task, and a preview of the comment body."""
        preview = self.body[:60].replace("\n", " ")
        return f"{self.author} on {self.task}: {preview}"

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
