from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F
from django.utils.translation import gettext_lazy as _

SLUG_PREFIX_VALIDATOR = RegexValidator(
    regex=r"^[A-Z]{2,6}$",
    message=_("Slug prefix must be 2–6 uppercase Latin letters."),
)


class Project(models.Model):
    """A project inside a workspace. Owns tasks, updates, and a slug counter.

    Slug prefixes (e.g. ``HRW``) are immutable in practice — changing them
    would invalidate every ``HRW-49``-style reference scattered across
    comments, descriptions, and historical activity log entries.
    See docs/decisions/0007-data-model-task-project.md.
    """

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="projects",
        help_text="Workspace this project belongs to",
    )
    name = models.CharField(
        max_length=120,
        help_text="Display name of the project",
    )
    icon = models.CharField(
        max_length=40,
        blank=True,
        help_text="Lucide icon name shown next to the project in the sidebar and lists. Optional",
    )
    description = models.TextField(
        blank=True,
        help_text="Project description in Markdown. Optional",
    )
    slug_prefix = models.CharField(
        max_length=6,
        validators=[
            SLUG_PREFIX_VALIDATOR,
        ],
        help_text=("Short uppercase identifier used in task references, e.g. HRW in HRW-49. " "Immutable in practice"),
    )
    lead = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="led_projects",
        help_text="Single user responsible for the project's direction. Implicit member",
    )
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="project_memberships",
        help_text=(
            "Contributors who work on the project. Opt-in list used for 'My Projects' filters "
            "and the subscriber set for project updates. Not enforced on Task.assignee — any "
            "workspace member can still be assigned a task"
        ),
    )
    next_task_number = models.PositiveIntegerField(
        default=1,
        help_text="Counter for the next task to be created in this project. Auto-incremented",
    )
    archived = models.BooleanField(
        default=False,
        help_text="Archived projects are hidden by default but retain their tasks and history",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the project was created",
    )

    class Meta:
        verbose_name = _("Project")
        verbose_name_plural = _("Projects")
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "workspace",
                    "slug_prefix",
                ],
                name="projects_unique_workspace_slug_prefix",
            ),
        ]

    def __str__(self) -> str:
        """Return the project slug prefix and name, e.g. ``HRW · Home Work``."""
        return f"{self.slug_prefix} · {self.name}"

    def clean(self):
        """Validate that ``lead`` (if set) is a member of the workspace.

        The ``members`` M2M set is validated at the serializer / admin
        layer instead — Django can't introspect M2M before the project
        row exists.
        """
        super().clean()
        if self.lead_id and self.workspace_id:
            is_workspace_member = self.workspace.members.filter(pk=self.lead_id).exists()
            if not is_workspace_member:
                raise ValidationError({"lead": _("Lead must be a member of the project's workspace.")})

    def allocate_task_number(self) -> int:
        """Reserve and return the next task number for this project.

        Acquires a row-level lock on the project row via
        ``SELECT FOR UPDATE`` so concurrent task creations serialize
        without colliding on the same number. Numbers are monotonic and
        never reused — deleting a task does not free its number.

        Caller MUST be inside ``transaction.atomic()``; the lock is held
        until the surrounding transaction commits.

        Returns:
            The newly reserved task number.

        Raises:
            Project.DoesNotExist: If the project row was deleted while
                this transaction was preparing to lock it.
        """
        locked = Project.objects.select_for_update().get(pk=self.pk)
        number = locked.next_task_number
        Project.objects.filter(pk=self.pk).update(
            next_task_number=F("next_task_number") + 1,
        )
        return number

    def allocate_task_numbers(self, count: int) -> list[int]:
        """Reserve and return ``count`` consecutive task numbers.

        Bulk variant of :meth:`allocate_task_number` for batch operations
        (project moves, future bulk imports). Atomically increments the
        counter by ``count`` so a single ``SELECT FOR UPDATE`` plus one
        ``UPDATE`` reserves the whole range — no per-row locking.

        Caller MUST be inside ``transaction.atomic()``; the lock is held
        until the surrounding transaction commits.

        Args:
            count: Number of consecutive task numbers to reserve. Must be
                positive.

        Returns:
            A list of length ``count`` with the reserved numbers in
            ascending order.

        Raises:
            ValueError: If ``count`` is not positive.
            Project.DoesNotExist: If the project row was deleted while
                this transaction was preparing to lock it.
        """
        if count <= 0:
            raise ValueError("count must be positive")
        locked = Project.objects.select_for_update().get(pk=self.pk)
        start = locked.next_task_number
        Project.objects.filter(pk=self.pk).update(
            next_task_number=F("next_task_number") + count,
        )
        return list(range(start, start + count))


class ProjectUpdate(models.Model):
    """A Linear-style manual status post on a project.

    See docs/decisions/0009-project-updates.md. Not auto-tracked in the
    activity log — the updates themselves are the audit trail for this
    surface.
    """

    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    OFF_TRACK = "off_track"
    COMPLETED = "completed"
    HEALTH_CHOICES = [
        (ON_TRACK, _("On track")),
        (AT_RISK, _("At risk")),
        (OFF_TRACK, _("Off track")),
        (COMPLETED, _("Completed")),
    ]

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="updates",
        help_text="Project this status update is about",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="project_updates",
        help_text="User who wrote the update",
    )
    health = models.CharField(
        max_length=12,
        choices=HEALTH_CHOICES,
        help_text="Current health signal of the project as judged by the author",
    )
    body = models.TextField(
        help_text="Update body in Markdown",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the update was posted",
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        help_text="When the update was last edited",
    )

    class Meta:
        verbose_name = _("Project update")
        verbose_name_plural = _("Project updates")
        ordering = [
            "-created_at",
        ]

    def __str__(self) -> str:
        """Return project, health, and date for the update."""
        return f"{self.project} · {self.health} · {self.created_at:%Y-%m-%d}"
