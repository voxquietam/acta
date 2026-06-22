from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _


class Meeting(models.Model):
    """A logged call or meeting — a first-class record, not a task.

    Captures that time was spent talking: when it happened, how long it
    ran, who took part, free-form notes, and which tasks it touched. The
    duration lives on the meeting as a whole; a linked task surfaces the
    meeting in its detail view and rolls up the minutes for context (a
    single meeting linked to N tasks shows its full duration on each — the
    rollup is a "time spent around this task" signal, not per-task billing,
    so aggregate totals are computed over ``Meeting`` rows, never by summing
    across tasks). See docs/decisions/0030-meetings.md.

    Manual only for now; a deferred Google Calendar / Zoom import would add
    ``source`` / ``external_id`` fields and feed the same model.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="meetings",
        help_text="Workspace the meeting belongs to; meetings are workspace-level and may link tasks across projects",
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meetings",
        help_text="Optional project tag; cleared (not deleted) if the project is removed",
    )
    title = models.CharField(
        max_length=200,
        help_text="Short title shown in the calls list and on linked tasks",
    )
    happened_at = models.DateTimeField(
        help_text="When the call took place",
    )
    duration_minutes = models.PositiveIntegerField(
        validators=[
            MinValueValidator(1),
        ],
        help_text="Time spent on the call, in minutes; the whole-meeting duration that rolls up to linked tasks",
    )
    participants = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="meetings_attended",
        help_text="Workspace members who took part in the call",
    )
    notes = models.TextField(
        blank=True,
        help_text="Free-form Markdown notes or outcomes from the call",
    )
    tasks = models.ManyToManyField(
        "tasks.Task",
        blank=True,
        related_name="meetings",
        help_text="Tasks this call relates to; the meeting appears on each linked task's detail view",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meetings_created",
        help_text="Who logged the meeting; null if that user was later deleted",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the meeting record was created",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="When the meeting record was last edited",
    )

    class Meta:
        verbose_name = _("Meeting")
        verbose_name_plural = _("Meetings")
        ordering = [
            "-happened_at",
        ]
        indexes = [
            models.Index(
                fields=[
                    "workspace",
                    "-happened_at",
                ],
            ),
        ]

    def __str__(self) -> str:
        """Return the title and the date/time the call happened."""
        return f"{self.title} · {self.happened_at:%Y-%m-%d %H:%M}"

    @property
    def was_edited(self) -> bool:
        """Return True if the meeting was edited after it was created.

        Mirrors :attr:`apps.projects.models.ProjectUpdate.was_edited`: the
        ``auto_now_add`` / ``auto_now`` timestamps differ by microseconds on
        the initial INSERT, so a one-second tolerance avoids flagging a
        freshly logged meeting as ``(edited)``.

        Returns:
            True iff ``updated_at`` is more than one second after
            ``created_at``.
        """
        if self.created_at is None or self.updated_at is None:
            return False
        return (self.updated_at - self.created_at).total_seconds() > 1

    def human_duration(self) -> str:
        """Return the duration as a compact ``Hh Mm`` string.

        Examples: ``"45m"``, ``"1h"``, ``"1h 30m"``. Used in the calls
        list and on the linked-task rollup.

        Returns:
            A short human-readable duration label.
        """
        hours, minutes = divmod(self.duration_minutes, 60)
        if hours and minutes:
            return _("%(h)sh %(m)sm") % {"h": hours, "m": minutes}
        if hours:
            return _("%(h)sh") % {"h": hours}
        return _("%(m)sm") % {"m": minutes}
