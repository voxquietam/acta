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


def reconcile_statuses(workspace, today: datetime.date) -> list:
    """Recompute every cycle's status for ``workspace`` from ``today``.

    Sets ``completed_at`` the first time a cycle crosses into completed
    and leaves it frozen thereafter. Only writes rows whose status (or
    newly-set ``completed_at``) actually changed.

    Returns:
        The list of cycles that transitioned **into** ``completed`` during
        this call (used to trigger task roll-over). Empty on steady state.
    """
    from .models import Cycle

    newly_completed = []
    for cycle in workspace.cycles.all():
        new_status = _status_for(cycle.start_date, cycle.end_date, today)
        fields = []
        status_changed = cycle.status != new_status
        if status_changed:
            cycle.status = new_status
            fields.append("status")
        if new_status == Cycle.COMPLETED and cycle.completed_at is None:
            cycle.completed_at = timezone.now()
            fields.append("completed_at")
        if fields:
            cycle.save(update_fields=fields)
        if status_changed and new_status == Cycle.COMPLETED:
            newly_completed.append(cycle)
    return newly_completed


def _rollover_unfinished(workspace, active_cycle, from_cycle_ids: list) -> None:
    """Move unfinished tasks out of just-completed cycles into the active one.

    Unfinished = ``to-do`` / ``in-progress`` / ``in-review`` and not
    archived. Done / cancelled stay put (they belong to the cycle that
    delivered — or didn't — them, and feed velocity). Emits a
    ``task.cycle_changed`` event per moved task with a ``None`` (system)
    actor, so timelines record the roll-over and peer boards refresh over
    SSE. Idempotent: a second pass finds nothing left to move.

    Args:
        workspace: The :class:`~apps.workspaces.models.Workspace`.
        active_cycle: The current active :class:`Cycle` to move into.
        from_cycle_ids: Ids of the cycles that just completed.
    """
    from apps.activity.models import ActivityLog
    from apps.tasks.events import broadcast_task_events, build_diff_events, snapshot_task
    from apps.tasks.models import Task

    if active_cycle is None or not from_cycle_ids:
        return
    movers = list(
        Task.objects.filter(
            cycle_id__in=from_cycle_ids,
            status__in=(Task.STATUS_TODO, Task.STATUS_IN_PROGRESS, Task.STATUS_IN_REVIEW),
            archived_at__isnull=True,
        )
        .exclude(cycle_id=active_cycle.id)
        .select_related("project__workspace", "cycle", "assignee")
        .prefetch_related("labels", "blocks", "blocked_by")
    )
    if not movers:
        return
    now = timezone.now()
    events = []
    for task in movers:
        old_state = snapshot_task(task)
        task.cycle = active_cycle
        task.updated_at = now
        events.extend(build_diff_events(old_state=old_state, task=task, actor=None))
    Task.objects.bulk_update(movers, ["cycle", "updated_at"])
    if events:
        ActivityLog.objects.bulk_create(events)
        broadcast_task_events(events, {t.pk: t for t in movers}, None)


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
        newly_completed = reconcile_statuses(workspace, today)
        active = workspace.cycles.filter(number=idx + 1).first()
        # Auto roll-over: when cadence config opts in, unfinished tasks of a
        # cycle that just ended follow the team into the new active cycle.
        if cfg.get("auto_rollover") and newly_completed and active is not None and active.status == Cycle.ACTIVE:
            _rollover_unfinished(workspace, active, [c.id for c in newly_completed])

    return active


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
    # planned + ready are the backlog zone — no cycle. Committing to work
    # (to-do onward) is what pulls a task into the active cycle.
    if task.status in (Task.STATUS_PLANNED, Task.STATUS_READY):
        if task.cycle_id is not None:
            task.cycle = None
            return True
        return False
    committed = (Task.STATUS_TODO, Task.STATUS_IN_PROGRESS, Task.STATUS_IN_REVIEW)
    if task.status in committed and task.cycle_id is None:
        # Cheap path first: the active cycle is almost always already
        # materialized (page loads run ensure_cycles). Only fall back to
        # the full materialize/reconcile when there's no active cycle yet,
        # so a routine status change doesn't pay for it every time.
        active = current_cycle(workspace)
        if active is None:
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


def cycle_summaries(cycles, today: datetime.date | None = None) -> dict:
    """Batch :func:`cycle_summary` for many cycles in a single query.

    Avoids the N-query loop of calling :func:`cycle_summary` per cycle on
    the dashboard — one grouped aggregate covers the whole list.

    Args:
        cycles: Iterable of :class:`~apps.cycles.models.Cycle`.
        today: Reference date for ``days_remaining``; defaults to today.

    Returns:
        ``{cycle_id: summary_dict}`` with the same shape as
        :func:`cycle_summary`.
    """
    from django.db.models import Count, Q, Sum

    from apps.tasks.models import Task

    cycles = list(cycles)
    ids = [c.id for c in cycles]
    rows = (
        Task.objects.filter(cycle_id__in=ids)
        .exclude(status=Task.STATUS_CANCELLED)
        .filter(archived_at__isnull=True)
        .values("cycle_id")
        .annotate(
            total=Count("id"),
            done=Count("id", filter=Q(status=Task.STATUS_DONE)),
            points_total=Sum("size"),
            points_done=Sum("size", filter=Q(status=Task.STATUS_DONE)),
        )
    )
    by_id = {r["cycle_id"]: r for r in rows}
    out = {}
    for cycle in cycles:
        row = by_id.get(cycle.id, {})
        total = row.get("total") or 0
        done = row.get("done") or 0
        out[cycle.id] = {
            "total": total,
            "done": done,
            "points_total": row.get("points_total") or 0,
            "points_done": row.get("points_done") or 0,
            "percent": round(done / total * 100) if total else 0,
            "days_remaining": cycle.days_remaining(today),
        }
    return out


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
    from django.db.models import Count, Sum

    from apps.cycles.models import Cycle
    from apps.tasks.models import Task

    cycles = list(
        workspace.cycles.exclude(status=Cycle.PLANNING).order_by("-start_date")[:limit],
    )
    cycles.reverse()
    # One grouped query for the whole window — not one per cycle.
    rows = (
        Task.objects.filter(
            cycle_id__in=[c.id for c in cycles],
            status=Task.STATUS_DONE,
            archived_at__isnull=True,
        )
        .values("cycle_id")
        .annotate(count=Count("id"), points=Sum("size"))
    )
    by_id = {r["cycle_id"]: r for r in rows}
    out = []
    for cycle in cycles:
        row = by_id.get(cycle.id, {})
        out.append(
            {
                "label": str(cycle.display_name),
                "count": row.get("count") or 0,
                "points": row.get("points") or 0,
            },
        )
    return out


# How many days before a cycle's end to fire the "ending soon" notice.
# 1 = the day before the last day (reads as "ends tomorrow").
CYCLE_ENDING_SOON_DAYS = 1


def _cycle_open_task_count(cycle) -> int:
    """Count the cycle's still-open (unfinished, non-archived) tasks."""
    from apps.tasks.models import Task

    return cycle.tasks.filter(
        status__in=(Task.STATUS_TODO, Task.STATUS_IN_PROGRESS, Task.STATUS_IN_REVIEW),
        archived_at__isnull=True,
    ).count()


def _cycle_recipient_ids(workspace) -> list:
    """Workspace member user ids — the audience for cycle notifications."""
    from apps.workspaces.models import WorkspaceMember

    return list(
        WorkspaceMember.objects.filter(workspace=workspace).values_list("user_id", flat=True),
    )


def notify_cycle_started(cycle) -> int:
    """Fan out a "cycle started" notification to every workspace member.

    System notification (no actor). Idempotent at the call site via
    ``Cycle.start_notified_at`` (the management command stamps it). Returns
    the number of notifications created.

    Args:
        cycle: The :class:`~apps.cycles.models.Cycle` that just became active.

    Returns:
        Count of notifications created.
    """
    from django.utils.translation import gettext as _

    from apps.notifications.services import notify

    title = _("%(label)s started") % {"label": cycle.display_name}
    preview = _("Runs %(start)s – %(end)s") % {
        "start": cycle.start_date.strftime("%b %-d"),
        "end": cycle.end_date.strftime("%b %-d"),
    }
    payload = {"title": str(title), "event": "started", "cycle_number": cycle.number}
    created = 0
    for user_id in _cycle_recipient_ids(cycle.workspace):
        if notify(
            recipient_id=user_id,
            actor=None,
            kind="cycle",
            workspace_id=cycle.workspace_id,
            preview=str(preview),
            payload=payload,
        ):
            created += 1
    return created


def notify_cycle_ending(cycle, today: datetime.date | None = None) -> int:
    """Fan out a "cycle ending soon" notification to every workspace member.

    Phrases the deadline relative to ``today`` (ends today / tomorrow / in
    N days) and previews how many tasks are still open. System
    notification; idempotent via ``Cycle.end_notified_at``.

    Args:
        cycle: The active :class:`~apps.cycles.models.Cycle`.
        today: Reference date; defaults to local today.

    Returns:
        Count of notifications created.
    """
    from django.utils.translation import gettext as _
    from django.utils.translation import ngettext

    from apps.notifications.services import notify

    today = today or timezone.localdate()
    days_left = (cycle.end_date - today).days
    if days_left <= 0:
        when = _("ends today")
    elif days_left == 1:
        when = _("ends tomorrow")
    else:
        when = _("ends in %(days)s days") % {"days": days_left}
    title = _("%(label)s %(when)s") % {"label": cycle.display_name, "when": when}
    open_count = _cycle_open_task_count(cycle)
    if open_count:
        preview = ngettext(
            "%(n)s task still open",
            "%(n)s tasks still open",
            open_count,
        ) % {"n": open_count}
    else:
        preview = _("Everything's done — nice.")
    payload = {"title": str(title), "event": "ending", "cycle_number": cycle.number, "open": open_count}
    created = 0
    for user_id in _cycle_recipient_ids(cycle.workspace):
        if notify(
            recipient_id=user_id,
            actor=None,
            kind="cycle",
            workspace_id=cycle.workspace_id,
            preview=str(preview),
            payload=payload,
        ):
            created += 1
    return created


def current_cycle(workspace, today: datetime.date | None = None):
    """Return the workspace's active cycle without materializing rows.

    Read-only lookup for surfaces that should not trigger creation (the
    cadence config is assumed already materialized by an earlier
    :func:`ensure_cycles`). Returns ``None`` when cadence is disabled or
    no active cycle exists yet.
    """
    from .models import Cycle

    return workspace.cycles.filter(status=Cycle.ACTIVE).order_by("-start_date").first()
