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

from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

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


def _late_days(task) -> int:
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
        "risk": sum(1 for task in tasks if _late_days(task) > 0),
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


def build_plan_rows(tasks: list, cut: str, today=None) -> list[dict]:
    """Flatten a project's work into the Plan tree's rows.

    Args:
        tasks: The project's tasks, with ``milestone`` and ``epic``
            select_related; archived and cancelled ones may ride along
            and are filtered here by the counting rule.
        cut: A key of :data:`CUTS`.
        today: Reference date; unused for now, kept so callers do not
            have to change when a row starts reading it.

    Returns:
        Row dicts: ``kind`` is ``group`` or ``task``, ``depth`` is 0 or
        1 for groups and 1 or 2 for tasks, and a group's ``key`` is what
        the collapse state is stored under.
    """
    counted = [task for task in tasks if _counts(task)]
    first, second = CUTS.get(cut, CUTS[DEFAULT_CUT])
    rows: list[dict] = []
    if first is None:
        rows.extend(_task_row(task, depth=0) for task in _sorted(counted))
        return rows
    for group in _GROUPERS[first](counted):
        rows.append(_group_row(group, depth=0))
        if second is None:
            rows.extend(_task_row(task, depth=1, parent=group["key"]) for task in _sorted(group["tasks"]))
            continue
        for inner in _GROUPERS[second](group["tasks"]):
            inner_key = f"{group['key']}:{inner['key']}"
            rows.append(_group_row(inner, depth=1, key=inner_key, parent=group["key"]))
            rows.extend(_task_row(task, depth=2, parent=inner_key) for task in _sorted(inner["tasks"]))
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


def _group_row(group: dict, *, depth: int, key: str | None = None, parent: str | None = None) -> dict:
    """Build one group row with its rollup.

    Args:
        group: The bucket from a grouper.
        depth: 0 for the outer level, 1 for the inner one.
        key: Collapse key; defaults to the group's own key.
        parent: The outer group's key, when nested.

    Returns:
        The row dict the Plan template renders.
    """
    row = {
        "kind": "group",
        "depth": depth,
        "indent": depth * 22,
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
        # Across the workspace the same date holds work from several
        # projects, and whose part is whose is the first thing a reader
        # asks. Inside one project it would say the project's own name
        # back at them, so it only appears when it carries information.
        projects = sorted({task.project.slug_prefix for task in group["tasks"]})
        if len(projects) > 1:
            parts.append(" ".join(projects))
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


def _task_row(task, *, depth: int, parent: str | None = None) -> dict:
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
        "late": _late_days(task),
    }
