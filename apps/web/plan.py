"""The Plan tree: one project's work cut two levels deep.

The Plan tab answers a question no single board does: *how does this
project's work sit inside the things it has committed to*. That is two
dimensions at once — a milestone holds epics, an epic spans milestones —
so the surface is a two-level cut rather than a group-by, and which
dimension goes first is the knob (design "AppPlan").

Rows come out flat, each carrying its depth and kind, because a flat
list renders in one template loop and collapses with one Alpine flag per
key. Nesting the data would buy nothing and cost a recursive include.

Counting follows the one rule the product states everywhere else
(:func:`~apps.tasks.models.counted_q`), and "at risk" is the milestone
rule from ADR 0037: a task is late when it is unfinished and its own due
date falls after the date it is committed to.
"""

import datetime

from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.milestones import services as milestone_services
from apps.milestones.models import Milestone
from apps.tasks.models import Task

#: The cuts the knob offers: ``key -> (first level, second level)``.
#: ``None`` as the first level is the flat list — no grouping at all.
CUTS = {
    "me": ("milestone", "epic"),
    "em": ("epic", "milestone"),
    "sm": ("status", "milestone"),
    "flat": (None, None),
}

CUT_LABELS = {
    "me": _("Milestone › Epic"),
    "em": _("Epic › Milestone"),
    "sm": _("Status › Milestone"),
    "flat": _("Flat"),
}

#: Column heading over the first column, which changes with the cut.
CUT_HEADINGS = {
    "me": _("Milestone › epic › task"),
    "em": _("Epic › milestone › task"),
    "sm": _("Status › milestone › task"),
    "flat": _("Task"),
}

DEFAULT_CUT = "me"

#: How the cut is drawn. The tree answers "what is in what"; the
#: timeline answers "when", with the same rows against a calendar.
RENDERS = {
    "tree": _("Tree"),
    "timeline": _("Timeline"),
}

DEFAULT_RENDER = "tree"


#: The Plan tree's optional columns, in the order they stand. The tree
#: column itself is not here — a tree with no names is not a view.
COLUMNS = {
    "date": _("Target date"),
    "progress": _("Progress"),
    "risk": _("Past its date"),
    "scope": _("Scope"),
}


def resolve_columns(request) -> dict[str, bool]:
    """Resolve which of the Plan tree's columns the reader wants.

    Shown unless switched off, and carried as one cookie rather than four
    — they are one decision ("what is this view for"), taken in one menu.

    Args:
        request: The active request.

    Returns:
        ``{key: shown}`` for every key of :data:`COLUMNS`.
    """
    raw = request.GET.getlist("plan_col")
    if not raw and "plan_col" not in request.GET:
        stored = request.COOKIES.get("acta_plan_cols")
        raw = stored.split(",") if stored is not None else list(COLUMNS)
    return {key: key in raw for key in COLUMNS}


def resolve_cut(request) -> str:
    """Resolve the Plan cut: querystring → cookie → default.

    Args:
        request: The active request.

    Returns:
        A key of :data:`CUTS`.
    """
    raw = request.GET.get("cut")
    if raw in CUTS:
        return raw
    cookie = request.COOKIES.get("acta_plan_cut")
    return cookie if cookie in CUTS else DEFAULT_CUT


def resolve_render(request) -> str:
    """Resolve how the Plan is drawn: querystring → cookie → default.

    Args:
        request: The active request.

    Returns:
        A key of :data:`RENDERS`.
    """
    raw = request.GET.get("render")
    if raw in RENDERS:
        return raw
    # The Timeline tab folded into this one. A link or a cookie still
    # saying ``view=timeline`` asked for the gantt, so it outranks the
    # stored render — otherwise an old bookmark opens a tree.
    if request.GET.get("view") == "timeline":
        return "timeline"
    cookie = request.COOKIES.get("acta_plan_render")
    return cookie if cookie in RENDERS else DEFAULT_RENDER


def _counts(task) -> bool:
    """Return whether a loaded task counts as work.

    Args:
        task: The task to judge.

    Returns:
        ``True`` when it counts towards progress.
    """
    if task.status == Task.STATUS_CANCELLED:
        return False
    return task.archived_at is None or task.status == Task.STATUS_DONE


def late_days(task) -> int:
    """Return how far a task runs past the date it is committed to.

    Args:
        task: The task to measure.

    Returns:
        Days past its milestone, or ``0`` when it is not late or not
        committed to anything.
    """
    if task.milestone_id is None or task.status == Task.STATUS_DONE:
        return 0
    if task.due_date is None:
        return 0
    over = (task.due_date - task.milestone.target_date).days
    return max(0, over)


def _aggregate(tasks: list) -> dict:
    """Summarise a group of tasks the way every Plan row reports itself.

    Args:
        tasks: The counted tasks in the group.

    Returns:
        ``done`` / ``total`` / ``percent`` / ``risk`` / ``window``.
    """
    done = sum(1 for task in tasks if task.status == Task.STATUS_DONE)
    total = len(tasks)
    starts = [task.start_date for task in tasks if task.start_date]
    ends = [task.due_date or task.end_date for task in tasks if (task.due_date or task.end_date)]
    return {
        "done": done,
        "total": total,
        "percent": round(done / total * 100) if total else 0,
        "risk": sum(1 for task in tasks if late_days(task) > 0),
        "window_start": min(starts) if starts else None,
        "window_end": max(ends) if ends else None,
    }


def _milestone_groups(tasks: list) -> list[dict]:
    """Bucket tasks by the milestone they are committed to, by date.

    Args:
        tasks: Counted tasks to split.

    Returns:
        Group dicts, soonest date first, with the uncommitted work last.
    """
    buckets: dict[int | None, dict] = {}
    for task in tasks:
        bucket = buckets.setdefault(
            task.milestone_id,
            {
                "key": f"ms-{task.milestone_id}" if task.milestone_id else "ms-none",
                "milestone": task.milestone,
                "epic": None,
                "status": None,
                "label": task.milestone.name if task.milestone_id else _("No milestone"),
                "tasks": [],
            },
        )
        bucket["tasks"].append(task)
    groups = list(buckets.values())
    groups.sort(
        key=lambda group: (
            group["milestone"] is None,
            group["milestone"].target_date if group["milestone"] else datetime.date.max,
            group["milestone"].id if group["milestone"] else 0,
        ),
    )
    return groups


def _epic_groups(tasks: list) -> list[dict]:
    """Bucket tasks by the epic that collects them, biggest first.

    Args:
        tasks: Counted tasks to split.

    Returns:
        Group dicts with the epic-less work last.
    """
    buckets: dict[int | None, dict] = {}
    for task in tasks:
        bucket = buckets.setdefault(
            task.epic_id,
            {
                "key": f"ep-{task.epic_id}" if task.epic_id else "ep-none",
                "milestone": None,
                "epic": task.epic,
                "status": None,
                "label": task.epic.title if task.epic_id else _("No epic"),
                "tasks": [],
            },
        )
        bucket["tasks"].append(task)
    groups = list(buckets.values())
    groups.sort(key=lambda group: (group["epic"] is None, -len(group["tasks"])))
    return groups


def _status_groups(tasks: list) -> list[dict]:
    """Bucket tasks by status, in the order the board columns run.

    Args:
        tasks: Counted tasks to split.

    Returns:
        Group dicts for the statuses that actually hold work.
    """
    order = [
        Task.STATUS_IN_PROGRESS,
        Task.STATUS_IN_REVIEW,
        Task.STATUS_READY,
        Task.STATUS_TODO,
        Task.STATUS_PLANNED,
        Task.STATUS_DONE,
    ]
    groups = []
    for status in order:
        held = [task for task in tasks if task.status == status]
        if held:
            groups.append(
                {
                    "key": f"st-{status}",
                    "milestone": None,
                    "epic": None,
                    "status": status,
                    "label": Task.STATUS_LABELS[status],
                    "tasks": held,
                },
            )
    return groups


_GROUPERS = {
    "milestone": _milestone_groups,
    "epic": _epic_groups,
    "status": _status_groups,
}


def build_plan_rows(tasks: list, cut: str, today=None, project=None) -> list[dict]:
    """Flatten a project's work into the Plan tree's rows.

    Args:
        tasks: The project's tasks, with ``milestone`` and ``epic``
            select_related; archived and cancelled ones may ride along
            and are filtered here by the counting rule.
        cut: A key of :data:`CUTS`.
        today: Reference date; unused for now, kept so callers do not
            have to change when a row starts reading it.
        project: The project the plan is scoped to, or ``None`` for the
            whole workspace. A milestone row says which projects it
            belongs to, and what that line is worth depends on where the
            reader stands: across the workspace it is "whose part is
            whose", inside one project it is "who else is in this".

    Returns:
        Row dicts: ``kind`` is ``group``, ``task``, ``note`` or
        ``fold``; ``depth`` is the indent level, and a group's ``key``
        is what the collapse state is stored under.
    """
    counted = [task for task in tasks if _counts(task)]
    first, second = CUTS.get(cut, CUTS[DEFAULT_CUT])
    scope = _milestone_scopes(counted)
    progress = _milestone_progress(scope)
    rows: list[dict] = []
    if first is None:
        rows.extend(_leaves(counted, "flat", 0, name_milestone=True))
        return rows
    for group in _GROUPERS[first](counted):
        rows.append(_group_row(group, depth=0, scope=scope, project=project, progress=progress))
        if second is None:
            rows.extend(_leaves(group["tasks"], group["key"], 1))
            continue
        for inner in _GROUPERS[second](group["tasks"]):
            inner_key = f"{group['key']}:{inner['key']}"
            rows.append(
                _group_row(
                    inner,
                    depth=1,
                    key=inner_key,
                    parent=group["key"],
                    scope=scope,
                    project=project,
                    progress=progress,
                ),
            )
            note = _direct_note(first, second, inner)
            if note:
                rows.append(
                    {
                        "kind": "note",
                        "depth": 2,
                        "indent": 44,
                        "ancestors": _ancestors(inner_key),
                        "key": f"{inner_key}:note",
                        "parent": inner_key,
                        "label": note,
                    },
                )
            rows.extend(_leaves(inner["tasks"], inner_key, 2))
    return rows


def _milestone_progress(scope: dict) -> dict[int, tuple[int, int]]:
    """Return every milestone's progress across its whole scope, in one query.

    Two rows read this. A shared date needs it to report what it is as a
    whole, next to the part this project holds. Every date needs it for
    its own state, which is otherwise two counts per row —
    :meth:`Milestone.counts` says as much, and the Plan draws a row per
    milestone.

    Args:
        scope: Milestone scopes from :func:`_milestone_scopes`.

    Returns:
        ``{milestone_id: (done, total)}``; a milestone with no counted
        work is absent.
    """
    if not scope:
        return {}
    return milestone_services.progress_by_milestone(list(scope))


def _direct_note(first: str, second: str, group: dict) -> str:
    """Say what a bucket of leftovers actually is, in the tree.

    Under a milestone, the tasks with no epic are not "unsorted" — they
    are attached to the date directly, which is a normal thing to be and
    reads as an omission unless the row says so.

    Args:
        first: The outer level of the cut.
        second: The inner level.
        group: The inner bucket.

    Returns:
        A translated line, or empty when the bucket needs no explaining.
    """
    if first != "milestone" or second != "epic" or group["epic"] is not None:
        return ""
    count = len(group["tasks"])
    return ngettext(
        "%(count)d task without an epic — attached directly",
        "%(count)d tasks without an epic — attached directly",
        count,
    ) % {"count": count}


FLAT_LIMIT = 30


def _leaves(tasks: list, parent: str, depth: int, *, name_milestone: bool = False) -> list[dict]:
    """Return a bucket's task rows, with the finished work folded away.

    Done work is the bulk of a long-running milestone and the part
    nobody is looking for; it collapses behind a line that says how much
    there is, and expands in place. Unfinished work is never folded —
    that is the work the page is about.

    Args:
        tasks: The bucket's tasks.
        parent: The collapse key the rows sit under.
        depth: Indent level for the rows.

    Returns:
        Task rows, plus a fold line and the done rows behind it.
    """
    ordered = _sorted(tasks)
    open_work = [task for task in ordered if task.status != Task.STATUS_DONE]
    done = [task for task in ordered if task.status == Task.STATUS_DONE]
    # The flat cut is the whole project in one list, so it stops at a
    # screenful of open work and offers the rest rather than rendering
    # two hundred rows nobody asked for.
    capped = open_work[:FLAT_LIMIT] if name_milestone else open_work
    rows = [_task_row(task, depth=depth, parent=parent, name_milestone=name_milestone) for task in capped]
    if len(capped) < len(open_work):
        more = len(open_work) - len(capped)
        rows.append(
            {
                "kind": "fold",
                "depth": depth,
                "indent": depth * 22,
                "ancestors": _ancestors(parent),
                "key": f"{parent}:more",
                "parent": parent,
                "label": ngettext("Show %(count)d more open", "Show %(count)d more open", more) % {"count": more},
            },
        )
        rows.extend(
            _task_row(task, depth=depth, parent=f"{parent}:more", name_milestone=name_milestone)
            for task in open_work[FLAT_LIMIT:]
        )
    if not done:
        return rows
    fold_key = f"{parent}:done"
    rows.append(
        {
            "kind": "fold",
            "depth": depth,
            "indent": depth * 22,
            "ancestors": _ancestors(parent),
            "key": fold_key,
            "parent": parent,
            "label": ngettext("%(count)d done", "%(count)d done", len(done)) % {"count": len(done)},
        },
    )
    rows.extend(_task_row(task, depth=depth, parent=fold_key, name_milestone=name_milestone) for task in done)
    return rows


def _sorted(tasks: list) -> list:
    """Order a group's tasks: unfinished first, then by due date.

    Args:
        tasks: The tasks to order.

    Returns:
        The same tasks, ordered for reading.
    """
    return sorted(
        tasks,
        key=lambda task: (
            task.status == Task.STATUS_DONE,
            task.due_date is None,
            task.due_date or datetime.date.max,
            task.number,
        ),
    )


def _milestone_scopes(tasks: list) -> dict[int, list]:
    """Return every milestone's project scope, in one query.

    A milestone row names the projects it belongs to, and that list is
    the milestone's own scope rather than a reading of its work — a
    project in scope with nothing attached yet is exactly the row worth
    seeing. One query for the page; walking ``milestone.projects`` per
    row would be an N+1 down the tree.

    Args:
        tasks: The counted tasks the rows are built from.

    Returns:
        ``{milestone_id: [projects]}``.
    """
    ids = {task.milestone_id for task in tasks if task.milestone_id}
    if not ids:
        return {}
    return {
        milestone.id: list(milestone.projects.all())
        for milestone in Milestone.objects.filter(id__in=ids).prefetch_related("projects")
    }


def _project_chips(group: dict, scope: dict, project) -> dict:
    """Describe which projects a milestone row belongs to.

    Args:
        group: The bucket being drawn.
        scope: Milestone scopes from :func:`_milestone_scopes`.
        project: The project the plan is scoped to, or ``None``.

    Returns:
        ``label`` and ``chips`` — each chip with its project, how much
        of this milestone's work sits there, and whether that is none.
    """
    milestone = group["milestone"]
    if milestone is None:
        return {"chips": [], "chips_label": ""}
    projects = scope.get(milestone.id, [])
    if project is not None:
        others = [row for row in projects if row.id != project.id]
        return {
            "chips": [{"project": row, "count": None, "empty": False} for row in others],
            "chips_label": _("shared with") if others else "",
        }
    here = {}
    for task in group["tasks"]:
        here[task.project_id] = here.get(task.project_id, 0) + 1
    return {
        "chips": [
            {
                "project": row,
                "count": here.get(row.id, 0),
                "empty": not here.get(row.id, 0),
            }
            for row in projects
        ],
        "chips_label": _("shared ·") if len(projects) > 1 else _("local ·"),
    }


def _group_row(
    group: dict,
    *,
    depth: int,
    key: str | None = None,
    parent: str | None = None,
    scope: dict | None = None,
    project=None,
    progress: dict | None = None,
) -> dict:
    """Build one group row with its rollup.

    Args:
        group: The bucket from a grouper.
        depth: 0 for the outer level, 1 for the inner one.
        key: Collapse key; defaults to the group's own key.
        parent: The outer group's key, when nested.
        scope: Milestone scopes from :func:`_milestone_scopes`.
        project: The project the plan is scoped to, or ``None``.
        progress: Whole-scope counts from :func:`_milestone_progress`.

    Returns:
        The row dict the Plan template renders.
    """
    row = {
        "kind": "group",
        "depth": depth,
        "indent": depth * 22,
        "state": None,
        "sub": "",
        # Every ancestor this row hides behind, so one ``x-show`` can ask
        # about the whole chain: a task under an open epic inside a
        # collapsed milestone is still collapsed.
        "ancestors": [parent] if parent else [],
        "key": key or group["key"],
        "parent": parent,
        "label": group["label"],
        "milestone": group["milestone"],
        "epic": group["epic"],
        "status": group["status"],
        "count": len(group["tasks"]),
    }
    row.update(_aggregate(group["tasks"]))
    row["scope"] = _group_scope(group)
    row.update(_project_chips(group, scope or {}, project))
    # The second line under the name. A milestone says where its date
    # stands, because that is the whole of what it is; anything else says
    # how much work it holds, because that is the whole of what it is.
    milestone = group["milestone"]
    if milestone is not None:
        counts = (progress or {}).get(milestone.id, (0, 0))
        state = milestone.state(counts=counts)
        countdown = milestone_services.countdown(milestone, state)
        row["state"] = state
        row["state_label"] = milestone_services.STATE_LABELS[state]
        # The tree prints the countdown under the date and the state beside
        # the name, the way every other milestone surface does; ``sub`` is
        # the one-line form the timeline's narrow left column takes.
        row["countdown"] = countdown
        row["sub"] = f"{date_format(milestone.target_date, 'M j')} · {countdown}"
        # A shared date reports twice: what this project holds, and what
        # the milestone is as a whole. Either number alone misreads — a
        # local 1/1 on a date that is 1/9 overall looks finished.
        if project is not None and len((scope or {}).get(milestone.id, [])) > 1:
            row["overall_done"], row["overall_total"] = counts
    else:
        row["sub"] = ngettext(
            "%(count)d task",
            "%(count)d tasks",
            len(group["tasks"]),
        ) % {"count": len(group["tasks"])}
    return row


def _group_scope(group: dict) -> str:
    """Say what a group is made of, in one line.

    Args:
        group: The bucket from a grouper.

    Returns:
        A translated line, empty when it would say nothing.
    """
    if group["milestone"] is not None or group["key"] == "ms-none":
        epics = {task.epic_id for task in group["tasks"] if task.epic_id}
        direct = sum(1 for task in group["tasks"] if not task.epic_id)
        parts = []
        if epics:
            parts.append(ngettext("%(count)d epic", "%(count)d epics", len(epics)) % {"count": len(epics)})
        if direct:
            parts.append(ngettext("%(count)d direct", "%(count)d direct", direct) % {"count": direct})
        return " · ".join(parts)
    if group["epic"] is not None:
        milestones = {task.milestone_id for task in group["tasks"] if task.milestone_id}
        loose = sum(1 for task in group["tasks"] if not task.milestone_id)
        parts = []
        if milestones:
            parts.append(
                ngettext("%(count)d milestone", "%(count)d milestones", len(milestones)) % {"count": len(milestones)},
            )
        if loose:
            parts.append(ngettext("%(count)d in none", "%(count)d in none", loose) % {"count": loose})
        return " · ".join(parts)
    return ""


def _ancestors(parent: str | None) -> list[str]:
    """Return every collapse key a row sits under, outermost first.

    Keys nest by composition (``ms-3:ep-7``), so the chain is read back
    out of the key itself rather than carried separately.

    Args:
        parent: The row's immediate parent key, if any.

    Returns:
        The ancestor keys, outermost first.
    """
    if not parent:
        return []
    parts = parent.split(":")
    return [":".join(parts[: index + 1]) for index in range(len(parts))]


def _task_row(task, *, depth: int, parent: str | None = None, name_milestone: bool = False) -> dict:
    """Build one leaf row.

    Args:
        task: The task to draw.
        depth: Indent level.
        parent: The group key it collapses under.

    Returns:
        The row dict the Plan template renders.
    """
    return {
        "kind": "task",
        "depth": depth,
        "indent": depth * 22,
        "ancestors": _ancestors(parent),
        "key": f"task-{task.id}",
        "parent": parent,
        "task": task,
        "late": late_days(task),
        # The flat cut has no group row above it to say which date this
        # belongs to, so the row says it itself.
        "milestone_name": (task.milestone.name if task.milestone_id else _("no milestone")) if name_milestone else "",
        # The date the bar turns rose at, for the chart: everything past
        # the milestone this task is committed to is overrun, and the
        # line it crosses is the point it was committed to.
        "milestone_date": task.milestone.target_date if task.milestone_id else None,
    }
