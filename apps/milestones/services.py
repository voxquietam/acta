"""Derivations the milestone pages are built from.

A milestone stores four facts: a date, a scope, a goal, and whether a
person has closed it. Everything a page says about it beyond those —
progress, state, the per-project and per-epic slices, what is at risk,
the burndown — is read off the work attached to it. Those readings live
here rather than in a view, so the pages, the MCP tools and the admin
cannot drift into giving different answers about the same date.

See docs/decisions/0037-milestones.md.
"""

from collections import defaultdict
import datetime

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.milestones import forecast
from apps.milestones.models import Milestone
from apps.tasks.models import Task, counted_q

#: Statuses of attached work, in the order the detail page stacks them:
#: what is moving first, what has not been picked up last.
ATTACHED_STATUS_ORDER = [
    Task.STATUS_IN_PROGRESS,
    Task.STATUS_IN_REVIEW,
    Task.STATUS_READY,
    Task.STATUS_TODO,
    Task.STATUS_PLANNED,
]

#: How many milestone cards the project Overview draws before it stops.
#: The grid fits four across the panel; a fifth would wrap into a second
#: row that says nothing the Plan tab does not say better.
OVERVIEW_CARD_LIMIT = 4

#: How far past the target date the chart still draws. The 85th
#: percentile sits further out than the old mean projection did, so the
#: window is wider — but a chart stretched to a date a year away is a flat
#: line and a wasted axis, and the sentence above it names the real date
#: either way.
PROJECTION_HORIZON_DAYS = 90

STATE_LABELS = {
    Milestone.STATE_OPEN: _("open"),
    Milestone.STATE_TODAY: _("open"),
    Milestone.STATE_OVERDUE: _("open"),
    Milestone.STATE_COMPLETE: _("ready to close"),
    Milestone.STATE_CLOSED: _("closed"),
}


def at_risk(milestone, today=None) -> list[tuple]:
    """Return the counted, unfinished work that will not make the date.

    Two sides, and the second is the one people forget: a task whose own
    due date falls after the milestone's date contradicts the plan, and
    once the date has passed every still-open task is late whatever its
    due date says. A milestone with only the first half looks healthy the
    day after it is missed.

    Args:
        milestone: The milestone to examine.
        today: Reference date; defaults to the local current date.

    Returns:
        A list of ``(task, days_over, why)`` tuples, worst first. The
        reason is a plain string, not a lazy one: the MCP tools put it
        straight into a JSON payload.
    """
    today = today or timezone.localdate()
    open_work = (
        milestone.counted_tasks().exclude(status=Task.STATUS_DONE).select_related("project").order_by("due_date")
    )
    past = milestone.target_date < today
    rows = []
    for task in open_work:
        if task.due_date and task.due_date > milestone.target_date:
            rows.append((task, (task.due_date - milestone.target_date).days, str(_("due after the milestone"))))
        elif past:
            rows.append((task, (today - milestone.target_date).days, str(_("still open"))))
    rows.sort(key=lambda row: -row[1])
    return rows


def countdown(milestone, state, today=None) -> str:
    """Return the one line that says where this date stands.

    The date is the milestone, so every surface that shows a milestone
    shows this line next to it. "All done" outranks the calendar: a
    finished milestone whose date has passed should read as finished, not
    as late.

    Args:
        milestone: The milestone to describe.
        state: Its state, from :meth:`Milestone.state`.
        today: Reference date; defaults to the local current date.

    Returns:
        A translated one-line reading of the date.
    """
    today = today or timezone.localdate()
    if state == Milestone.STATE_CLOSED:
        return _("closed on %(date)s") % {"date": date_label(milestone.closed_at or milestone.target_date)}
    if state == Milestone.STATE_COMPLETE:
        return _("all done · ready to close")
    days = (milestone.target_date - today).days
    if days < 0:
        return ngettext(
            "overdue by %(count)d day",
            "overdue by %(count)d days",
            -days,
        ) % {"count": -days}
    if days == 0:
        return _("due today")
    if days == 1:
        return _("due tomorrow")
    return ngettext(
        "due in %(count)d day",
        "due in %(count)d days",
        days,
    ) % {"count": days}


def date_label(value) -> str:
    """Format a date (or datetime) the short way every milestone surface uses.

    Args:
        value: A ``date`` or ``datetime``.

    Returns:
        The date as ``Mon D``, e.g. ``Oct 30``.
    """
    if isinstance(value, datetime.datetime):
        value = timezone.localtime(value).date() if timezone.is_aware(value) else value.date()
    return f"{value:%b} {value.day}"


def _counted_cells(milestone_ids: list[int]) -> list[tuple]:
    """Return one row per counted task of the given milestones.

    One query for a whole page: the list needs a per-project slice and an
    at-risk count for every row, and walking either per milestone is an
    N+1 across the workspace.

    Args:
        milestone_ids: Milestones to read the attached work of.

    Returns:
        ``(milestone_id, project_id, status, due_date)`` tuples.
    """
    if not milestone_ids:
        return []
    return list(
        Task.objects.filter(
            counted_q(),
            milestone_id__in=milestone_ids,
        ).values_list(
            "milestone_id",
            "project_id",
            "status",
            "due_date",
        ),
    )


def progress_by_milestone(milestone_ids: list[int]) -> dict[int, tuple[int, int]]:
    """Return ``{milestone_id: (done, total)}`` across each milestone's whole scope.

    The Plan tab cuts its rows from one project's work, so the rollup it
    computes itself is the local part. On a date several projects share
    that part is not the commitment: "3/4 here" says nothing about
    whether the milestone is nearly met or barely started. This is the
    other number, read in one query for the whole page.

    Args:
        milestone_ids: Milestones to read the attached work of.

    Returns:
        Done and total counted tasks per milestone; a milestone with no
        counted work is absent rather than ``(0, 0)``.
    """
    counts: dict[int, list[int]] = {}
    for milestone_id, _project_id, status, _due_date in _counted_cells(milestone_ids):
        cell = counts.setdefault(milestone_id, [0, 0])
        cell[1] += 1
        if status == Task.STATUS_DONE:
            cell[0] += 1
    return {milestone_id: (done, total) for milestone_id, (done, total) in counts.items()}


def workspace_rows(workspace, today=None) -> list[dict]:
    """Return a row per milestone of the workspace, soonest date first.

    Each row carries what the list draws: state and countdown, progress
    over counted work, the projects in scope with their own slice, how
    much is at risk, and whether another milestone shares the day.

    Args:
        workspace: The workspace whose milestones to list.
        today: Reference date; defaults to the local current date.

    Returns:
        A list of row dicts, each holding its ``milestone``.
    """
    today = today or timezone.localdate()
    milestones = list(
        workspace.milestones.select_related("owner")
        .prefetch_related("projects")
        .order_by(
            "target_date",
            "id",
        ),
    )
    cells = _counted_cells([m.id for m in milestones])
    by_milestone: dict[int, list[tuple]] = defaultdict(list)
    for milestone_id, project_id, status, due_date in cells:
        by_milestone[milestone_id].append((project_id, status, due_date))
    same_day: dict[datetime.date, list] = defaultdict(list)
    for milestone in milestones:
        same_day[milestone.target_date].append(milestone)
    return [_row(milestone, by_milestone[milestone.id], same_day, today) for milestone in milestones]


def _row(milestone, cells: list[tuple], same_day: dict, today) -> dict:
    """Build one list row from the milestone's counted work.

    Args:
        milestone: The milestone, with ``projects`` prefetched.
        cells: Its ``(project_id, status, due_date)`` tuples.
        same_day: Every milestone of the workspace, keyed by target date.
        today: Reference date.

    Returns:
        The row dict the list template renders.
    """
    done = sum(1 for _project_id, status, _due in cells if status == Task.STATUS_DONE)
    total = len(cells)
    state = _state_from_cells(milestone, done, total, today)
    past = milestone.target_date < today
    risk = _risk_count(milestone, cells, past)
    per_project: dict[int, dict] = {}
    for project in milestone.projects.all():
        per_project[project.id] = {
            "project": project,
            "done": 0,
            "total": 0,
            "risk": 0,
        }
    for project_id, status, due_date in cells:
        slice_row = per_project.get(project_id)
        if slice_row is None:
            continue
        slice_row["total"] += 1
        if status == Task.STATUS_DONE:
            slice_row["done"] += 1
        elif _is_at_risk(milestone, due_date, past):
            slice_row["risk"] += 1
    return {
        "milestone": milestone,
        "state": state,
        "state_label": STATE_LABELS[state],
        "countdown": countdown(milestone, state, today),
        "done": done,
        "total": total,
        "open": total - done,
        "percent": round(done / total * 100) if total else 0,
        "risk": risk,
        "is_past": past,
        "projects": sorted(per_project.values(), key=lambda row: row["project"].slug_prefix),
        "same_day": [other.name for other in same_day[milestone.target_date] if other.id != milestone.id],
    }


def _state_from_cells(milestone, done: int, total: int, today) -> str:
    """Return the milestone's state without re-counting its work.

    :meth:`Milestone.state` walks the members, which is one query per
    row on a list page. The rule is the same one, read off counts the
    caller already has.

    Args:
        milestone: The milestone.
        done: Counted work that is done.
        total: Counted work in total.
        today: Reference date.

    Returns:
        One of the ``Milestone.STATE_*`` values.
    """
    if milestone.is_closed:
        return Milestone.STATE_CLOSED
    if total and done == total:
        return Milestone.STATE_COMPLETE
    if milestone.target_date < today:
        return Milestone.STATE_OVERDUE
    if milestone.target_date == today:
        return Milestone.STATE_TODAY
    return Milestone.STATE_OPEN


def _is_at_risk(milestone, due_date, past: bool) -> bool:
    """Return whether one unfinished task is at risk against the date.

    Args:
        milestone: The milestone the task is attached to.
        due_date: The task's own due date, or ``None``.
        past: Whether the milestone's date has already passed.

    Returns:
        ``True`` when the task is due after the date, or still open once
        the date has gone.
    """
    return past or (due_date is not None and due_date > milestone.target_date)


def _risk_count(milestone, cells: list[tuple], past: bool) -> int:
    """Count the at-risk work in a milestone's cells.

    Args:
        milestone: The milestone.
        cells: Its ``(project_id, status, due_date)`` tuples.
        past: Whether the date has passed.

    Returns:
        How many counted, unfinished tasks are at risk.
    """
    return sum(
        1
        for _project_id, status, due_date in cells
        if status != Task.STATUS_DONE and _is_at_risk(milestone, due_date, past)
    )


def group_rows(rows: list[dict], today=None) -> list[dict]:
    """Bucket list rows into "Past" plus one group per upcoming month.

    By month and not by quarter: most months hold one to three dates, so
    the order stays readable, where a quarter stacks six into one bucket
    and says nothing about which comes next. Past dates fold away, but
    the group header says what is still wrong inside it — an overdue
    milestone that is collapsed out of sight is how a missed date goes
    unnoticed.

    Args:
        rows: Rows from :func:`workspace_rows`, soonest date first.
        today: Reference date; defaults to the local current date.

    Returns:
        A list of group dicts with ``key``, ``label``, ``rows``,
        ``collapsed`` and a ``summary`` list of ``{text, tone}``.
    """
    today = today or timezone.localdate()
    past = [row for row in rows if row["is_past"]]
    upcoming = [row for row in rows if not row["is_past"]]
    groups = []
    if past:
        groups.append(
            {
                "key": "past",
                "label": _("Past"),
                "rows": past,
                "count": len(past),
                "collapsed": True,
                "summary": _past_summary(past),
            },
        )
    months: dict[tuple[int, int], dict] = {}
    for row in upcoming:
        target = row["milestone"].target_date
        key = (target.year, target.month)
        group = months.get(key)
        if group is None:
            group = months[key] = {
                "key": f"{target.year}-{target.month:02d}",
                "label": f"{target:%B} {target.year}",
                "rows": [],
                "collapsed": False,
                "summary": [],
            }
        group["rows"].append(row)
    for group in months.values():
        group["count"] = len(group["rows"])
        risk = sum(row["risk"] for row in group["rows"])
        if risk:
            group["summary"] = [
                {
                    "text": ngettext("%(count)d at risk", "%(count)d at risk", risk) % {"count": risk},
                    "tone": "risk",
                },
            ]
        groups.append(group)
    return groups


def _past_summary(rows: list[dict]) -> list[dict]:
    """Say what is still wrong behind a collapsed "Past" header.

    Args:
        rows: The past rows.

    Returns:
        A list of ``{text, tone}`` chips, or a single muted "all closed".
    """
    summary = []
    for row in rows:
        if row["state"] == Milestone.STATE_OVERDUE:
            summary.append(
                {
                    "text": _("%(name)s %(countdown)s, %(open)d open")
                    % {
                        "name": row["milestone"].name,
                        "countdown": row["countdown"],
                        "open": row["open"],
                    },
                    "tone": "risk",
                },
            )
        elif row["state"] == Milestone.STATE_COMPLETE:
            summary.append(
                {
                    "text": _("%(name)s all done, not closed") % {"name": row["milestone"].name},
                    "tone": "done",
                },
            )
    if not summary:
        summary.append(
            {
                "text": _("all closed"),
                "tone": "muted",
            },
        )
    return summary


def detail_context(milestone, today=None) -> dict:
    """Build everything the milestone page shows about one date.

    Args:
        milestone: The milestone, with ``projects`` prefetched.
        today: Reference date; defaults to the local current date.

    Returns:
        A context dict: state and countdown, progress, the at-risk list,
        the per-project and per-epic slices, the attached work in groups,
        the counting note, and the burndown series.
    """
    today = today or timezone.localdate()
    attached = list(
        milestone.tasks.select_related("project", "assignee", "epic").order_by(
            "project__slug_prefix",
            "number",
        ),
    )
    counted = [task for task in attached if _counts(task)]
    done = [task for task in counted if task.status == Task.STATUS_DONE]
    open_work = [task for task in counted if task.status != Task.STATUS_DONE]
    past = milestone.target_date < today
    state = _state_from_cells(milestone, len(done), len(counted), today)
    risk_rows = [
        {
            "task": task,
            "over": (
                (task.due_date - milestone.target_date).days
                if task.due_date and task.due_date > milestone.target_date
                else (today - milestone.target_date).days
            ),
            "why": (
                _("due %(date)s") % {"date": date_label(task.due_date)}
                if task.due_date and task.due_date > milestone.target_date
                else _("still open")
            ),
        }
        for task in open_work
        if _is_at_risk(milestone, task.due_date, past)
    ]
    risk_rows.sort(key=lambda row: -row["over"])
    return {
        "milestone": milestone,
        "state": state,
        "state_label": STATE_LABELS[state],
        "countdown": countdown(milestone, state, today),
        "done": len(done),
        "total": len(counted),
        "open": len(open_work),
        "percent": round(len(done) / len(counted) * 100) if counted else 0,
        "attached_count": len(attached),
        "risk_rows": risk_rows,
        "risk": len(risk_rows),
        "by_project": _project_slices(milestone, counted, past),
        "by_epic": _epic_slices(milestone, counted, past),
        "groups": _attached_groups(milestone, attached, counted, past, today),
        "note": _counting_note(attached),
        "burndown": burndown(milestone, today=today),
        "is_past": past,
    }


def _counts(task) -> bool:
    """Return whether one loaded task counts as work.

    The in-memory half of :func:`~apps.tasks.models.counted_q`, for a
    page that has the attached tasks in hand and must not run a second
    query to filter them.

    Args:
        task: The task to judge.

    Returns:
        ``True`` when the task counts towards progress.
    """
    if task.status == Task.STATUS_CANCELLED:
        return False
    return task.archived_at is None or task.status == Task.STATUS_DONE


def _slice_row(label, counted: list, milestone, past: bool, **extra) -> dict:
    """Summarise one slice of a milestone's counted work.

    Args:
        label: What the slice is called on screen.
        counted: The counted tasks in this slice.
        milestone: The milestone the slice belongs to.
        past: Whether the date has passed.
        **extra: Further keys to carry into the row.

    Returns:
        A row dict with ``done``, ``total``, ``percent`` and ``risk``.
    """
    done = sum(1 for task in counted if task.status == Task.STATUS_DONE)
    total = len(counted)
    risk = sum(1 for task in counted if task.status != Task.STATUS_DONE and _is_at_risk(milestone, task.due_date, past))
    return {
        "label": label,
        "done": done,
        "total": total,
        "percent": round(done / total * 100) if total else 0,
        "risk": risk,
        **extra,
    }


def _project_slices(milestone, counted: list, past: bool) -> list[dict]:
    """Return one row per project in scope, including the empty ones.

    A project in scope with no work attached is the interesting row on
    this table, not a row to hide: it says the commitment has not reached
    that team yet.

    Args:
        milestone: The milestone.
        counted: Its counted tasks.
        past: Whether the date has passed.

    Returns:
        Slice rows, by project slug prefix.
    """
    rows = []
    for project in sorted(milestone.projects.all(), key=lambda p: p.slug_prefix):
        tasks = [task for task in counted if task.project_id == project.id]
        rows.append(_slice_row(project.name, tasks, milestone, past, project=project))
    return rows


def _epic_slices(milestone, counted: list, past: bool) -> list[dict]:
    """Return one row per epic the counted work sits in, biggest first.

    An epic whose work spans three milestones is normal, so this reads
    the epics off the tasks rather than off a field the epic does not
    have. Work in no epic is a row of its own, last.

    Args:
        milestone: The milestone.
        counted: Its counted tasks.
        past: Whether the date has passed.

    Returns:
        Slice rows, with ``epic`` set to ``None`` on the "no epic" row.
    """
    buckets: dict[int | None, list] = defaultdict(list)
    epics: dict[int | None, object] = {}
    for task in counted:
        buckets[task.epic_id].append(task)
        epics[task.epic_id] = task.epic
    rows = []
    for epic_id, tasks in buckets.items():
        epic = epics[epic_id]
        rows.append(
            _slice_row(
                epic.title if epic is not None else _("No epic"),
                tasks,
                milestone,
                past,
                epic=epic,
            ),
        )
    rows.sort(key=lambda row: (row["epic"] is None, -row["total"]))
    return rows


def _attached_groups(milestone, attached: list, counted: list, past: bool, today) -> list[dict]:
    """Group the attached work the way the page stacks it.

    One group per live status in the order of :data:`ATTACHED_STATUS_ORDER`,
    then the done work, then everything the counting rule leaves out —
    shown, and labelled with why, because a page whose numbers say
    ``4/7`` and whose list holds nine rows has to account for the other
    two.

    Args:
        milestone: The milestone.
        attached: Every task attached to it.
        counted: The subset that counts.
        past: Whether the date has passed.
        today: Reference date.

    Returns:
        Group dicts with ``label``, ``status``, ``tasks`` and ``count``.
    """
    counted_ids = {task.id for task in counted}
    groups = []
    for status in ATTACHED_STATUS_ORDER:
        tasks = sorted(
            (task for task in counted if task.status == status),
            key=lambda task: (task.due_date is None, task.due_date),
        )
        if tasks:
            groups.append(
                {
                    "status": status,
                    "label": Task.STATUS_LABELS[status],
                    "tasks": [_task_row(milestone, task, past, today) for task in tasks],
                    "count": len(tasks),
                    "note": "",
                },
            )
    done_tasks = [task for task in counted if task.status == Task.STATUS_DONE]
    if done_tasks:
        groups.append(
            {
                "status": Task.STATUS_DONE,
                "label": Task.STATUS_LABELS[Task.STATUS_DONE],
                "tasks": [_task_row(milestone, task, past, today) for task in done_tasks],
                "count": len(done_tasks),
                "note": "",
            },
        )
    excluded = [task for task in attached if task.id not in counted_ids]
    if excluded:
        groups.append(
            {
                "status": Task.STATUS_CANCELLED,
                "label": _("Not counted or archived"),
                "tasks": [_task_row(milestone, task, past, today) for task in excluded],
                "count": len(excluded),
                "note": _("shown so the numbers add up"),
            },
        )
    return groups


def _task_row(milestone, task, past: bool, today) -> dict:
    """Describe one attached task against the milestone's date.

    Args:
        milestone: The milestone.
        task: The attached task.
        past: Whether the date has passed.
        today: Reference date.

    Returns:
        A row dict with the task, how late it runs, and why it is or is
        not counted.
    """
    over = 0
    if _counts(task) and task.status != Task.STATUS_DONE and task.due_date:
        over = (task.due_date - milestone.target_date).days
    if task.status == Task.STATUS_CANCELLED:
        why = _("cancelled · not counted")
    elif task.archived_at is not None:
        why = _("archived done · counted") if task.status == Task.STATUS_DONE else _("archived unfinished · dropped")
    elif past and task.status != Task.STATUS_DONE:
        why = _("still open")
    else:
        why = ""
    return {
        "task": task,
        "over": over,
        "why": why,
    }


def _counting_note(attached: list) -> str:
    """Say which attached work the counting rule treated specially.

    Args:
        attached: Every task attached to the milestone.

    Returns:
        A one-line note, empty when every attached task simply counts.
    """
    archived_done = sum(1 for task in attached if task.archived_at is not None and task.status == Task.STATUS_DONE)
    archived_open = sum(
        1
        for task in attached
        if task.archived_at is not None and task.status not in (Task.STATUS_DONE, Task.STATUS_CANCELLED)
    )
    cancelled = sum(1 for task in attached if task.status == Task.STATUS_CANCELLED)
    parts = []
    if archived_done:
        parts.append(_("%(count)d archived done counted") % {"count": archived_done})
    if archived_open:
        parts.append(_("%(count)d archived unfinished dropped") % {"count": archived_open})
    if cancelled:
        parts.append(_("%(count)d cancelled not counted") % {"count": cancelled})
    return " · ".join(parts)


def _event_day(created) -> datetime.date:
    """Return the local day an activity event was written on.

    Args:
        created: The event's ``created_at``.

    Returns:
        The local date.
    """
    return timezone.localtime(created).date() if timezone.is_aware(created) else created.date()


def _membership_history(milestone, task_ids: list[int]) -> dict[int, list[list]]:
    """Replay when each task joined and left this milestone.

    Membership is stored on the task, so its history is the
    ``task.milestone_changed`` events — the same activity-log replay the
    cycle burndown uses, with no snapshot table (ADR 0026). A task that
    was attached without an event (created with the milestone already
    set) counts from the day the milestone or the task appeared,
    whichever is later.

    Args:
        milestone: The milestone to trace.
        task_ids: Tasks currently attached to it.

    Returns:
        ``{task_id: [[joined, left_or_None], ...]}``.
    """
    from apps.activity.models import ActivityLog

    events = (
        ActivityLog.objects.filter(
            Q(payload__to_milestone_id=milestone.id) | Q(payload__from_milestone_id=milestone.id),
            target_type=ActivityLog.TARGET_TASK,
            event_type="task.milestone_changed",
        )
        .order_by("created_at")
        .values_list("target_id", "payload", "created_at")
    )
    history: dict[int, list[list]] = defaultdict(list)
    for task_id, payload, created in events:
        day = _event_day(created)
        payload = payload or {}
        if payload.get("to_milestone_id") == milestone.id:
            history[task_id].append([day, None])
        elif payload.get("from_milestone_id") == milestone.id:
            spans = history[task_id]
            if spans and spans[-1][1] is None:
                spans[-1][1] = day
            else:
                spans.append([day, day])
    for task_id in task_ids:
        if task_id not in history:
            history[task_id] = []
    return history


def _done_days(task_ids: list[int]) -> dict[int, datetime.date]:
    """Map each task to the day it last became done, by activity replay.

    Args:
        task_ids: Tasks to resolve done-days for.

    Returns:
        ``{task_id: date}`` for tasks currently in the done state.
    """
    from apps.activity.models import ActivityLog

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
        if (payload or {}).get("to") == Task.STATUS_DONE:
            done[task_id] = _event_day(created)
        else:
            done.pop(task_id, None)
    return done


def burndown(milestone, today=None) -> dict | None:
    """Return the burndown series for a milestone, ready for Chart.js.

    Three lines and a fan, which is the shape GitLab and Jira release
    reports settled on with the projection made honest: how much work
    remains, how much work the milestone holds at all (so a scope change
    reads as a scope change and not as a stall), and the straight line to
    zero on the date. Where those reports draw one projection, this draws
    two — the median and the 85th percentile of a replay of the team's
    own days (ADR 0038) — because the gap between them is the only
    truthful thing to say about a date.

    Drawn from the activity log, never from due dates: a due date is a
    plan, and a burndown drawn from plans cannot be wrong.

    Args:
        milestone: The milestone to chart.
        today: Reference date; defaults to the local current date.

    Returns:
        A dict of parallel series plus ``forecast`` and the ``reading``
        the page branches its tone on, or ``None`` when nothing counted
        is attached — zero of zero is not a chart.
    """
    today = today or timezone.localdate()
    now_ids = list(
        milestone.tasks.filter(counted_q()).values_list("id", flat=True),
    )
    if not now_ids:
        return None
    history = _membership_history(milestone, now_ids)
    ever_ids = list(history.keys())
    statuses = dict(
        Task.objects.filter(
            counted_q(),
            id__in=ever_ids,
        ).values_list("id", "status"),
    )
    created_days = dict(
        Task.objects.filter(id__in=ever_ids).values_list("id", "created_at"),
    )
    ever_ids = [task_id for task_id in ever_ids if task_id in statuses]
    done_days = _done_days(ever_ids)
    joined_fallback = max(
        _event_day(milestone.created_at),
        min((_event_day(created_days[task_id]) for task_id in ever_ids), default=today),
    )
    for task_id in ever_ids:
        if not history[task_id]:
            history[task_id] = [
                [
                    max(joined_fallback, _event_day(created_days[task_id])),
                    None,
                ],
            ]
    start = min(history[task_id][0][0] for task_id in ever_ids)
    total = len(now_ids)
    open_now = sum(1 for task_id in now_ids if statuses.get(task_id) != Task.STATUS_DONE)
    # The whole answer about "when", from the days the team actually had
    # (ADR 0038). Seeded on the milestone so a refresh does not reshuffle
    # the numbers under the reader.
    outlook = forecast.forecast(
        closes=forecast.daily_closes(done_days, today),
        remaining=open_now,
        history_days=(today - start).days,
        today=today,
        target=milestone.target_date,
        seed=milestone.id,
    )
    reading = forecast.reading(outlook)
    if outlook.get("passed"):
        # The page says how long ago, and only the caller knows "ago".
        outlook["days_over"] = (today - milestone.target_date).days
    far = outlook.get("p85")
    axis_end = max(
        milestone.target_date,
        today,
        min(far, milestone.target_date + datetime.timedelta(days=PROJECTION_HORIZON_DAYS)) if far else today,
    )
    labels = []
    scope = []
    remaining = []
    day = start
    while day <= axis_end:
        labels.append(day.isoformat())
        in_scope = [task_id for task_id in ever_ids if _in_milestone(history[task_id], day)]
        if day <= today:
            scope.append(len(in_scope))
            remaining.append(
                sum(1 for task_id in in_scope if not (task_id in done_days and done_days[task_id] <= day)),
            )
        else:
            scope.append(None)
            remaining.append(None)
        day += datetime.timedelta(days=1)
    span_days = max(1, (milestone.target_date - start).days)
    ideal = []
    for index, label in enumerate(labels):
        offset = (datetime.date.fromisoformat(label) - start).days
        ideal.append(round(total * max(0, span_days - offset) / span_days, 2) if offset <= span_days else 0)
    today_index = (today - start).days
    p50_line = _straight_down(labels, today_index, open_now, outlook.get("p50"), start)
    p85_line = _straight_down(labels, today_index, open_now, outlook.get("p85"), start)
    scope_moves = sum(
        1 for index in range(1, today_index + 1) if index < len(scope) and scope[index] != scope[index - 1]
    )
    return {
        "labels": labels,
        "scope": scope,
        "remaining": remaining,
        "ideal": ideal,
        "p50_line": p50_line,
        "p85_line": p85_line,
        "total": total,
        "open": open_now,
        "today_index": today_index,
        "target_index": (milestone.target_date - start).days,
        "forecast": outlook,
        "reading": reading,
        "scope_note": (
            ngettext(
                "scope moved on %(count)d day",
                "scope moved on %(count)d days",
                scope_moves,
            )
            % {"count": scope_moves}
            if scope_moves
            else _("no work joined or left")
        ),
    }


def _in_milestone(spans: list[list], day: datetime.date) -> bool:
    """Return whether a task sat in the milestone on a given day.

    Args:
        spans: The task's ``[joined, left_or_None]`` spans.
        day: The day to test.

    Returns:
        ``True`` when one span covers that day.
    """
    return any(joined <= day and (left is None or left > day) for joined, left in spans)


def _straight_down(labels, today_index: int, open_now: int, landing, start) -> list:
    """Draw one projection line from today's remainder to zero on a date.

    A percentile is a date, not a slope, so the line between here and
    there is the plainest thing that can connect them. Two of these — the
    median and the 85th — are what replace the single "at this pace"
    line, and the gap between them is the uncertainty made visible.

    Args:
        labels: The chart's ISO day labels.
        today_index: Where today sits in them.
        open_now: Counted work still unfinished.
        landing: The day the line reaches zero, or ``None`` when there is
            no forecast to draw.
        start: The chart's first day.

    Returns:
        A series as long as ``labels``, ``None`` outside the line.
    """
    line = [None] * len(labels)
    if not landing or not open_now or not 0 <= today_index < len(labels):
        return line
    end_index = min((landing - start).days, len(labels) - 1)
    line[today_index] = open_now
    span = end_index - today_index
    if span <= 0:
        return line
    for index in range(today_index + 1, end_index + 1):
        line[index] = round(open_now * (1 - (index - today_index) / span), 2)
    return line


def fill_candidates(milestone, query: str = "", limit: int = 12) -> list[dict]:
    """Return in-scope work this milestone could still take, soonest first.

    The reverse of the task rail: standing on the date, pick the work
    that belongs to it. Only counted, unfinished work that is not already
    here, and only from projects the scope covers — a task may not join
    a milestone that does not cover its project.

    Work that already sits in another milestone is offered, and says so:
    attaching moves it, which is a decision worth making with the
    consequence in view.

    Args:
        milestone: The milestone being filled.
        query: Optional text to match against title and slug.
        limit: How many rows to return.

    Returns:
        Row dicts with ``task`` and the ``other`` milestone it would
        leave, if any.
    """
    rows = (
        Task.objects.filter(
            counted_q(),
            kind=Task.KIND_TASK,
            project__in=milestone.projects.all(),
        )
        .exclude(status=Task.STATUS_DONE)
        .exclude(milestone_id=milestone.pk)
        .select_related("project", "milestone", "assignee")
    )
    rows = _search(rows, query).order_by(
        "due_date",
        "project__slug_prefix",
        "number",
    )
    return [
        {
            "task": task,
            "other": task.milestone,
        }
        for task in rows[:limit]
    ]


def out_of_scope_matches(milestone, query: str, limit: int = 3) -> list[dict]:
    """Return matching work the scope does not reach, with the way in.

    Shown and refused rather than hidden: a search that silently drops
    the task someone is looking for reads as a bug, where a row saying
    "Web is not in this milestone's scope" names the fix — widen the
    scope, or leave the task where it is.

    Args:
        milestone: The milestone being filled.
        query: Text to match against title and slug; empty returns
            nothing, since "everything else in the workspace" is not an
            answer to a question nobody asked.
        limit: How many rows to return.

    Returns:
        Row dicts with ``task`` and its ``project``.
    """
    if not query.strip():
        return []
    rows = (
        Task.objects.filter(
            counted_q(),
            kind=Task.KIND_TASK,
            project__workspace_id=milestone.workspace_id,
        )
        .exclude(status=Task.STATUS_DONE)
        .exclude(project__in=milestone.projects.all())
        .select_related("project")
    )
    return [
        {
            "task": task,
            "project": task.project,
        }
        for task in _search(rows, query).order_by("project__slug_prefix", "number")[:limit]
    ]


def _search(queryset, query: str):
    """Narrow a task queryset by a free-text query over title and slug.

    Args:
        queryset: The tasks to narrow.
        query: What the person typed; blank leaves the queryset alone.

    Returns:
        The narrowed queryset.
    """
    query = query.strip()
    if not query:
        return queryset
    number = None
    tail = query.rsplit("-", 1)[-1]
    if tail.isdigit():
        number = int(tail)
    match = Q(title__icontains=query) | Q(project__slug_prefix__istartswith=query)
    if number is not None:
        match |= Q(number=number)
    return queryset.filter(match)


def membership_reports(milestone, today=None) -> dict:
    """Return the two reports that keep a milestone's membership honest.

    Both read off data already held, and neither ever joins a task to
    anything: they are questions, and a person answers them.

    *Near, not in* — work in scope, unfinished, due on or before the
    date, committed to no milestone. Forgotten, or deliberately out?

    *Blocks, not in* — work that blocks this milestone's tasks from
    outside it, up to two hops out. This is the valuable one: it finds a
    hole in the plan rather than a slip of the hand, and the worst case
    it names is a blocker scheduled *after* the work it blocks.

    Args:
        milestone: The milestone to examine.
        today: Reference date; defaults to the local current date.

    Returns:
        ``{"near": [...], "blocks": [...]}``.
    """
    today = today or timezone.localdate()
    return {
        "near": _near_report(milestone),
        "blocks": _blocks_report(milestone, today),
    }


def _near_report(milestone, limit: int = 8) -> list[dict]:
    """Return in-scope work due before the date that joined no milestone.

    Args:
        milestone: The milestone to examine.
        limit: How many rows to return.

    Returns:
        Row dicts with ``task``, soonest due first.
    """
    rows = (
        Task.objects.filter(
            counted_q(),
            kind=Task.KIND_TASK,
            project__in=milestone.projects.all(),
            milestone__isnull=True,
            due_date__lte=milestone.target_date,
        )
        .exclude(status=Task.STATUS_DONE)
        .select_related("project")
        .order_by("due_date", "number")
    )
    return [{"task": task} for task in rows[:limit]]


def _blocks_report(milestone, today, limit: int = 6) -> list[dict]:
    """Return the work blocking this milestone from outside it.

    Walks the ``blocks`` graph outwards from the milestone's own open
    work: first what blocks it directly, then what blocks those blockers.
    Two hops and no further — past that the chain stops being something
    a person can act on.

    Args:
        milestone: The milestone to examine.
        today: Reference date.
        limit: How many rows to return.

    Returns:
        Row dicts with ``task``, the ``chain`` back to the milestone's
        own task, the ``other`` milestone it sits in, whether it is
        ``in_scope``, and ``why`` it matters.
    """
    inside = list(
        milestone.tasks.filter(counted_q()).exclude(status=Task.STATUS_DONE).values_list("id", flat=True),
    )
    if not inside:
        return []
    scope_ids = set(milestone.projects.values_list("id", flat=True))
    seen: dict[int, list] = {}
    frontier = inside
    chains: dict[int, list] = {task_id: [task_id] for task_id in inside}
    for _hop in range(2):
        blockers = (
            Task.objects.filter(counted_q(), blocks__id__in=frontier)
            .exclude(status=Task.STATUS_DONE)
            .exclude(id__in=inside)
            .select_related("project", "milestone")
            .prefetch_related("blocks")
            .distinct()
        )
        next_frontier = []
        for blocker in blockers:
            if blocker.id in seen:
                continue
            blocked = next(
                (task.id for task in blocker.blocks.all() if task.id in chains),
                frontier[0],
            )
            chains[blocker.id] = [blocker.id, *chains.get(blocked, [blocked])]
            seen[blocker.id] = chains[blocker.id]
            next_frontier.append(blocker.id)
            if len(seen) >= limit:
                break
        if len(seen) >= limit or not next_frontier:
            frontier = next_frontier
            break
        frontier = next_frontier
    if not seen:
        return []
    rows = []
    blockers = (
        Task.objects.filter(id__in=list(seen))
        .select_related("project", "milestone")
        .order_by("project__slug_prefix", "number")
    )
    for blocker in blockers:
        in_scope = blocker.project_id in scope_ids
        other = blocker.milestone
        rows.append(
            {
                "task": blocker,
                "chain": seen[blocker.id],
                "other": other,
                "in_scope": in_scope,
                "why": _blocker_reason(milestone, blocker, other, in_scope),
            },
        )
    return rows


def _blocker_reason(milestone, blocker, other, in_scope: bool) -> str:
    """Say why a blocker outside the milestone is worth looking at.

    Args:
        milestone: The milestone being blocked.
        blocker: The blocking task.
        other: The milestone the blocker sits in, if any.
        in_scope: Whether the milestone's scope covers the blocker.

    Returns:
        A translated sentence, or the empty string when there is nothing
        to say beyond what the row already shows.
    """
    if not in_scope:
        return _("%(project)s is outside this milestone's scope — widen it or drop the dependency.") % {
            "project": blocker.project.name,
        }
    if other is None:
        # Saying "in no milestone at all" next to an Add button that is
        # offered for exactly that reason costs a line per row and tells
        # the reader nothing they cannot already see.
        return ""
    gap = (other.target_date - milestone.target_date).days
    if gap > 0:
        return ngettext(
            "Sits in %(name)s, %(count)d day after this date — the blocker is scheduled after the work it blocks.",
            "Sits in %(name)s, %(count)d days after this date — the blocker is scheduled after the work it blocks.",
            gap,
        ) % {
            "name": other.name,
            "count": gap,
        }
    return _("Sits in %(name)s, %(date)s.") % {
        "name": other.name,
        "date": date_label(other.target_date),
    }


def epic_milestone_rows(tasks) -> list[dict]:
    """Return where an epic's work sits, read off the tasks.

    An epic stores no milestone of its own; it derives the set from the
    work it collects, exactly as it already derives its dates, its size
    and its progress. An epic whose work spans three milestones is
    normal and reads as normal here.

    Args:
        tasks: The epic's counted tasks, with ``milestone`` loaded.

    Each row carries what the epic's own share of that date looks like —
    how much of it is done, how much already runs past the date — and the
    milestone's state, which is read from its WHOLE scope rather than
    from this epic's slice: an epic finishing its three tasks does not
    make the milestone complete.

    Args:
        tasks: The epic's counted tasks, with ``milestone`` loaded.

    Returns:
        Row dicts with ``milestone`` (``None`` for the unattached
        remainder), ``count``, ``done``, ``percent``, ``late``, and for a
        real milestone ``state`` / ``state_label`` / ``countdown``.
        Soonest date first, unattached last.
    """
    buckets: dict[int | None, dict] = {}
    for task in tasks:
        row = buckets.setdefault(
            task.milestone_id,
            {
                "milestone": task.milestone,
                "count": 0,
                "done": 0,
                "late": 0,
            },
        )
        row["count"] += 1
        if task.status == Task.STATUS_DONE:
            row["done"] += 1
        elif task.milestone_id and task.due_date and task.due_date > task.milestone.target_date:
            row["late"] += 1
    rows = list(buckets.values())
    # One batch for the page: a row's badge needs the milestone's own
    # state, and an epic can sit in a dozen of them.
    progress = progress_by_milestone([row["milestone"].id for row in rows if row["milestone"] is not None])
    for row in rows:
        row["percent"] = round(row["done"] / row["count"] * 100) if row["count"] else 0
        milestone = row["milestone"]
        if milestone is None:
            continue
        state = milestone.state(counts=progress.get(milestone.id, (0, 0)))
        row["state"] = state
        row["state_label"] = STATE_LABELS[state]
        row["countdown"] = countdown(milestone, state)
    rows.sort(
        key=lambda row: (
            row["milestone"] is None,
            row["milestone"].target_date if row["milestone"] else datetime.date.max,
            row["milestone"].id if row["milestone"] else 0,
        ),
    )
    return rows


def epic_milestone_targets(workspace, tasks) -> list[dict]:
    """Return the milestones an epic's work could be committed to.

    "Set the milestone for this epic's tasks" is a bulk write onto the
    tasks, so the question each candidate has to answer is how much of
    the epic it can actually take: a milestone only covers the projects
    in its scope, and an epic that spans four projects will meet plenty
    that cover one.

    Args:
        workspace: The epic's workspace.
        tasks: The epic's counted tasks.

    Returns:
        Row dicts with ``milestone``, ``fits`` and ``total``, soonest
        date first, leaving out the ones that fit nothing.
    """
    if not tasks:
        return []
    project_ids = [task.project_id for task in tasks]
    rows = []
    candidates = (
        Milestone.objects.filter(workspace=workspace, closed_at__isnull=True)
        .prefetch_related("projects")
        .order_by(
            "target_date",
            "id",
        )[:20]
    )
    for milestone in candidates:
        scope = {project.id for project in milestone.projects.all()}
        fits = sum(1 for project_id in project_ids if project_id in scope)
        if not fits:
            continue
        rows.append(
            {
                "milestone": milestone,
                "fits": fits,
                "total": len(tasks),
            },
        )
    return rows


def project_overview(project, today=None) -> dict:
    """Return the milestone block the project's Overview tab draws.

    Two readings of the same set, because they answer different
    questions. The cards say where the next dates stand and how much of
    each one this project owns — on a shared date the local slice is not
    the commitment, so both numbers are kept. The decisions say which of
    those dates waits on a person rather than on work: finished work
    stays open until someone closes it, a missed date has to be closed
    or moved, an empty one has nothing attached yet, and work in no
    milestone belongs to no date at all.

    A milestone's state is read from its whole scope while its progress
    bar is read from this project's slice. The two are different numbers
    on purpose: a project finishing its three tasks does not make a
    shared date complete.

    Closed milestones are left out of both readings — a closed date is
    history, and history reads better on the Plan tab.

    Args:
        project: The project whose Overview is being rendered.
        today: Reference date; defaults to the local current date.

    Returns:
        ``cards`` (at most :data:`OVERVIEW_CARD_LIMIT`, the most overdue
        first), ``decisions``, ``open_count``, ``shared_count`` and
        ``unattached``.
    """
    today = today or timezone.localdate()
    milestones = list(
        Milestone.objects.filter(
            projects=project,
            closed_at__isnull=True,
        )
        .prefetch_related("projects")
        .order_by(
            "target_date",
            "id",
        ),
    )
    by_milestone: dict[int, list[tuple]] = defaultdict(list)
    for milestone_id, project_id, status, due_date in _counted_cells([m.id for m in milestones]):
        by_milestone[milestone_id].append((project_id, status, due_date))
    rows = [_overview_row(milestone, by_milestone[milestone.id], project, today) for milestone in milestones]
    unattached = (
        Task.objects.work()
        .filter(counted_q(), project=project, milestone__isnull=True)
        .exclude(status=Task.STATUS_DONE)
        .count()
    )
    overdue = [row for row in rows if row["state"] == Milestone.STATE_OVERDUE]
    ahead = [row for row in rows if row["state"] not in (Milestone.STATE_OVERDUE, Milestone.STATE_COMPLETE)]
    return {
        "cards": (overdue + ahead)[:OVERVIEW_CARD_LIMIT],
        "decisions": _overview_decisions(rows, unattached),
        "open_count": len(rows),
        "shared_count": sum(1 for row in rows if row["shared"]),
        "unattached": unattached,
    }


def _overview_row(milestone, cells: list[tuple], project, today) -> dict:
    """Build one Overview card from a milestone's counted work.

    Args:
        milestone: The milestone, with ``projects`` prefetched.
        cells: Its ``(project_id, status, due_date)`` tuples.
        project: The project whose slice the card reports.
        today: Reference date.

    Returns:
        The card dict the Overview template renders.
    """
    scope_done = sum(1 for _project_id, status, _due in cells if status == Task.STATUS_DONE)
    scope_total = len(cells)
    state = _state_from_cells(milestone, scope_done, scope_total, today)
    past = milestone.target_date < today
    mine = [(status, due_date) for project_id, status, due_date in cells if project_id == project.id]
    done = sum(1 for status, _due in mine if status == Task.STATUS_DONE)
    scope = sorted(other.slug_prefix for other in milestone.projects.all())
    return {
        "milestone": milestone,
        "state": state,
        "state_label": STATE_LABELS[state],
        "countdown": countdown(milestone, state, today),
        "date_label": date_label(milestone.target_date),
        "done": done,
        "total": len(mine),
        "open": len(mine) - done,
        "percent": round(done / len(mine) * 100) if mine else 0,
        "risk": sum(
            1 for status, due_date in mine if status != Task.STATUS_DONE and _is_at_risk(milestone, due_date, past)
        ),
        "scope_total": scope_total,
        "shared": len(scope) > 1,
        "scope": scope,
    }


def _overview_decisions(rows: list[dict], unattached: int) -> list[dict]:
    """Collect the milestones of a project that wait on a person.

    Grouped by kind rather than by date, because the kind is what the
    reader acts on: everything closeable first, then everything missed,
    then the dates nobody has attached work to.

    Args:
        rows: Every open milestone of the project, from
            :func:`_overview_row`, soonest date first.
        unattached: Open counted work of the project in no milestone.

    Returns:
        Row dicts with ``kind`` (``closeable`` / ``missed`` / ``empty``
        / ``unattached``), ``text``, ``action`` and the ``milestone`` to
        open — ``None`` on the unattached remainder, which opens the
        Plan instead. The icon and the colour are the template's to
        pick: a Lucide name reaching the tag through a variable is
        invisible to the sprite builder.
    """
    decisions = []
    for row in rows:
        if row["state"] == Milestone.STATE_COMPLETE:
            decisions.append(
                {
                    "kind": "closeable",
                    "milestone": row["milestone"],
                    "text": _("%(name)s — all done, not closed") % {"name": row["milestone"].name},
                    "action": _("Close"),
                },
            )
    for row in rows:
        if row["state"] == Milestone.STATE_OVERDUE:
            decisions.append(
                {
                    "kind": "missed",
                    "milestone": row["milestone"],
                    "text": _("%(name)s — %(countdown)s, %(open)s")
                    % {
                        "name": row["milestone"].name,
                        "countdown": row["countdown"],
                        "open": ngettext(
                            "%(count)d open here",
                            "%(count)d open here",
                            row["open"],
                        )
                        % {"count": row["open"]},
                    },
                    "action": _("Close or move"),
                },
            )
    for row in rows:
        if row["state"] == Milestone.STATE_OPEN and not row["scope_total"]:
            decisions.append(
                {
                    "kind": "empty",
                    "milestone": row["milestone"],
                    "text": _("%(name)s — nothing attached yet") % {"name": row["milestone"].name},
                    "action": _("Add work"),
                },
            )
    if unattached:
        decisions.append(
            {
                "kind": "unattached",
                "milestone": None,
                "text": ngettext(
                    "%(count)d open task in no milestone",
                    "%(count)d open tasks in no milestone",
                    unattached,
                )
                % {"count": unattached},
                "action": _("Open plan"),
            },
        )
    return decisions


#: The states the list offers as a filter, in the order the chips sit.
#: ``today`` folds into ``open`` — a date due today is open until it is
#: missed, and a chip for the one day it applies would be noise.
LIST_STATES = [
    Milestone.STATE_OPEN,
    Milestone.STATE_OVERDUE,
    Milestone.STATE_COMPLETE,
    Milestone.STATE_CLOSED,
]


def filter_rows(rows: list[dict], *, state="", project_id=None, owner_id=None, at_risk=False) -> list[dict]:
    """Narrow the milestone list to what was asked for.

    Filtered after the rows are built rather than in SQL, because three
    of the four things worth filtering on are read off the work and not
    stored: a milestone's state, its progress and its risk all come from
    replaying what is attached. The page is a dozen rows, so the cost of
    building them all first is nothing against the cost of teaching the
    database to derive them.

    Args:
        rows: Rows from :func:`workspace_rows`.
        state: One of :data:`LIST_STATES`, or empty for every state.
        project_id: Keep only milestones whose scope covers this project.
        owner_id: Keep only milestones this person answers for.
        at_risk: Keep only milestones with work that will miss the date.

    Returns:
        The rows that survive, in the order they arrived.
    """
    kept = rows
    if state in LIST_STATES:
        wanted = {state}
        if state == Milestone.STATE_OPEN:
            # A date due today has not been missed, so it is open.
            wanted.add(Milestone.STATE_TODAY)
        kept = [row for row in kept if row["state"] in wanted]
    if project_id:
        kept = [row for row in kept if any(slice_["project"].id == project_id for slice_ in row["projects"])]
    if owner_id:
        kept = [row for row in kept if row["milestone"].owner_id == owner_id]
    if at_risk:
        kept = [row for row in kept if row["risk"]]
    return kept


def list_facets(rows: list[dict], today=None) -> dict:
    """Count what each filter would leave, so a chip can say so.

    A filter chip that offers a choice leading to an empty page is worse
    than no chip. Every count here is taken against the unfiltered rows,
    which is what makes them stable while someone clicks around.

    Args:
        rows: Every row of the workspace, unfiltered.
        today: Reference date; defaults to the local current date.

    Returns:
        ``states`` (key, label, count), ``projects`` and ``owners`` as
        rows with their counts, and ``at_risk``.
    """
    today = today or timezone.localdate()
    states = []
    for key in LIST_STATES:
        count = len(filter_rows(rows, state=key))
        states.append(
            {
                "key": key,
                "label": STATE_LABELS[key],
                "count": count,
            },
        )
    projects: dict = {}
    owners: dict = {}
    for row in rows:
        for slice_ in row["projects"]:
            cell = projects.setdefault(slice_["project"].id, {"project": slice_["project"], "count": 0})
            cell["count"] += 1
        owner = row["milestone"].owner
        if owner is not None:
            cell = owners.setdefault(owner.id, {"owner": owner, "count": 0})
            cell["count"] += 1
    return {
        "states": states,
        "projects": sorted(projects.values(), key=lambda cell: cell["project"].slug_prefix),
        "owners": sorted(owners.values(), key=lambda cell: cell["owner"].display_name),
        "at_risk": sum(1 for row in rows if row["risk"]),
    }
