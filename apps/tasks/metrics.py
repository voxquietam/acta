"""Flow metrics computed from the activity log.

Scrumban needs measurable flow, not just a board. Every status change is
already an ``ActivityLog`` row (``task.status_changed`` with a
``{from, to}`` payload + ``created_at`` timestamp), so cycle time, lead
time and throughput are a replay of that log — no extra bookkeeping at
write time. This is the "live computation" half of the metrics design
(ADR 0022): cheap enough for the insights page (bounded by completed
tasks in a window, not a hot path). The Cumulative Flow Diagram, which
needs a daily WIP history, gets its own snapshot table separately.

All durations are in **hours** (float); the view layer formats them.
"""

from __future__ import annotations

from collections import defaultdict
import datetime
import statistics
from typing import Any

from django.utils import timezone

from apps.activity.models import ActivityLog

from .models import Task


def _percentile(values: list[float], pct: float) -> float | None:
    """Return the ``pct`` (0-100) percentile of ``values`` or ``None``.

    Uses nearest-rank on the sorted sample — adequate for the small
    samples a single project produces and free of interpolation
    surprises.

    Args:
        values: Unsorted numeric sample.
        pct: Percentile in ``[0, 100]``.

    Returns:
        The percentile value, or ``None`` for an empty sample.
    """
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered)) - 1))
    return ordered[k]


def compute_flow_metrics(project, *, today: datetime.date | None = None, weeks: int = 8) -> dict[str, Any]:
    """Compute cycle time, lead time and throughput for one project.

    Replays the project's ``task.status_changed`` events to derive, per
    task that is currently ``done``:

    * **cycle time** — first ``→ in-progress`` to last ``→ done``
      (the active-work span; skipped when the task never entered
      in-progress);
    * **lead time** — ``created_at`` to last ``→ done`` (includes
      backlog wait).

    Throughput counts completions (``→ done`` transitions) per ISO week
    over the trailing ``weeks`` window. Cycle / lead samples are limited
    to tasks completed inside that window so the medians track recent
    flow rather than all-time history.

    Args:
        project: The :class:`~apps.projects.models.Project` to measure.
        today: Date anchor for the trailing window (defaults to today).
        weeks: Size of the trailing window, in weeks.

    Returns:
        A dict with ``cycle_times`` / ``lead_times`` (hour floats),
        ``cycle_median`` / ``cycle_p85`` / ``lead_median`` (hours or
        ``None``), ``throughput`` (list of ``{week, label, count}``
        oldest-first), ``completed_count`` and ``window_weeks``.
    """
    today = today or timezone.localdate()
    window_start = today - datetime.timedelta(weeks=weeks)

    task_created = dict(Task.objects.filter(project=project).values_list("id", "created_at"))
    task_status = dict(Task.objects.filter(project=project).values_list("id", "status"))

    events = (
        ActivityLog.objects.filter(
            project=project,
            target_type=ActivityLog.TARGET_TASK,
            event_type="task.status_changed",
        )
        .order_by("created_at")
        .values_list("target_id", "payload", "created_at")
    )

    first_in_progress: dict[int, datetime.datetime] = {}
    last_done: dict[int, datetime.datetime] = {}
    for task_id, payload, occurred in events:
        to_status = (payload or {}).get("to")
        if to_status == Task.STATUS_IN_PROGRESS and task_id not in first_in_progress:
            first_in_progress[task_id] = occurred
        if to_status == Task.STATUS_DONE:
            last_done[task_id] = occurred

    cycle_times: list[float] = []
    lead_times: list[float] = []
    throughput_counts: dict[datetime.date, int] = defaultdict(int)
    completed = 0

    for task_id, done_at in last_done.items():
        # Only tasks that are *currently* done count as completed — a
        # later reopen (done → to-do) takes them back out of the sample.
        if task_status.get(task_id) != Task.STATUS_DONE:
            continue
        done_date = timezone.localtime(done_at).date()
        if done_date < window_start:
            continue
        completed += 1
        # ISO-week Monday as the bucket key.
        week_monday = done_date - datetime.timedelta(days=done_date.weekday())
        throughput_counts[week_monday] += 1
        created = task_created.get(task_id)
        if created:
            lead_times.append((done_at - created).total_seconds() / 3600.0)
        started = first_in_progress.get(task_id)
        if started and started <= done_at:
            cycle_times.append((done_at - started).total_seconds() / 3600.0)

    # Dense weekly throughput series across the whole window (zero-filled).
    throughput = []
    first_monday = window_start - datetime.timedelta(days=window_start.weekday())
    week = first_monday
    while week <= today:
        throughput.append(
            {
                "week": week.isoformat(),
                "label": week.strftime("%b %d"),
                "count": throughput_counts.get(week, 0),
            }
        )
        week += datetime.timedelta(weeks=1)

    return {
        "cycle_times": cycle_times,
        "lead_times": lead_times,
        "cycle_median": statistics.median(cycle_times) if cycle_times else None,
        "cycle_p85": _percentile(cycle_times, 85),
        "lead_median": statistics.median(lead_times) if lead_times else None,
        "throughput": throughput,
        "completed_count": completed,
        "window_weeks": weeks,
    }
