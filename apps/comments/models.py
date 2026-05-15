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
