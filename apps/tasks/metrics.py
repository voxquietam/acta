"""Flow metrics computed from the activity log.

Scrumban needs measurable flow, not just a board. Every status change is
already an ``ActivityLog`` row (``task.status_changed`` with a
``{from, to}`` payload + ``created_at`` timestamp), so cycle time, lead
time and throughput are a replay of that log — no extra bookkeeping at
write time. Even the Cumulative Flow Diagram (daily WIP per status) is
reconstructed from the log rather than a snapshot table: replaying gives
full history immediately, whereas a snapshot populated by a daily cron
would start empty. Cheap enough for the insights page (bounded by a
trailing window, not a hot path), so no snapshot table / cron is needed.
See docs/decisions/0026-scrumban-metrics.md.

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


def _task_status_events(project):
    """Return ``{task_id: [(date, from, to), …]}`` of status changes.

    One ordered pass over the project's ``task.status_changed`` rows.
    Dates are local. Shared by the CFD reconstruction and the
    time-in-status accumulation.
    """
    events: dict[int, list] = defaultdict(list)
    rows = (
        ActivityLog.objects.filter(
            project=project,
            target_type=ActivityLog.TARGET_TASK,
            event_type="task.status_changed",
        )
        .order_by("created_at")
        .values_list("target_id", "payload", "created_at")
    )
    for task_id, payload, occurred in rows:
        payload = payload or {}
        events[task_id].append((occurred, payload.get("from"), payload.get("to")))
    return events


def compute_cfd(project, *, today: datetime.date | None = None, weeks: int = 8) -> dict[str, Any]:
    """Reconstruct a Cumulative Flow Diagram from the activity log.

    For each day in the trailing window, counts how many tasks sat in
    each workflow status as of end-of-day — rebuilt by replaying each
    task's status changes (no daily snapshot table needed, so history is
    available immediately). The terminal ``cancelled`` status is left out
    of the bands. ``O(tasks × days)`` — fine for the insights page.

    Returns:
        ``{labels, statuses, series}`` where ``series[status]`` is a
        per-day count list aligned with ``labels``.
    """
    today = today or timezone.localdate()
    start = today - datetime.timedelta(weeks=weeks)
    statuses = list(Task.KANBAN_STATUS_VALUES)
    days = [start + datetime.timedelta(days=i) for i in range((today - start).days + 1)]

    events = _task_status_events(project)
    tasks = Task.objects.filter(project=project).values_list("id", "status", "created_at")

    series = {s: [0] * len(days) for s in statuses}
    for task_id, current_status, created_at in tasks:
        created_date = timezone.localtime(created_at).date()
        evs = [(timezone.localtime(ts).date(), efrom, eto) for ts, efrom, eto in events.get(task_id, [])]
        # Status at creation = the ``from`` of the first logged change,
        # else the task's current status (never changed).
        initial = evs[0][1] if evs else current_status
        for di, day in enumerate(days):
            if day < created_date:
                continue
            status = initial
            for edate, _efrom, eto in evs:
                if edate <= day:
                    status = eto
                else:
                    break
            if status in series:
                series[status][di] += 1

    return {
        "labels": [d.strftime("%b %d") for d in days],
        "statuses": statuses,
        "series": series,
    }


def compute_bottlenecks(project, *, today: datetime.date | None = None, weeks: int = 8) -> dict[str, Any]:
    """Diagnose where work piles up: time-in-status, WIP, reopen rate.

    * **time_in_status** — average hours a task spends in each status
      before leaving it (closed segments only); the high bars are the
      bottlenecks.
    * **wip** — current task count per active status (live).
    * **reopen_rate** — share of completions that later went back from
      ``done`` (a rework signal), over the trailing window.

    Returns a dict consumed by the insights template + its charts.
    """
    # ``today`` is naive (date) but only used for chart labels; the comparison
    # at line 271 is ``ts < window_start_dt`` (datetime ← datetime). The mix is
    # intentional and the two types never meet. Wave 2 C1 §F2.
    today = today or timezone.localdate()
    window_start_dt = timezone.now() - datetime.timedelta(weeks=weeks)
    events = _task_status_events(project)
    tasks = dict(Task.objects.filter(project=project).values_list("id", "status"))

    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    for task_id, current_status in tasks.items():
        evs = events.get(task_id, [])
        if not evs:
            continue
        # Each change closes the segment of its ``from`` status: the time
        # between the previous transition (or — for the first — we lack a
        # creation-status timestamp, so start from the first event) and
        # this one.
        for (prev_ts, _pf, _pt), (ts, efrom, _et) in zip(evs, evs[1:]):
            if efrom:
                totals[efrom] += (ts - prev_ts).total_seconds() / 3600.0
                counts[efrom] += 1

    time_in_status = {s: round(totals[s] / counts[s], 1) if counts.get(s) else 0.0 for s in Task.KANBAN_STATUS_VALUES}

    wip = {s: sum(1 for st in tasks.values() if st == s) for s in Task.KANBAN_STATUS_VALUES if s != Task.STATUS_DONE}

    # Reopen rate = share of tasks COMPLETED in the window that later left
    # done (a rework signal). Counted per distinct task — not per
    # transition — so the rate is a true proportion bounded at 100%.
    # (Counting raw done→away events let reopens of pre-window completions
    # push it past 100%, which read as a broken metric.)
    completed_ids: set[int] = set()
    reopened_ids: set[int] = set()
    for task_id, evs in events.items():
        completed_in_window = False
        for ts, efrom, eto in evs:
            if ts < window_start_dt:
                continue
            if eto == Task.STATUS_DONE:
                completed_ids.add(task_id)
                completed_in_window = True
            elif efrom == Task.STATUS_DONE and completed_in_window:
                reopened_ids.add(task_id)
    reopened = len(reopened_ids)
    completions = len(completed_ids)
    reopen_rate = round(reopened / completions * 100, 1) if completions else 0.0

    return {
        "time_in_status": time_in_status,
        "wip": wip,
        "reopen_rate": reopen_rate,
        "reopened": reopened,
    }
