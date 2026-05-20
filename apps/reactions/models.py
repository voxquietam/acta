from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Reaction(models.Model):
    """A single emoji reaction by one user on one target.

    Generic across surfaces the same way :class:`apps.comments.models.Comment`
    is: exactly one of ``task`` / ``comment`` / ``project_update`` is set
    (enforced by a DB check constraint), so the model spans tasks, comments
    (on tasks *or* updates), and project updates without content types — see
    docs/decisions/0022-polymorphic-comments.md for the precedent.

    A user may react to one target with many distinct emoji but only once
    per emoji; that ``(user, target, emoji)`` uniqueness is enforced per
    target via partial unique constraints, since a plain multi-column
    unique index would treat the two ``NULL`` target columns as distinct.
    """

    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reactions",
        help_text="Task this reaction is on. Null when it targets a comment or a project update",
    )
    comment = models.ForeignKey(
        "comments.Comment",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reactions",
        help_text="Comment this reaction is on. Null when it targets a task or a project update",
    )
    project_update = models.ForeignKey(
        "projects.ProjectUpdate",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reactions",
        help_text="Project update this reaction is on. Null when it targets a task or a comment",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reactions",
        help_text="User who reacted",
    )
    emoji = models.CharField(
        max_length=64,
        help_text="The reacted emoji as a Unicode grapheme (may be a multi-codepoint ZWJ sequence)",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the reaction was added",
    )

    class Meta:
        verbose_name = _("Reaction")
        verbose_name_plural = _("Reactions")
        ordering = [
            "created_at",
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(task__isnull=False, comment__isnull=True, project_update__isnull=True)
                    | models.Q(task__isnull=True, comment__isnull=False, project_update__isnull=True)
                    | models.Q(task__isnull=True, comment__isnull=True, project_update__isnull=False)
                ),
                name="reaction_exactly_one_target",
            ),
            models.UniqueConstraint(
                fields=[
                    "user",
                    "task",
                    "emoji",
                ],
                condition=models.Q(task__isnull=False),
                name="reaction_unique_user_task_emoji",
            ),
            models.UniqueConstraint(
                fields=[
                    "user",
                    "comment",
                    "emoji",
                ],
                condition=models.Q(comment__isnull=False),
                name="reaction_unique_user_comment_emoji",
            ),
            models.UniqueConstraint(
                fields=[
                    "user",
                    "project_update",
                    "emoji",
                ],
                condition=models.Q(project_update__isnull=False),
                name="reaction_unique_user_update_emoji",
            ),
        ]

    def __str__(self) -> str:
        """Return user, emoji, and the target it reacts to."""
        target = self.task or self.comment or self.project_update
        return f"{self.user} {self.emoji} on {target}"

    def clean(self) -> None:
        """Validate that exactly one target FK is set.

        Raises:
            ValidationError: If zero or more than one of ``task`` /
                ``comment`` / ``project_update`` is set.
        """
        from django.core.exceptions import ValidationError

        set_targets = sum(1 for value in (self.task_id, self.comment_id, self.project_update_id) if value is not None)
        if set_targets != 1:
            raise ValidationError(_("A reaction must target exactly one of a task, a comment, or a project update."))
