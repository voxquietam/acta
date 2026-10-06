from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.tasks.models import Task, counted_q


class MilestoneQuerySet(models.QuerySet):
    """Queryset helpers for milestone progress."""

    def with_rollup(self):
        """Annotate each milestone with the counts its progress is read from.

        One aggregate for a whole page: a milestone list shows a
        percentage per row, and walking the members per row would be an
        N+1 across the workspace. Counts through :func:`counted_q`, the
        same predicate the epic rollup uses — two near-identical
        expressions is how ``1/3`` once became ``0/2``.

        Returns:
            The queryset with ``counted_total`` and ``counted_done``
            annotations.
        """
        counted = counted_q("tasks")
        return self.annotate(
            counted_total=models.Count(
                "tasks",
                filter=counted,
                distinct=True,
            ),
            counted_done=models.Count(
                "tasks",
                filter=counted & models.Q(tasks__status=Task.STATUS_DONE),
                distinct=True,
            ),
        )


class Milestone(models.Model):
    """A date the work aims at.

    A point, not a span: one target date by which something must be
    true. It reaches one or more projects of its workspace — one project
    is a local checkpoint, four is a shared commitment every one of them
    shows. Tasks name it explicitly and never inherit it.

    See docs/decisions/0037-milestones.md.
    """

    STATE_OPEN = "open"
    STATE_TODAY = "today"
    STATE_OVERDUE = "overdue"
    STATE_COMPLETE = "complete"
    STATE_CLOSED = "closed"

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="milestones",
        help_text="Workspace this milestone belongs to; its projects must all be in it",
    )
    projects = models.ManyToManyField(
        "projects.Project",
        related_name="milestones",
        help_text="Projects aiming at this date; a task may only join from a project listed here",
    )
    name = models.CharField(
        max_length=120,
        help_text="Short name shown in lists, pickers and on the timeline marker",
    )
    goal = models.CharField(
        max_length=200,
        blank=True,
        help_text="One line saying what must be true when this is reached",
    )
    description = models.TextField(
        blank=True,
        help_text="Longer notes; the goal line is what the plan shows",
    )
    target_date = models.DateField(
        help_text="The date the work aims at; a milestone has no duration",
    )
    owner = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_milestones",
        help_text="Person answerable for the date; optional",
    )
    closed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When a person closed it; closing is an action, every other state is derived",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the milestone was created",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="When the milestone was last edited",
    )

    objects = MilestoneQuerySet.as_manager()

    class Meta:
        verbose_name = _("Milestone")
        verbose_name_plural = _("Milestones")
        ordering = [
            "target_date",
            "id",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "workspace",
                    "name",
                ],
                name="milestones_unique_workspace_name",
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    "workspace",
                    "target_date",
                ],
            ),
        ]

    def __str__(self) -> str:
        """Return the workspace slug, the name and the date."""
        return f"{self.workspace.slug} · {self.name} ({self.target_date.isoformat()})"

    @property
    def is_closed(self) -> bool:
        """``True`` once a person has closed this milestone."""
        return self.closed_at is not None

    def counted_tasks(self):
        """Return the tasks that count towards this milestone's progress.

        Cancelled work is not work; archived work that is done still
        counts; archived work that is not done is shelved and drops out.

        Returns:
            A queryset of :class:`~apps.tasks.models.Task`.
        """
        return self.tasks.filter(counted_q())

    def counts(self) -> tuple[int, int]:
        """Return ``(done, total)`` over the work that counts.

        Prefer :meth:`MilestoneQuerySet.with_rollup` when rendering more
        than one milestone — this walks the members for a single row.

        Returns:
            A ``(done, total)`` pair.
        """
        counted = self.counted_tasks()
        return (
            counted.filter(status=Task.STATUS_DONE).count(),
            counted.count(),
        )

    def state(self, today=None, counts=None) -> str:
        """Return the milestone's state for a given day.

        Closed is stored because the event either happened or it did
        not; everything else is read off the date and the work.

        Args:
            today: Reference date; defaults to the local current date.
            counts: A ``(done, total)`` pair the caller already has. A
                page drawing many milestones reads them all in one query
                and hands them in; without it every row pays the two
                counts :meth:`counts` runs.

        Returns:
            One of ``closed``, ``complete``, ``overdue``, ``today`` or
            ``open``.
        """
        if self.is_closed:
            return self.STATE_CLOSED
        done, total = counts if counts is not None else self.counts()
        if total and done == total:
            return self.STATE_COMPLETE
        today = today or timezone.localdate()
        if self.target_date < today:
            return self.STATE_OVERDUE
        if self.target_date == today:
            return self.STATE_TODAY
        return self.STATE_OPEN

    def clean(self):
        """Reject a scope that reaches outside the milestone's workspace.

        Raises:
            ValidationError: If any project belongs to another workspace.
        """
        super().clean()
        if self.pk is None or self.workspace_id is None:
            return
        outside = self.projects.exclude(workspace_id=self.workspace_id)
        if outside.exists():
            raise ValidationError(
                {"projects": "Every project must belong to the milestone's workspace."},
            )
