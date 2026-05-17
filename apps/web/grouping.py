"""Group a flat task list into ordered sections for the List view.

The List view (``_list_panel.html``) is the third tab next to Kanban
and Table. It renders the same task set as the other two but lays
the tasks out as labelled sections grouped by one of several axes —
deadline, status, priority, assignee, or project — the user picks
from a dropdown.

Each axis returns the same shape: a list of ``{"key", "label",
"tone", "tasks"}`` dicts. Sections with no tasks are dropped so the
template doesn't render empty headers; the one exception is the
deadline axis on My Work, where the ``recently_done`` section is
always rendered (mirrors the original My Work layout).
"""

from __future__ import annotations

import datetime

from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.tasks.models import Task

LIST_AXES = ("deadline", "status", "priority", "assignee", "project")

_STATUS_TONES = {
    Task.STATUS_PLANNED: "zinc",
    Task.STATUS_TODO: "blue",
    Task.STATUS_IN_PROGRESS: "violet",
    Task.STATUS_IN_REVIEW: "amber",
    Task.STATUS_DONE: "emerald",
}

_PRIORITY_TONES = {
    Task.URGENT: "rose",
    Task.HIGH: "orange",
    Task.MEDIUM: "amber",
    Task.LOW: "sky",
    Task.NO_PRIORITY: "zinc",
}


def group_tasks(tasks, axis, *, request_user=None, keep_empty=()):
    """Return ``[{"key", "label", "tone", "tasks"}]`` for ``axis``.

    Args:
        tasks: Iterable of :class:`Task`. The caller has already
            filtered / ordered it; this helper just buckets the
            existing list in Python.
        axis: One of :data:`LIST_AXES`. Unknown axes fall back to
            empty sections.
        request_user: Acting user — used by the deadline axis to
            decide what counts as "today" in the user's timezone.
            Unused by the other axes but kept for symmetry.
        keep_empty: Iterable of section keys that should render even
            when empty (e.g. ``("recently_done",)`` for My Work so
            the slot stays visible).

    Returns:
        Ordered list of section dicts. Section ordering is axis-
        specific: deadline runs Overdue → Recently-done, status runs
        Planned → Done, priority runs Urgent → No-priority, assignee
        / project run alphabetical by display name.
    """
    keep_empty = set(keep_empty)
    if axis == "deadline":
        sections = _group_by_deadline(tasks)
    elif axis == "status":
        sections = _group_by_status(tasks)
    elif axis == "priority":
        sections = _group_by_priority(tasks)
    elif axis == "assignee":
        sections = _group_by_assignee(tasks, request_user=request_user)
    elif axis == "project":
        sections = _group_by_project(tasks)
    else:
        sections = []
    return [s for s in sections if s["tasks"] or s["key"] in keep_empty]


def _group_by_deadline(tasks):
    """Bucket by due_date relative to today; done-recent gets its own slot."""
    today = timezone.localdate()
    week_end = today + datetime.timedelta(days=6)
    done_cutoff = timezone.now() - datetime.timedelta(days=7)
    buckets = {
        "overdue": [],
        "today": [],
        "week": [],
        "later": [],
        "no_deadline": [],
        "recently_done": [],
    }
    for task in tasks:
        if task.status == Task.STATUS_DONE:
            if task.updated_at >= done_cutoff:
                buckets["recently_done"].append(task)
            continue
        if task.due_date is None:
            buckets["no_deadline"].append(task)
        elif task.due_date < today:
            buckets["overdue"].append(task)
        elif task.due_date == today:
            buckets["today"].append(task)
        elif task.due_date <= week_end:
            buckets["week"].append(task)
        else:
            buckets["later"].append(task)
    return [
        {"key": "overdue", "label": _("Overdue"), "tone": "rose", "tasks": buckets["overdue"]},
        {"key": "today", "label": _("Today"), "tone": "amber", "tasks": buckets["today"]},
        {"key": "week", "label": _("This week"), "tone": "violet", "tasks": buckets["week"]},
        {"key": "later", "label": _("Later"), "tone": "zinc", "tasks": buckets["later"]},
        {"key": "no_deadline", "label": _("No deadline"), "tone": "zinc", "tasks": buckets["no_deadline"]},
        {
            "key": "recently_done",
            "label": _("Recently done"),
            "tone": "emerald",
            "tasks": buckets["recently_done"],
        },
    ]


def _group_by_status(tasks):
    """Bucket by ``Task.status`` in workflow order."""
    by_status = {s: [] for s in Task.STATUS_VALUES}
    for task in tasks:
        by_status.setdefault(task.status, []).append(task)
    return [
        {
            "key": s,
            "label": Task.STATUS_LABELS[s],
            "tone": _STATUS_TONES[s],
            "tasks": by_status[s],
        }
        for s in Task.STATUS_VALUES
    ]


def _group_by_priority(tasks):
    """Bucket by ``Task.priority``: urgent → low → no-priority."""
    by_priority = {p: [] for p, _label in Task.PRIORITY_CHOICES}
    for task in tasks:
        by_priority.setdefault(task.priority, []).append(task)
    # Order: urgent (1), high (2), medium (3), low (4), no-priority (0).
    order = [Task.URGENT, Task.HIGH, Task.MEDIUM, Task.LOW, Task.NO_PRIORITY]
    priority_labels = dict(Task.PRIORITY_CHOICES)
    return [
        {
            "key": str(p),
            "label": priority_labels[p],
            "tone": _PRIORITY_TONES[p],
            "tasks": by_priority.get(p, []),
        }
        for p in order
    ]


def _group_by_assignee(tasks, *, request_user):
    """Bucket by assignee, alphabetical by display name. Unassigned last."""
    by_user = {}
    unassigned = []
    for task in tasks:
        if task.assignee_id is None:
            unassigned.append(task)
            continue
        by_user.setdefault(task.assignee_id, {"user": task.assignee, "tasks": []})
        by_user[task.assignee_id]["tasks"].append(task)
    ordered = sorted(
        by_user.values(),
        key=lambda e: (e["user"].display_name.lower(), e["user"].username.lower()),
    )
    sections = [
        {
            "key": str(entry["user"].id),
            "label": entry["user"].display_name,
            "tone": "zinc",
            "tasks": entry["tasks"],
        }
        for entry in ordered
    ]
    sections.append(
        {"key": "unassigned", "label": _("Unassigned"), "tone": "zinc", "tasks": unassigned},
    )
    return sections


def _group_by_project(tasks):
    """Bucket by project, alphabetical by name."""
    by_project = {}
    for task in tasks:
        pid = task.project_id
        by_project.setdefault(pid, {"project": task.project, "tasks": []})
        by_project[pid]["tasks"].append(task)
    ordered = sorted(by_project.values(), key=lambda e: e["project"].name.lower())
    return [
        {
            "key": str(entry["project"].id),
            "label": entry["project"].name,
            "tone": "zinc",
            "tasks": entry["tasks"],
        }
        for entry in ordered
    ]
