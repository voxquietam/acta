from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _


class Task(models.Model):
    """A unit of work inside a project.

    Tasks have a per-project sequential ``number``. Combined with the
    project's ``slug_prefix`` this forms the user-facing ID (e.g.
    ``HRW-49``). Subtasks are modeled via a self-referential ``parent``
    FK with depth limited to one level. See
    docs/decisions/0007-data-model-task-project.md.
    """

    NO_PRIORITY = 0
    URGENT = 1
    HIGH = 2
    MEDIUM = 3
    LOW = 4
    PRIORITY_CHOICES = [
        (NO_PRIORITY, "No priority"),
        (URGENT, "Urgent"),
        (HIGH, "High"),
        (MEDIUM, "Medium"),
        (LOW, "Low"),
    ]

    # Status — fixed enum at this stage; stored as CharField without `choices=`
    # at the DB level so a future migration to a per-project Status FK is
    # non-destructive. See docs/decisions/0004-statuses.md.
    STATUS_PLANNED = "planned"
    STATUS_TODO = "to-do"
    STATUS_IN_PROGRESS = "in-progress"
    STATUS_IN_REVIEW = "in-review"
    STATUS_DONE = "done"
    STATUS_VALUES = (
        STATUS_PLANNED,
        STATUS_TODO,
        STATUS_IN_PROGRESS,
        STATUS_IN_REVIEW,
        STATUS_DONE,
    )
    # Human-readable status labels for the UI. Kept separate from
    # STATUS_VALUES because the DB stores internal codes (per ADR 0004);
    # only UI rendering uses these.
    STATUS_LABELS = {
        STATUS_PLANNED: _("Planned"),
        STATUS_TODO: _("To do"),
        STATUS_IN_PROGRESS: _("In progress"),
        STATUS_IN_REVIEW: _("In review"),
        STATUS_DONE: _("Done"),
    }

    SIZE_VALUES = (
        1,
        2,
        3,
        5,
        8,
        13,
    )
    SIZE_CHOICES = [(s, str(s)) for s in SIZE_VALUES]

    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        related_name="tasks",
        help_text="Project this task belongs to",
    )
    number = models.PositiveIntegerField(
        help_text=(
            "Sequential number within the project; combined with the project's slug "
            "prefix forms the user-facing ID (e.g. HRW-49)"
        ),
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="subtasks",
        help_text="Parent task if this is a subtask. Depth limited to one level",
    )

    title = models.CharField(
        max_length=200,
        help_text="Short title shown in lists and the kanban board",
    )
    description = models.TextField(
        blank=True,
        help_text="Full description in Markdown",
    )

    status = models.CharField(
        max_length=20,
        default=STATUS_TODO,
        help_text="Workflow state: one of planned, to-do, in-progress, in-review, done",
    )
    priority = models.SmallIntegerField(
        default=NO_PRIORITY,
        choices=PRIORITY_CHOICES,
        help_text="Task priority. 0 = no priority, 1 = urgent, 4 = low",
    )
    size = models.SmallIntegerField(
        null=True,
        blank=True,
        choices=SIZE_CHOICES,
        help_text="Story-point estimate. Restricted to the Fibonacci set 1, 2, 3, 5, 8, 13",
    )
    due_date = models.DateField(
        null=True,
        blank=True,
        help_text="Optional deadline (date only, no time-of-day)",
    )

    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_tasks",
        help_text="User responsible for completing the task",
    )
    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="reported_tasks",
        help_text="User who created the task. Set automatically from request.user",
    )
    labels = models.ManyToManyField(
        "labels.Label",
        blank=True,
        related_name="tasks",
        help_text="Labels attached to the task",
    )
    blocks = models.ManyToManyField(
        "self",
        symmetrical=False,
        related_name="blocked_by",
        blank=True,
        help_text="Tasks that cannot start until this one is done (directional). Reverse side is blocked_by",
    )
    related = models.ManyToManyField(
        "self",
        symmetrical=True,
        blank=True,
        help_text="Tasks related to this one without a blocking dependency (symmetric)",
    )

    archived_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Timestamp when the task was archived. Orthogonal to status: an archived task "
            "keeps its original status (typically done) so unarchiving restores the prior state. "
            "Archived tasks are hidden from default views — Show archived toggle in the sidebar "
            "brings them back, and ?xstatus / explicit filters still apply"
        ),
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the task was created",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="When the task was last modified",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "project",
                    "number",
                ],
                name="tasks_unique_project_number",
            ),
        ]
        indexes = [
            # Kanban grouping inside a project.
            models.Index(
                fields=[
                    "project",
                    "status",
                ],
            ),
            # Default project table sort (most-recent-first).
            models.Index(
                fields=[
                    "project",
                    "-updated_at",
                ],
            ),
            # My Work scope.
            models.Index(
                fields=[
                    "assignee",
                    "status",
                ],
            ),
            # Global table sort fallback (All Tasks default ordering).
            models.Index(
                fields=[
                    "-updated_at",
                ],
            ),
            # Sort-by-due-date in My Work / All Tasks. Nulls park
            # together (Postgres treats NULL ordering deterministically
            # within an index), and the index speeds up the
            # ``ORDER BY due_date ASC NULLS LAST`` query used by the
            # deadline bucketing in My Work.
            models.Index(
                fields=[
                    "due_date",
                ],
            ),
            # Sort-by-priority + tie-breaker on updated_at — matches the
            # ``order=priority`` clause shape in apps/web/filters.py.
            models.Index(
                fields=[
                    "priority",
                    "-updated_at",
                ],
            ),
            # Sort-by-size for the size column header.
            models.Index(
                fields=[
                    "size",
                ],
            ),
            # Default views exclude archived rows. Partial index keeps
            # the index small on tables where most rows are active,
            # and makes the ``WHERE archived_at IS NULL`` slice fast
            # without scanning archived rows. ``status, -updated_at``
            # covers both kanban + table table for the active set.
            models.Index(
                fields=[
                    "status",
                    "-updated_at",
                ],
                name="tasks_active_status_updated",
                condition=models.Q(archived_at__isnull=True),
            ),
            # Future dashboards / analytics: workload by assignee with
            # active-only filter. Saves a sequential scan when the
            # team grows past a few hundred members.
            models.Index(
                fields=[
                    "assignee",
                    "-updated_at",
                ],
                name="tasks_active_assignee_updated",
                condition=models.Q(archived_at__isnull=True),
            ),
        ]
        ordering = [
            "-updated_at",
        ]

    def __str__(self) -> str:
        """Return the user-facing slug and title (e.g. ``HRW-49 · Fix login``)."""
        return f"{self.project.slug_prefix}-{self.number} · {self.title}"

    @property
    def slug(self) -> str:
        """Return the user-facing identifier in ``<prefix>-<number>`` form.

        Returns:
            The string ``"{slug_prefix}-{number}"``, e.g. ``"HRW-49"``.
        """
        return f"{self.project.slug_prefix}-{self.number}"

    @property
    def incomplete_blockers(self):
        """Return the blocking tasks that are not yet done.

        A task is "blocked" while any task in its ``blocked_by`` set is
        still open (status != done) and not archived. Used by the
        Blocked badge + scrumban pull-flow signalling. Callers that
        render many tasks should ``prefetch_related("blocked_by")`` to
        avoid an N+1.
        """
        return [
            blocker
            for blocker in self.blocked_by.all()
            if blocker.status != self.STATUS_DONE and blocker.archived_at is None
        ]

    @property
    def is_blocked(self) -> bool:
        """``True`` when at least one incomplete task blocks this one."""
        return len(self.incomplete_blockers) > 0

    @property
    def is_blocking(self) -> bool:
        """``True`` when this task blocks at least one still-open task.

        Mirror of :attr:`is_blocked` for the other end of the edge.
        Callers rendering many tasks should ``prefetch_related("blocks")``.
        """
        return any(t.status != self.STATUS_DONE and t.archived_at is None for t in self.blocks.all())

    def clean(self) -> None:
        """Validate cross-field invariants beyond what field validators cover.

        Enforces:
            * Subtask depth limit of one (a subtask cannot have its own
              subtasks).
            * Subtask and parent must live in the same project.
            * ``size`` must be in the Fibonacci set if set at all.
            * ``status`` must be a known value from ``STATUS_VALUES``.

        Raises:
            ValidationError: If any invariant is violated. The error
                payload uses field names as keys so DRF / forms can map
                messages to inputs.
        """
        if self.parent_id is not None:
            if self.parent.parent_id is not None:
                raise ValidationError({"parent": "Subtasks cannot have their own subtasks (depth limit 1)."})
            if self.parent.project_id != self.project_id:
                raise ValidationError({"parent": "Subtask must be in the same project as its parent."})
        if self.size is not None and self.size not in self.SIZE_VALUES:
            raise ValidationError({"size": "Size must be one of 1, 2, 3, 5, 8, 13."})
        if self.status not in self.STATUS_VALUES:
            raise ValidationError({"status": f"Unknown status: {self.status!r}."})

    def save(self, *args, **kwargs):
        """Persist the task, allocating a project-scoped number on first save.

        On create (``self._state.adding`` is True) the task receives a
        fresh ``number`` from the project's counter via
        :meth:`Project.allocate_task_number`. The allocation runs inside a
        defensive ``transaction.atomic()`` so a missing outer transaction
        does not cause races; if the caller already holds one, the inner
        block is a savepoint and a no-op.

        Args:
            *args: Positional arguments forwarded to ``Model.save``.
            **kwargs: Keyword arguments forwarded to ``Model.save``.
        """
        if self._state.adding and not self.number:
            with transaction.atomic():
                self.number = self.project.allocate_task_number()
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)
