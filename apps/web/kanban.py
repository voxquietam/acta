"""The kanban board's lanes — one row per milestone, or per epic.

The columns answer "what state is this in". A lane answers "whose part of
the plan is it", and the two are not alternatives: a lane is a cut across
the same columns, so a card in the ``in-progress`` column means the same
thing whether or not the board is sliced. See
``docs/decisions/0037-milestones.md`` for what a milestone is.

Everything here works on a task list the caller has already paid for. The
one query it makes is the whole-scope progress of the milestones on
screen, read in a single batch — a lane header reports what its milestone
is as a whole, which the board's own filtered rows cannot tell it.
"""

from __future__ import annotations

from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.milestones import services as milestone_services
from apps.tasks.models import Task
from apps.web.plan import late_days

#: The axes a board can be sliced by, in the order the knob offers them.
LANES = {
    "none": _("None"),
    "milestone": _("Milestone"),
    "epic": _("Epic"),
}
DEFAULT_LANES = "none"


def resolve_lanes(request) -> str:
    """Resolve the lane axis: querystring → cookie → none.

    Args:
        request: The active request.

    Returns:
        A key of :data:`LANES`.
    """
    raw = request.GET.get("lanes")
    if raw in LANES:
        return raw
    cookie = request.COOKIES.get("acta_kanban_lanes")
    return cookie if cookie in LANES else DEFAULT_LANES


def lane_options(active: str) -> list[dict]:
    """Render-ready knob entries.

    Args:
        active: The currently chosen axis.

    Returns:
        ``{"key", "label", "active"}`` dicts in the knob's order.
    """
    return [{"key": key, "label": label, "active": key == active} for key, label in LANES.items()]


def build_lanes(tasks, axis: str, *, project=None) -> list[dict]:
    """Slice an already-filtered board into lanes.

    Args:
        tasks: The board's tasks, with ``milestone`` and ``epic``
            select_related.
        axis: ``"milestone"`` or ``"epic"``; anything else returns ``[]``.
        project: The project in view, or ``None`` across the workspace —
            a milestone lane names the other projects it covers only
            where there is more than one in play.

    Returns:
        Lane dicts, each carrying its header and one cell per kanban
        status. The uncommitted bucket is last and is dropped when it
        holds nothing.
    """
    if axis == "milestone":
        groups, loose_label, loose_icon = _by_milestone(tasks), _("No milestone"), "circle-dashed"
    elif axis == "epic":
        groups, loose_label, loose_icon = _by_epic(tasks), _("No epic"), "circle-dashed"
    else:
        return []

    # One batch for the page, not one query per lane: a lane header
    # reports what its milestone is as a whole, and a board can draw a
    # dozen of them.
    progress = milestone_services.progress_by_milestone(
        [group["milestone"].id for group in groups if group.get("milestone") is not None],
    )
    lanes = [_lane(group, progress=progress, project=project) for group in groups if group["key"] != "none"]
    loose = next((group for group in groups if group["key"] == "none"), None)
    if loose and loose["tasks"]:
        lanes.append(
            {
                **_rollup(loose["tasks"]),
                "key": "none",
                "label": loose_label,
                "icon": loose_icon,
                "sub": _count_line(loose["tasks"]),
                "state": "",
                "state_label": "",
                "countdown": "",
                "collapsed": False,
                "cells": _cells(loose["tasks"]),
            },
        )
    return lanes


def _by_milestone(tasks) -> list[dict]:
    """Bucket by milestone, soonest date first, the uncommitted last.

    Args:
        tasks: The board's tasks.

    Returns:
        Bucket dicts with ``key`` / ``milestone`` / ``tasks``.
    """
    buckets: dict[int, dict] = {}
    loose = []
    for task in tasks:
        if task.milestone_id is None:
            loose.append(task)
            continue
        bucket = buckets.setdefault(task.milestone_id, {"milestone": task.milestone, "tasks": []})
        bucket["tasks"].append(task)
    ordered = sorted(buckets.values(), key=lambda b: (b["milestone"].target_date, b["milestone"].id))
    groups = [{"key": f"ms-{b['milestone'].id}", "milestone": b["milestone"], "tasks": b["tasks"]} for b in ordered]
    groups.append({"key": "none", "milestone": None, "tasks": loose})
    return groups


def _by_epic(tasks) -> list[dict]:
    """Bucket by epic, by title, the ungrouped last.

    Args:
        tasks: The board's tasks.

    Returns:
        Bucket dicts with ``key`` / ``epic`` / ``tasks``.
    """
    buckets: dict[int, dict] = {}
    loose = []
    for task in tasks:
        if task.epic_id is None:
            loose.append(task)
            continue
        bucket = buckets.setdefault(task.epic_id, {"epic": task.epic, "tasks": []})
        bucket["tasks"].append(task)
    ordered = sorted(buckets.values(), key=lambda b: (b["epic"].title.lower(), b["epic"].id))
    groups = [{"key": f"ep-{b['epic'].id}", "epic": b["epic"], "tasks": b["tasks"]} for b in ordered]
    groups.append({"key": "none", "epic": None, "tasks": loose})
    return groups


def _lane(group: dict, *, progress: dict, project=None) -> dict:
    """Build one lane from its bucket.

    Args:
        group: A bucket from :func:`_by_milestone` or :func:`_by_epic`.
        progress: Whole-scope counts from
            :func:`~apps.milestones.services.progress_by_milestone`.
        project: The project in view, or ``None``.

    Returns:
        The lane dict the board renders.
    """
    milestone = group.get("milestone")
    lane = {
        **_rollup(group["tasks"]),
        "key": group["key"],
        "cells": _cells(group["tasks"]),
        "state": "",
        "state_label": "",
        "countdown": "",
        "collapsed": False,
    }
    if milestone is not None:
        state = milestone.state(counts=progress.get(milestone.id, (0, 0)))
        lane.update(
            {
                "label": milestone.name,
                "icon": "diamond",
                "milestone": milestone,
                "state": state,
                "state_label": milestone_services.STATE_LABELS[state],
                "countdown": milestone_services.countdown(milestone, state),
                "sub": date_format(milestone.target_date, "M j"),
                # A date already settled is not what anyone opened the
                # board for; it starts folded and says so in its header.
                "collapsed": state in ("closed", "complete"),
            },
        )
    else:
        epic = group["epic"]
        lane.update(
            {
                "label": epic.title,
                "icon": "square-kanban",
                "epic": epic,
                "sub": _count_line(group["tasks"]),
            },
        )
    return lane


def _rollup(tasks) -> dict:
    """Summarise a lane's work the way its header reports it.

    Args:
        tasks: The lane's tasks.

    Returns:
        ``done`` / ``total`` / ``percent`` / ``risk``.
    """
    done = sum(1 for task in tasks if task.status == Task.STATUS_DONE)
    total = len(tasks)
    return {
        "done": done,
        "total": total,
        "percent": round(done / total * 100) if total else 0,
        # Work already running past the date it was committed to. Only a
        # milestone lane can have any; an epic lane inherits whatever its
        # tasks are committed to elsewhere, which is still worth saying.
        "risk": sum(1 for task in tasks if late_days(task) > 0),
    }


def _cells(tasks) -> list[dict]:
    """Split a lane's tasks into one cell per kanban status.

    Args:
        tasks: The lane's tasks.

    Returns:
        One cell per status, in the board's column order.
    """
    by_status: dict[str, list] = {status: [] for status in Task.KANBAN_STATUS_VALUES}
    for task in tasks:
        if task.status in by_status:
            by_status[task.status].append(task)
    return [{"key": status, "tasks": by_status[status]} for status in Task.KANBAN_STATUS_VALUES]


def _count_line(tasks) -> str:
    """Return "N tasks" for a lane with no date to talk about.

    Args:
        tasks: The lane's tasks.

    Returns:
        A translated line.
    """
    return ngettext("%(count)d task", "%(count)d tasks", len(tasks)) % {"count": len(tasks)}
