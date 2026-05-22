"""Auto-rolling cadence logic for workspace cycles.

Cycles are deterministic time windows derived from a workspace's cadence
config (an anchor date plus a length in weeks). Rather than a background
job minting rows on a schedule, the windows are computed on demand and
the current + next cycle are materialized lazily — :func:`ensure_cycles`
is cheap and idempotent, so any view that needs cycles just calls it.

The mapping is a pure function: window ``index`` (0-based, since the
anchor) has bounds ``[anchor + index·length, …]`` and surfaces as
``Cycle.number == index + 1``. Status is reconciled from ``today`` on
every call; bounds of already-created rows are never rewritten, so a
later cadence change reshapes only cycles not yet materialized.
"""

from __future__ import annotations

import datetime

from django.db import transaction
from django.utils import timezone


def cycle_bounds(anchor: datetime.date, length_weeks: int, index: int) -> tuple[datetime.date, datetime.date]:
    """Return the ``(start, end)`` dates for the window at ``index``.

    Args:
        anchor: Start date of window 0 (the cadence anchor).
        length_weeks: Window length in whole weeks.
        index: 0-based window index since the anchor.

    Returns:
        A ``(start_date, end_date)`` tuple with ``end_date`` inclusive.
    """
    span = length_weeks * 7
    start = anchor + datetime.timedelta(days=span * index)
    end = start + datetime.timedelta(days=span - 1)
    return start, end


def current_index(anchor: datetime.date, length_weeks: int, today: datetime.date) -> int:
    """Return the 0-based window index that contains ``today``.

    Clamped to ``0`` while ``today`` is still before the anchor — the
    cadence hasn't started, so the first window is the relevant one (it
    will read as ``planning`` until its start date arrives).
    """
    span = length_weeks * 7
    delta = (today - anchor).days
    if delta < 0:
        return 0
    return delta // span


def _status_for(start: datetime.date, end: datetime.date, today: datetime.date) -> str:
    """Return the lifecycle status for a window given ``today``."""
    from .models import Cycle

    if today < start:
        return Cycle.PLANNING
    if today > end:
        return Cycle.COMPLETED
    return Cycle.ACTIVE


def reconcile_statuses(workspace, today: datetime.date) -> None:
    """Recompute every cycle's status for ``workspace`` from ``today``.

    Sets ``completed_at`` the first time a cycle crosses into completed
    and leaves it frozen thereafter. Only writes rows whose status (or
    newly-set ``completed_at``) actually changed.
    """
    from .models import Cycle

    for cycle in workspace.cycles.all():
        new_status = _status_for(cycle.start_date, cycle.end_date, today)
        fields = []
        if cycle.status != new_status:
            cycle.status = new_status
            fields.append("status")
        if new_status == Cycle.COMPLETED and cycle.completed_at is None:
            cycle.completed_at = timezone.now()
            fields.append("completed_at")
        if fields:
            cycle.save(update_fields=fields)


def ensure_cycles(workspace, today: datetime.date | None = None):
    """Materialize the current + next cycle and reconcile all statuses.

    No-op (returns ``None``) when the workspace has cadence disabled.
    Otherwise idempotently creates the two windows tasks can be assigned
    to (current and upcoming) and refreshes every cycle's status, then
    returns the active/current cycle. Rolls strictly forward — windows
    that elapsed before the first call are never backfilled, so setting
    an anchor far in the past does not spawn a row per missed window.

    Args:
        workspace: The :class:`~apps.workspaces.models.Workspace`.
        today: Reference date; defaults to the local current date.

    Returns:
        The current :class:`~apps.cycles.models.Cycle` (the window
        containing ``today``), or ``None`` when cadence is disabled.
    """
    from .models import Cycle

    cfg = workspace.cycle_config()
    if not cfg["enabled"] or not cfg["start_date"]:
        return None
    today = today or timezone.localdate()
    anchor = datetime.date.fromisoformat(cfg["start_date"])
    length = cfg["length_weeks"]

    idx = current_index(anchor, length, today)
    with transaction.atomic():
        for i in (idx, idx + 1):
            start, end = cycle_bounds(anchor, length, i)
            Cycle.objects.get_or_create(
                workspace=workspace,
                number=i + 1,
                defaults={
                    "start_date": start,
                    "end_date": end,
                },
            )
        reconcile_statuses(workspace, today)

    return workspace.cycles.filter(number=idx + 1).first()


def apply_cycle_policy(task) -> bool:
    """Enforce the status-driven cycle rule on a task, in memory.

    The cadence model couples cycle membership to status:

    * ``planned`` is the backlog — a planned task carries **no** cycle.
    * Moving into committed work (``to-do`` / ``in-progress`` /
      ``in-review``) pulls the task into the workspace's **active**
      cycle, but only when it has none yet (never overrides a deliberate
      pick).
    * ``done`` / ``cancelled`` keep whatever cycle they had.

    Mutates ``task.cycle`` in memory and returns whether it changed; the
    caller is responsible for saving. No-op (returns ``False``) when the
    workspace has cadence disabled. Requires ``task.project`` loaded.

    Args:
        task: The :class:`~apps.tasks.models.Task` whose status was just
            set (in memory) and whose cycle should be reconciled.

    Returns:
        ``True`` if ``task.cycle`` was changed, else ``False``.
    """
    from apps.tasks.models import Task

    workspace = task.project.workspace
    if not workspace.cycle_config()["enabled"]:
        return False
    if task.status == Task.STATUS_PLANNED:
        if task.cycle_id is not None:
            task.cycle = None
            return True
        return False
    committed = (Task.STATUS_TODO, Task.STATUS_IN_PROGRESS, Task.STATUS_IN_REVIEW)
    if task.status in committed and task.cycle_id is None:
        ensure_cycles(workspace)
        active = current_cycle(workspace)
        if active is not None:
            task.cycle = active
            return True
    return False


def cycle_summary(cycle, today: datetime.date | None = None) -> dict:
    """Return progress counters for a cycle's committed work.

    Counts the cycle's active tasks (excludes cancelled + archived) and
    their story points, split by done vs. total, plus the days remaining.
    Drives the active-cycle header. One aggregate query.

    Args:
        cycle: The :class:`~apps.cycles.models.Cycle`.
        today: Reference date for ``days_remaining``; defaults to local
            today.

    Returns:
        A dict with ``total``, ``done``, ``points_total``,
        ``points_done``, ``percent`` (0-100 by task count), and
        ``days_remaining``.
    """
    from apps.tasks.models import Task

    rows = (
        cycle.tasks.exclude(status=Task.STATUS_CANCELLED)
        .filter(archived_at__isnull=True)
        .values_list(
            "status",
            "size",
        )
    )
    total = done = points_total = points_done = 0
    for status, size in rows:
        total += 1
        pts = size or 0
        points_total += pts
        if status == Task.STATUS_DONE:
            done += 1
            points_done += pts
    percent = round(done / total * 100) if total else 0
    return {
        "total": total,
        "done": done,
        "points_total": points_total,
        "points_done": points_done,
        "percent": percent,
        "days_remaining": cycle.days_remaining(today),
    }


def _done_dates(task_ids: list[int]) -> dict[int, datetime.date]:
    """Map each task to the date it last became done, by activity replay.

    Walks ``task.status_changed`` events oldest-first: a transition *to*
    done records the day; any transition *away* clears it. The surviving
    entries are tasks currently done and the day they reached it. Mirrors
    the activity-log-replay approach in :mod:`apps.tasks.metrics`
    (ADR 0026) — no snapshot table.

    Args:
        task_ids: Task primary keys to resolve done-dates for.

    Returns:
        ``{task_id: date}`` for tasks currently in the done state.
    """
    from apps.activity.models import ActivityLog
    from apps.tasks.models import Task

    if not task_ids:
        return {}
    events = (
        ActivityLog.objects.filter(
            target_type=ActivityLog.TARGET_TASK,
            target_id__in=task_ids,
            event_type="task.status_changed",
        )
        .order_by("created_at")
        .values_list("target_id", "payload", "created_at")
    )
    done: dict[int, datetime.date] = {}
    for task_id, payload, created in events:
        to_status = (payload or {}).get("to")
        day = timezone.localtime(created).date() if timezone.is_aware(created) else created.date()
        if to_status == Task.STATUS_DONE:
            done[task_id] = day
        else:
            done.pop(task_id, None)
    return done


def compute_cycle_burndown(cycle, today: datetime.date | None = None) -> dict:
    """Return a daily burndown series for a cycle plus its ideal line.

    Tracks the count of still-open (not-done) committed tasks across the
    cycle's calendar days. The ``ideal`` line drops linearly from the
    total scope to zero over the full span; ``remaining`` carries actuals
    up to today and ``None`` afterwards so the chart line stops at today.

    Args:
        cycle: The :class:`~apps.cycles.models.Cycle`.
        today: Reference date; defaults to local today.

    Returns:
        ``{labels, ideal, remaining, total}`` ready for Chart.js.
    """
    from apps.tasks.models import Task

    today = today or timezone.localdate()
    task_ids = list(
        cycle.tasks.exclude(status=Task.STATUS_CANCELLED).filter(archived_at__isnull=True).values_list("id", flat=True),
    )
    total = len(task_ids)
    done_on = _done_dates(task_ids)

    span = cycle.length_days
    labels = [(cycle.start_date + datetime.timedelta(days=i)).isoformat() for i in range(span)]
    if span > 1:
        ideal = [round(total * (1 - i / (span - 1)), 2) for i in range(span)]
    else:
        ideal = [0]

    last_actual = min(today, cycle.end_date)
    remaining: list = []
    for i in range(span):
        day = cycle.start_date + datetime.timedelta(days=i)
        if day > last_actual:
            remaining.append(None)
            continue
        done_by_day = sum(1 for d in done_on.values() if d <= day)
        remaining.append(total - done_by_day)
    return {"labels": labels, "ideal": ideal, "remaining": remaining, "total": total}


def compute_velocity(workspace, limit: int = 6) -> list[dict]:
    """Return completed work per recent non-planning cycle (velocity).

    Oldest-first list of the last ``limit`` active/completed cycles with
    the count and story-point sum of their done tasks. Planning cycles
    are skipped (nothing delivered yet).

    Args:
        workspace: The :class:`~apps.workspaces.models.Workspace`.
        limit: Maximum number of cycles to include.

    Returns:
        ``[{"label", "count", "points"}]`` oldest cycle first.
    """
    from apps.cycles.models import Cycle
    from apps.tasks.models import Task

    cycles = list(
        workspace.cycles.exclude(status=Cycle.PLANNING).order_by("-start_date")[:limit],
    )
    cycles.reverse()
    out = []
    for cycle in cycles:
        count = points = 0
        for size in cycle.tasks.filter(status=Task.STATUS_DONE, archived_at__isnull=True).values_list(
            "size", flat=True
        ):
            count += 1
            points += size or 0
        out.append({"label": str(cycle.display_name), "count": count, "points": points})
    return out


def current_cycle(workspace, today: datetime.date | None = None):
    """Return the workspace's active cycle without materializing rows.

    Read-only lookup for surfaces that should not trigger creation (the
    cadence config is assumed already materialized by an earlier
    :func:`ensure_cycles`). Returns ``None`` when cadence is disabled or
    no active cycle exists yet.
    """
    from .models import Cycle

    return workspace.cycles.filter(status=Cycle.ACTIVE).order_by("-start_date").first()
