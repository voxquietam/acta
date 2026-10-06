from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


def counted_q(prefix: str = "") -> models.Q:
    """Build the "this task counts as work" predicate.

    One rule in one place, because epics and milestones both read it and
    two near-identical copies is how a container once went ``1/3 → 0/2``.
    Finished work keeps counting after the auto-archive job files it
    away; dropping it would walk progress backwards with nothing having
    happened, and leave a finished container reading ``0/0``. Unfinished
    work that was archived is shelved, not done, and stays out. Cancelled
    work is not work at all.

    Args:
        prefix: Relation to reach the task through, e.g. ``"epic_tasks"``
            or ``"tasks"``. Empty when filtering tasks directly.

    Returns:
        A :class:`~django.db.models.Q` selecting the tasks that count.
    """
    field = f"{prefix}__" if prefix else ""
    live = models.Q(**{f"{field}archived_at__isnull": True}) | models.Q(
        **{f"{field}status": Task.STATUS_DONE},
    )
    return live & ~models.Q(**{f"{field}status": Task.STATUS_CANCELLED})


class TaskQuerySet(models.QuerySet):
    """Queryset helpers that know about epics.

    Deliberately not installed as a default filter: Django reaches for
    the *base* manager on related descriptors (``project.tasks.all()``),
    so a default would be bypassed exactly where it matters, and a
    blanket exclusion would also 404 every epic detail page. The
    exclusion is explicit at the call sites that count work.
    See docs/decisions/0036-epics.md.
    """

    def work(self):
        """Return only the rows that are work, dropping epics.

        For every surface that counts, lists or boards tasks: an epic is
        an umbrella over work, not work itself, and counting it makes
        every total one too many.

        Returns:
            A :class:`TaskQuerySet` without epics.
        """
        return self.exclude(kind=Task.KIND_EPIC)

    def epics(self):
        """Return only the epics.

        Returns:
            A :class:`TaskQuerySet` of epics.
        """
        return self.filter(kind=Task.KIND_EPIC)

    def with_epic_rollup(self):
        """Annotate epics with the counts their progress is read from.

        One aggregate for the whole page: the Epics tab and the epic
        picker both show a percentage per epic, and walking
        ``epic_counts`` per row would be an N+1 over the entire
        workspace. Counts what :meth:`Task.epic_counted` counts — which
        is NOT what :meth:`Task.epic_members` lists.

        Returns:
            The queryset with ``member_total`` and ``member_done``
            annotations.
        """
        counted = counted_q("epic_tasks")
        return self.annotate(
            member_total=models.Count("epic_tasks", filter=counted, distinct=True),
            member_done=models.Count(
                "epic_tasks",
                filter=counted & models.Q(epic_tasks__status=Task.STATUS_DONE),
                distinct=True,
            ),
        )


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
    # Replenishment buffer between the backlog and active work: a groomed,
    # pullable task that hasn't started yet. Like planned, it carries no
    # cycle (the cadence policy commits a task to a cycle at to-do). See
    # docs/decisions/0004-statuses.md.
    STATUS_READY = "ready"
    STATUS_TODO = "to-do"
    STATUS_IN_PROGRESS = "in-progress"
    STATUS_IN_REVIEW = "in-review"
    STATUS_DONE = "done"
    # Terminal "won't do" state — the task was real and time may have been
    # spent, but the need disappeared. Distinct from done (completed) and
    # archive (hide). Sits last in the enum; hidden from default views and
    # the kanban board, and excluded from throughput. See ADR 0004.
    STATUS_CANCELLED = "cancelled"
    STATUS_VALUES = (
        STATUS_PLANNED,
        STATUS_READY,
        STATUS_TODO,
        STATUS_IN_PROGRESS,
        STATUS_IN_REVIEW,
        STATUS_DONE,
        STATUS_CANCELLED,
    )
    # Workflow statuses that surface as kanban columns. Excludes the
    # terminal STATUS_CANCELLED so the board stays focused on active work;
    # cancelling a task drops its card off the board (re-open by picking
    # any other status). STATUS_DONE stays a column.
    KANBAN_STATUS_VALUES = (
        STATUS_PLANNED,
        STATUS_READY,
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
        STATUS_READY: _("Ready"),
        STATUS_TODO: _("To do"),
        STATUS_IN_PROGRESS: _("In progress"),
        STATUS_IN_REVIEW: _("In review"),
        STATUS_DONE: _("Done"),
        STATUS_CANCELLED: _("Cancelled"),
    }

    # A task either *is* work or *collects* it. An epic is a task with
    # ``kind = epic``: nine of the eleven things it needs — description,
    # comments, attachments, activity, labels, assignee, links, search,
    # the graph — already live here, so it is a flag and not a model.
    # See docs/decisions/0036-epics.md.
    KIND_TASK = "task"
    KIND_EPIC = "epic"
    KIND_VALUES = (
        KIND_TASK,
        KIND_EPIC,
    )
    KIND_LABELS = {
        KIND_TASK: _("Task"),
        KIND_EPIC: _("Epic"),
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

    objects = TaskQuerySet.as_manager()

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
    kind = models.CharField(
        max_length=8,
        default=KIND_TASK,
        help_text="Whether this row is work (task) or collects work (epic)",
    )
    epic = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="epic_tasks",
        help_text=(
            "Epic this task belongs to. Deliberately separate from parent: a subtask "
            "keeps its parent and may still belong to an epic, and an epic collects "
            "across the whole workspace while a parent must share its child's project"
        ),
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
        default=STATUS_PLANNED,
        help_text="Workflow state: one of planned, ready, to-do, in-progress, in-review, done, cancelled",
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
    start_date = models.DateField(
        null=True,
        blank=True,
        help_text="Work start date. Auto-set to today on first transition to in-progress when null",
    )
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Planned finish date; drives the right edge of the timeline bar (start_date to end_date)",
    )
    due_date = models.DateField(
        null=True,
        blank=True,
        help_text="Hard deadline (a marker, not the bar end). Late when end_date or today is past it",
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
    cycle = models.ForeignKey(
        "cycles.Cycle",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
        help_text="Workspace cycle (time-box) this task is committed to; null means backlog",
    )
    milestone = models.ForeignKey(
        "milestones.Milestone",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="tasks",
        help_text=(
            "Milestone this task is committed to. At most one, and only one whose scope "
            "includes this task's project — membership is chosen, never inferred from dates"
        ),
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
        help_text=(
            "When the task was last modified. ``auto_now`` fires on ``.save()``; the bulk "
            "UPDATE path sets this column explicitly (see ``apps.tasks.bulk``) so it stays "
            "correct when ``save()`` is bypassed for batched writes"
        ),
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Timestamp when the task entered the done status; cleared when it leaves done. "
            "Maintained in save() and the bulk update path. Powers the completed-date filter"
        ),
    )
    recurrence = models.ForeignKey(
        "recurring.RecurringTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="instances",
        help_text=(
            "Recurring-task rule that spawned this task; null for normal tasks. "
            "SET_NULL so the task survives the rule's deletion"
        ),
    )
    occurrence_date = models.DateField(
        null=True,
        blank=True,
        help_text=(
            "The scheduled occurrence date this task was generated for; null for "
            "normal tasks. Unique per recurrence for idempotent generation"
        ),
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
            models.UniqueConstraint(
                fields=[
                    "recurrence",
                    "occurrence_date",
                ],
                condition=models.Q(recurrence__isnull=False),
                name="tasks_unique_recurrence_occurrence",
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
    def file_attachments(self):
        """Return this task's panel file attachments, uploader preloaded.

        Excludes inline editor images (``kind="inline_image"``), which live
        in the description rather than the attachments panel. Evaluated
        once per task-detail render, so it stays a single query; not for
        use across a list of tasks.

        Returns:
            A queryset of :class:`apps.attachments.models.Attachment`,
            oldest first.
        """
        from apps.attachments.models import Attachment

        return self.attachments.filter(kind=Attachment.KIND_FILE).select_related("uploader")

    @property
    def is_blocking(self) -> bool:
        """``True`` when this task blocks at least one still-open task.

        Mirror of :attr:`is_blocked` for the other end of the edge.
        Callers rendering many tasks should ``prefetch_related("blocks")``.
        """
        return any(t.status != self.STATUS_DONE and t.archived_at is None for t in self.blocks.all())

    @property
    def done_subtask_count(self) -> int:
        """Return how many of this task's subtasks are done.

        Reads the prefetched ``subtasks`` cache, so a board rendering
        many cards pays nothing extra for it.

        Returns:
            The number of subtasks in ``done``.
        """
        return sum(1 for sub in self.subtasks.all() if sub.status == self.STATUS_DONE)

    # ---- epic rollup -----------------------------------------------------
    #
    # Everything an epic says about its own state is read off the tasks it
    # collects, never stored: a status someone can type is a second source
    # of truth about the same thing, and an epic marked done at four of
    # fourteen is simply wrong. Computed on read rather than denormalised
    # because ``QuerySet.update()`` — which the bulk endpoint uses — fires
    # no signal, so a cached rollup would go stale exactly when a lot of
    # tasks move at once. See docs/decisions/0036-epics.md.

    def epic_members(self, *, include_archived=False):
        """Return the tasks this epic collects, as a queryset.

        The live work, which is what a board shows. Cancelled tasks are
        never included — "won't do" is not work. Archived ones are out by
        default and come back on request, the same way the rest of the
        app treats the archive: filed away, not gone. Progress is counted
        from a different set, :meth:`epic_counted`.

        Args:
            include_archived: Bring the archived members back, for the
                board's "show archived" toggle.

        Returns:
            A queryset of :class:`Task`, empty for a non-epic.
        """
        if self.kind != self.KIND_EPIC:
            return Task.objects.none()
        members = self.epic_tasks.exclude(status=self.STATUS_CANCELLED)
        return members if include_archived else members.filter(archived_at__isnull=True)

    def epic_counted(self):
        """Return the tasks that count towards this epic's progress.

        Deliberately not :meth:`epic_members`. That one answers "what is
        on the board", and the board is live work. This one answers "how
        much of the effort is done", and finished work does not stop
        having happened when the auto-archive job files it away — losing
        it would walk the counter backwards (1/3 → 0/2) with nothing
        having changed, and leave a finished epic reading 0/0.

        Cancelled work is out: it was never done and never will be.
        Archived work that is *not* done is shelved, and counting it
        would hold progress down for work nobody is doing.

        Returns:
            A queryset of :class:`Task`, empty for a non-epic.
        """
        if self.kind != self.KIND_EPIC:
            return Task.objects.none()
        return self.epic_tasks.exclude(status=self.STATUS_CANCELLED).filter(
            models.Q(archived_at__isnull=True) | models.Q(status=self.STATUS_DONE),
        )

    @property
    def epic_counts(self) -> tuple[int, int]:
        """Return ``(done, total)`` over the tasks this epic collects.

        Callers rendering many epics should annotate instead — see
        :meth:`TaskQuerySet.with_epic_rollup` — since this walks the
        members of one epic.

        Returns:
            A ``(done, total)`` pair; ``(0, 0)`` for a non-epic.
        """
        members = list(self.epic_counted())
        return sum(1 for t in members if t.status == self.STATUS_DONE), len(members)

    @property
    def epic_status(self) -> str:
        """Return the status an epic is in, derived from its tasks.

        The ladder, highest rung first: everything finished means done;
        anything started (in progress, in review, or already done while
        others are not) means in progress; anything pulled means to-do;
        otherwise the epic is still planned. An epic with no tasks is
        planned — it has been described but not filled.

        Returns:
            One of :data:`STATUS_VALUES`.
        """
        # Counted, not listed: an epic whose work is finished and filed
        # away is done, not "planned" for want of anything on the board.
        statuses = [t.status for t in self.epic_counted()]
        if not statuses:
            return self.STATUS_PLANNED
        if all(s == self.STATUS_DONE for s in statuses):
            return self.STATUS_DONE
        if any(s in (self.STATUS_IN_PROGRESS, self.STATUS_IN_REVIEW, self.STATUS_DONE) for s in statuses):
            return self.STATUS_IN_PROGRESS
        if any(s == self.STATUS_TODO for s in statuses):
            return self.STATUS_TODO
        return self.STATUS_PLANNED

    @property
    def epic_span(self) -> tuple:
        """Return ``(earliest start, latest end)`` across the epic's tasks.

        An epic has no dates of its own; it spans whatever its work
        spans. A task contributes its ``start_date`` and whichever of
        ``due_date`` / ``end_date`` reaches furthest.

        Returns:
            A pair of dates, either of which may be ``None``.
        """
        starts, ends = [], []
        for task in self.epic_members():
            if task.start_date:
                starts.append(task.start_date)
            for value in (task.due_date, task.end_date):
                if value:
                    ends.append(value)
        return (min(starts) if starts else None, max(ends) if ends else None)

    def clean(self) -> None:
        """Validate cross-field invariants beyond what field validators cover.

        Enforces:
            * Subtask depth limit of one (a subtask cannot have its own
              subtasks).
            * Subtask and parent must live in the same project.
            * ``kind`` must be a known value.
            * An epic collects tasks (never other epics) from its own
              workspace, is never a subtask, and carries none of the
              fields it derives from its tasks.
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
        if self.kind not in self.KIND_VALUES:
            raise ValidationError({"kind": f"Unknown kind: {self.kind!r}."})
        if self.epic_id is not None:
            if self.kind == self.KIND_EPIC:
                raise ValidationError({"epic": "An epic cannot belong to another epic."})
            if self.epic.kind != self.KIND_EPIC:
                raise ValidationError({"epic": "Tasks can only be collected by an epic."})
            # An epic reaches across projects on purpose — that is what
            # ``parent`` cannot do — but never across workspaces, where
            # labels, members and cycles stop being valid.
            if self.epic.project.workspace_id != self.project.workspace_id:
                raise ValidationError({"epic": "Epic must be in the same workspace."})
        if (self.kind == self.KIND_EPIC or self.epic_id is not None) and not self.project.workspace.epics_enabled:
            # Turning the feature off hides it and refuses new ones; what
            # already exists keeps its tasks and returns on re-enable.
            raise ValidationError({"kind": "Epics are turned off for this workspace."})
        if self.kind == self.KIND_EPIC:
            if self.parent_id is not None:
                raise ValidationError({"parent": "An epic cannot be a subtask."})
            # Progress, dates and status are read off the tasks an epic
            # collects, so the fields a person would type are meaningless
            # on one.
            if self.due_date is not None:
                raise ValidationError({"due_date": "An epic takes its dates from its tasks."})
            if self.size is not None:
                raise ValidationError({"size": "An epic takes its size from its tasks."})
            if self.cycle_id is not None:
                raise ValidationError({"cycle": "An epic does not join a cycle."})
            if self.milestone_id is not None:
                raise ValidationError(
                    {"milestone": "An epic takes its milestones from its tasks."},
                )
        if self.milestone_id is not None and self.project_id is not None:
            # Scope is the whole reason a milestone can be shared: a task
            # joins one its own project aims at, or none. See ADR 0037.
            if not self.milestone.projects.filter(pk=self.project_id).exists():
                raise ValidationError(
                    {"milestone": "This milestone does not cover the task's project."},
                )
        if self.size is not None and self.size not in self.SIZE_VALUES:
            raise ValidationError({"size": "Size must be one of 1, 2, 3, 5, 8, 13."})
        if self.status not in self.STATUS_VALUES:
            raise ValidationError({"status": f"Unknown status: {self.status!r}."})

    def _sync_done_dates(self, kwargs):
        """Maintain the done-driven date fields from the status before saving.

        On the transition *into* done (detected via ``completed_at`` still
        being unset) this stamps two things: ``completed_at`` with the
        current time, and ``end_date`` with today — the actual finish date,
        which overwrites any planned ``end_date`` so the timeline bar ends
        where work really stopped. Subsequent saves while done are no-ops
        (``completed_at`` already set), so ``end_date`` is not re-bumped.
        Leaving done clears ``completed_at`` but keeps ``end_date`` (the
        finish stays on the record, mirroring how ``start_date`` survives a
        status revert). When the caller restricts the write to
        ``update_fields``, the touched fields are appended so the change is
        persisted — the bulk path (``QuerySet.update``) bypasses ``save``
        entirely and stamps these fields itself.

        Args:
            kwargs: The keyword arguments about to be passed to
                ``Model.save`` (mutated in place to extend
                ``update_fields`` when needed).
        """
        touched = []
        if self.status == self.STATUS_DONE:
            if self.completed_at is not None:
                return
            self.completed_at = timezone.now()
            self.end_date = timezone.localdate()
            touched = ["completed_at", "end_date"]
            # Task created directly in done — no in-flight period to measure,
            # so stamp ``start_date`` with the same day. Gives the timeline
            # bar both ends and lets cycle/lead-time metrics record a span
            # of zero instead of NaN. For tasks moving into done later we
            # leave ``start_date`` alone (``today`` would be a lie about
            # when work began).
            if self._state.adding and self.start_date is None:
                self.start_date = self.end_date
                touched.append("start_date")
        else:
            if self.completed_at is None:
                return
            self.completed_at = None
            touched = ["completed_at"]
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            missing = [f for f in touched if f not in update_fields]
            if missing:
                kwargs["update_fields"] = list(update_fields) + missing

    def save(self, *args, **kwargs):
        """Persist the task, allocating a project-scoped number on first save.

        On create (``self._state.adding`` is True) the task receives a
        fresh ``number`` from the project's counter via
        :meth:`Project.allocate_task_number`. The allocation runs inside a
        defensive ``transaction.atomic()`` so a missing outer transaction
        does not cause races; if the caller already holds one, the inner
        block is a savepoint and a no-op. ``completed_at`` and ``end_date``
        are reconciled from the status on every save (see
        :meth:`_sync_done_dates`).

        Args:
            *args: Positional arguments forwarded to ``Model.save``.
            **kwargs: Keyword arguments forwarded to ``Model.save``.
        """
        self._sync_done_dates(kwargs)
        if self._state.adding and not self.number:
            with transaction.atomic():
                self.number = self.project.allocate_task_number()
                super().save(*args, **kwargs)
            return
        super().save(*args, **kwargs)


class TaskEmbedding(models.Model):
    """One task's meaning as a vector, for "what already looks like this".

    Written by :mod:`apps.tasks.similarity` after the task's text changes
    and read whenever something asks for neighbours — the create dialog,
    the MCP tools, the task page. The vector is stored already
    normalised, so comparing two of them is a plain dot product.

    ``workspace`` is denormalised off ``task.project.workspace`` on
    purpose: every read loads one workspace's vectors in a single query,
    and joining through the project to do it would put a join on the hot
    path for no gain.

    A row is only comparable to rows built by the same model, which is
    why ``model`` is stored next to the vector rather than assumed from
    settings: swapping the model leaves the old rows in place but out of
    every search until the backfill rebuilds them.
    """

    task = models.OneToOneField(
        "tasks.Task",
        on_delete=models.CASCADE,
        related_name="embedding",
        help_text="Task this vector describes",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="task_embeddings",
        help_text="Denormalised from the task's project so one query loads a whole workspace",
    )
    vector = models.BinaryField(
        help_text="Normalised float32 vector, little-endian, as produced by the embedding model",
    )
    dimensions = models.PositiveSmallIntegerField(
        help_text="Length of the vector, so a mismatched model is caught before the dot product",
    )
    model = models.CharField(
        max_length=120,
        help_text="Embedding model that produced this vector; rows from other models are ignored",
    )
    text_hash = models.CharField(
        max_length=64,
        help_text="SHA-256 of the text that was embedded, so unchanged tasks are not re-sent",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="When the vector was last rebuilt; also stamps the in-process matrix cache",
    )

    class Meta:
        verbose_name = "task embedding"
        verbose_name_plural = "task embeddings"
        indexes = [
            models.Index(fields=["workspace", "model"]),
        ]

    def __str__(self):
        """Return the task slug plus the model that embedded it."""
        return f"{self.task.slug} ({self.model})"
