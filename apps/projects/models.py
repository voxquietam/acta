from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import F

SLUG_PREFIX_VALIDATOR = RegexValidator(
    regex=r"^[A-Z]{2,6}$",
    message="Slug prefix must be 2–6 uppercase Latin letters.",
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
        (ON_TRACK, "On track"),
        (AT_RISK, "At risk"),
        (OFF_TRACK, "Off track"),
        (COMPLETED, "Completed"),
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
        ordering = [
            "-created_at",
        ]

    def __str__(self) -> str:
        """Return project, health, and date for the update."""
        return f"{self.project} · {self.health} · {self.created_at:%Y-%m-%d}"
