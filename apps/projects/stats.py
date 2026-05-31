"""Snapshot helpers for the opt-in stats block on a project update.

When the author ticks "Include stats since last update" at compose time,
:func:`compute_update_stats` is called once and its result is frozen on
``ProjectUpdate.stats``. The snapshot stays meaningful even after the
underlying tasks change or are deleted — it captures what the project
looked like at the moment the update was posted.
"""

from django.db.models import Count, Q
from django.utils import timezone

from apps.tasks.models import Task


def compute_update_stats(project, since):
    """Return the frozen counters dict for a project at ``since → now``.

    One ``aggregate()`` query collects all counters via filtered ``Count``
    expressions — no per-status round trips. ``closed`` keys off the
    ``completed_at`` window (the moment a task transitioned into done),
    not the looser ``updated_at`` — see ``Task.save`` (``apps/tasks/
    models.py``) for where ``completed_at`` is stamped. The "now"
    counters are current snapshots; over time they drift from reality,
    but that's the point of freezing them.

    Args:
        project: The :class:`Project` whose counters we're snapping.
        since: ``datetime`` lower bound for the "closed" window. Usually
            the previous update's ``created_at``; falls back to
            ``project.created_at`` for the first update on a project.

    Returns:
        A JSON-serialisable dict with ``since`` / ``until`` ISO
        timestamps and integer counters: ``closed``, ``in_progress``,
        ``in_review``, ``planned``, ``ready``.
    """
    until = timezone.now()
    active = Q(archived_at__isnull=True)
    stats = Task.objects.filter(project=project).aggregate(
        closed=Count("id", filter=Q(completed_at__gte=since, completed_at__lte=until)),
        in_progress=Count("id", filter=active & Q(status=Task.STATUS_IN_PROGRESS)),
        in_review=Count("id", filter=active & Q(status=Task.STATUS_IN_REVIEW)),
        planned=Count("id", filter=active & Q(status=Task.STATUS_PLANNED)),
        ready=Count("id", filter=active & Q(status=Task.STATUS_READY)),
    )
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "closed": stats["closed"],
        "in_progress": stats["in_progress"],
        "in_review": stats["in_review"],
        "planned": stats["planned"],
        "ready": stats["ready"],
    }


def resolve_stats_window_start(project):
    """Pick the "since" anchor for a fresh stats snapshot on ``project``.

    Returns the previous update's ``created_at`` when one exists so the
    window covers the cadence the author is reporting on; otherwise the
    project's own ``created_at`` so the first-ever update reflects
    everything that has happened on the project so far.
    """
    latest = project.updates.order_by("-created_at").values_list("created_at", flat=True).first()
    return latest if latest is not None else project.created_at
