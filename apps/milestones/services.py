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

#: How far past the target date a projection is still drawn. Beyond this
#: the line says "much later" more honestly than a date does.
PROJECTION_HORIZON_DAYS = 40

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

    Three lines and a projection, which is the shape GitLab and Jira
    release reports settled on: how much work remains, how much work the
    milestone holds at all (so a scope change reads as a scope change and
    not as a stall), and the straight line to zero on the date. The
    projection continues today's pace, and its verdict line — "at this
    pace, done eight days after the date" — is the part people read.

    Drawn from the activity log, never from due dates: a due date is a
    plan, and a burndown drawn from plans cannot be wrong.

    Args:
        milestone: The milestone to chart.
        today: Reference date; defaults to the local current date.

    Returns:
        A dict of parallel series plus the verdict, or ``None`` when
        nothing counted is attached — zero of zero is not a chart.
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
    velocity = _velocity(ever_ids, done_days, start, today)
    projected = today + datetime.timedelta(days=round(open_now / velocity)) if open_now else today
    axis_end = max(
        milestone.target_date,
        today,
        min(projected, milestone.target_date + datetime.timedelta(days=PROJECTION_HORIZON_DAYS)),
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
    projection = [None] * len(labels)
    today_index = (today - start).days
    if open_now and 0 <= today_index < len(labels):
        projection[today_index] = open_now
        end_index = min((projected - start).days, len(labels) - 1)
        for index in range(today_index + 1, end_index + 1):
            elapsed = index - today_index
            projection[index] = round(max(0, open_now - velocity * elapsed), 2)
    slip = (projected - milestone.target_date).days if open_now else 0
    scope_moves = sum(
        1 for index in range(1, today_index + 1) if index < len(scope) and scope[index] != scope[index - 1]
    )
    return {
        "labels": labels,
        "scope": scope,
        "remaining": remaining,
        "ideal": ideal,
        "projection": projection,
        "total": total,
        "open": open_now,
        "today_index": today_index,
        "target_index": (milestone.target_date - start).days,
        "projected_date": projected if open_now else None,
        "slip": slip,
        "behind": open_now > 0 and slip > 0,
        "verdict": _verdict(open_now, projected, slip),
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


def _velocity(task_ids: list[int], done_days: dict, start, today) -> float:
    """Return tasks finished per day since the milestone started filling.

    Args:
        task_ids: Every task that was ever in the milestone.
        done_days: When each finished task became done.
        start: The first day anything joined.
        today: Reference date.

    Returns:
        A strictly positive rate, so a projection always lands somewhere.
    """
    finished = sum(1 for task_id in task_ids if task_id in done_days and done_days[task_id] >= start)
    elapsed = max(1, (today - start).days)
    return max(0.05, finished / elapsed)


def _verdict(open_now: int, projected, slip: int) -> str:
    """Phrase the one line that reads the burndown out loud.

    Args:
        open_now: Counted work still unfinished.
        projected: The day today's pace lands on.
        slip: Days between that day and the target date.

    Returns:
        A translated verdict.
    """
    if not open_now:
        return _("all done")
    if slip > 0:
        return ngettext(
            "Behind · at this pace done %(date)s, %(count)d day after the date",
            "Behind · at this pace done %(date)s, %(count)d days after the date",
            slip,
        ) % {
            "date": date_label(projected),
            "count": slip,
        }
    if slip < 0:
        return ngettext(
            "On track · at this pace done %(date)s, %(count)d day early",
            "On track · at this pace done %(date)s, %(count)d days early",
            -slip,
        ) % {
            "date": date_label(projected),
            "count": -slip,
        }
    return _("On track · at this pace done %(date)s") % {"date": date_label(projected)}
