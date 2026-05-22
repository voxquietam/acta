from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Cycle(models.Model):
    """A workspace-level time-box (Linear-style "cycle" / Scrum sprint).

    Cycles belong to the **workspace**, not a single project: the whole
    team runs one cadence across every project. They are derived from the
    workspace's cadence config (anchor date + length) and materialized
    lazily by :func:`apps.cycles.services.ensure_cycles` so tasks can
    point at a stable row. ``number`` is the 1-based index of the cycle's
    time window since the anchor, so it is monotonic and never reused.

    Status is a pure function of ``today`` versus the cycle's bounds and
    is reconciled on every ``ensure_cycles`` call; it is stored (not
    computed on read) so queries and the activity feed can filter on it.
    """

    PLANNING = "planning"
    ACTIVE = "active"
    COMPLETED = "completed"
    STATUS_CHOICES = [
        (PLANNING, _("Planning")),
        (ACTIVE, _("Active")),
        (COMPLETED, _("Completed")),
    ]

    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="cycles",
        help_text="Workspace this cycle belongs to; cadence is shared across all its projects",
    )
    number = models.PositiveIntegerField(
        help_text="1-based index of the cycle's time window since the cadence anchor; forms the label Cycle N",
    )
    name = models.CharField(
        max_length=120,
        blank=True,
        help_text="Optional custom name; falls back to Cycle N when empty",
    )
    start_date = models.DateField(
        help_text="First day of the cycle (inclusive)",
    )
    end_date = models.DateField(
        help_text="Last day of the cycle (inclusive)",
    )
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default=PLANNING,
        help_text="Lifecycle state derived from today vs the cycle bounds: planning, active, or completed",
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the cycle row was materialized",
    )
    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the cycle first transitioned to completed; frozen once set",
    )
    start_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the cycle-started notification was fanned out; stamped once to stay idempotent",
    )
    end_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the cycle-ending-soon notification was fanned out; stamped once to stay idempotent",
    )

    class Meta:
        verbose_name = _("Cycle")
        verbose_name_plural = _("Cycles")
        ordering = [
            "-start_date",
        ]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "workspace",
                    "number",
                ],
                name="cycles_unique_workspace_number",
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    "workspace",
                    "status",
                ],
            ),
        ]

    def __str__(self) -> str:
        """Return the workspace slug and cycle label, e.g. ``acme · Cycle 7``."""
        return f"{self.workspace.slug} · {self.display_name}"

    @property
    def display_name(self) -> str:
        """Return the custom name, or ``Cycle N`` when none is set."""
        return self.name or _("Cycle %(number)s") % {"number": self.number}

    @property
    def is_active(self) -> bool:
        """``True`` while this is the cycle in progress today."""
        return self.status == self.ACTIVE

    def days_remaining(self, today=None) -> int:
        """Return whole days left until the cycle ends (inclusive).

        Args:
            today: Reference date; defaults to the local current date.

        Returns:
            Days from ``today`` through ``end_date`` inclusive. ``0`` once
            the end date has passed (never negative).
        """
        today = today or timezone.localdate()
        return max(0, (self.end_date - today).days + 1)

    @property
    def length_days(self) -> int:
        """Return the cycle length in whole days (inclusive of both ends)."""
        return (self.end_date - self.start_date).days + 1
