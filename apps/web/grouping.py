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
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from apps.tasks.models import Task
from apps.web.plan import late_days

LIST_AXES = ("deadline", "status", "priority", "assignee", "project", "cycle", "epic")

# Order cycles within the group-by view: active cycle first, then
# upcoming (planning), then completed.
_CYCLE_STATUS_RANK = {
    "active": 0,
    "planning": 1,
    "completed": 2,
}

_STATUS_TONES = {
    Task.STATUS_PLANNED: "zinc",
    Task.STATUS_READY: "cyan",
    Task.STATUS_TODO: "blue",
    Task.STATUS_IN_PROGRESS: "violet",
    Task.STATUS_IN_REVIEW: "amber",
    Task.STATUS_DONE: "emerald",
    Task.STATUS_CANCELLED: "zinc",
}

_PRIORITY_TONES = {
    Task.URGENT: "rose",
    Task.HIGH: "orange",
    Task.MEDIUM: "amber",
    Task.LOW: "sky",
    Task.NO_PRIORITY: "zinc",
}


def compute_list_section_keys(task, *, request_user=None):
    """Return ``{axis: section_key}`` mapping ``task`` to its bucket per list axis.

    Used to drive client-side row insertion / move in the list view: for
    each axis the panel pre-renders, the JS handler looks up the matching
    ``[data-list-axis]`` wrapper and the ``[data-section-key]`` section
    inside it. Reuses :func:`group_tasks` so the keying logic stays in
    one place — pass a single-task list and pick the only non-empty
    bucket per axis.

    Args:
        task: The :class:`Task` whose section keys to compute.
        request_user: Acting user, forwarded to :func:`group_tasks`.
            Currently unused for key computation (no axis personalises
            its bucket key by viewer); kept for symmetry with the
            grouping helpers so future axes can opt in.

    Returns:
        Dict ``{axis: key}`` covering every axis in :data:`LIST_AXES`
        where a bucket exists for ``task``. ``key`` is always a string
        (matches the ``data-section-key`` attribute the template emits).
    """
    keys: dict[str, str] = {}
    for axis in LIST_AXES:
        sections = group_tasks([task], axis, request_user=request_user)
        for section in sections:
            if section["tasks"]:
                keys[axis] = str(section["key"])
                break
    return keys


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
        / project / epic run alphabetical by display name, with the
        "no epic" bucket last.
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
    elif axis == "cycle":
        sections = _group_by_cycle(tasks)
    elif axis == "epic":
        sections = _group_by_epic(tasks)
    elif axis == "milestone":
        sections = _group_by_milestone(tasks)
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
        # Cancelled is hidden by default, so its section only appears when
        # the user has filtered cancelled tasks back in — never as an empty
        # bucket. The workflow statuses keep their always-present sections.
        if s != Task.STATUS_CANCELLED or by_status[s]
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


def _group_by_cycle(tasks):
    """Bucket by cycle: active first, then upcoming, completed; backlog last.

    Tasks with no cycle collect in a trailing ``backlog`` section. Cycle
    sections sort by lifecycle (active → planning → completed) then by
    cycle number, so the running cycle leads and the backlog trails.
    """
    by_cycle = {}
    backlog = []
    for task in tasks:
        if task.cycle_id is None:
            backlog.append(task)
            continue
        by_cycle.setdefault(task.cycle_id, {"cycle": task.cycle, "tasks": []})
        by_cycle[task.cycle_id]["tasks"].append(task)
    ordered = sorted(
        by_cycle.values(),
        key=lambda e: (_CYCLE_STATUS_RANK.get(e["cycle"].status, 9), e["cycle"].number),
    )
    sections = [
        {
            "key": str(entry["cycle"].id),
            "label": entry["cycle"].display_name,
            "tone": "violet" if entry["cycle"].status == "active" else "zinc",
            "tasks": entry["tasks"],
        }
        for entry in ordered
    ]
    sections.append(
        {"key": "backlog", "label": _("Backlog"), "tone": "zinc", "tasks": backlog},
    )
    return sections


def _group_by_milestone(tasks):
    """Bucket by milestone, soonest date first, no-milestone last.

    A milestone is a date, so the sections run in date order rather than
    alphabetically: the question this axis answers is "what is due
    next", and a name tells you nothing about that. Overdue milestones
    therefore lead, which is where attention belongs. Work committed to
    nothing collects in one trailing bucket instead of vanishing — on
    most boards it is the majority.

    Args:
        tasks: Iterable of :class:`Task`.

    Returns:
        Ordered section dicts.
    """
    by_milestone = {}
    loose = []
    for task in tasks:
        if task.milestone_id is None:
            loose.append(task)
            continue
        by_milestone.setdefault(task.milestone_id, {"milestone": task.milestone, "tasks": []})
        by_milestone[task.milestone_id]["tasks"].append(task)
    ordered = sorted(
        by_milestone.values(),
        key=lambda entry: (entry["milestone"].target_date, entry["milestone"].id),
    )
    sections = [
        {
            "key": str(entry["milestone"].id),
            "label": entry["milestone"].name,
            # Closed is settled; the rest is still a promise.
            "tone": "zinc" if entry["milestone"].is_closed else "violet",
            # The date the section is named after, and how much of what
            # sits under it already runs past that date. A heading that
            # says only "Beta · 12" hides the one fact worth acting on.
            "sub": date_format(entry["milestone"].target_date, "M j"),
            "risk": sum(1 for task in entry["tasks"] if late_days(task) > 0),
            "tasks": entry["tasks"],
        }
        for entry in ordered
    ]
    sections.append(
        {
            "key": "none",
            "label": str(_("No milestone")),
            "tone": "zinc",
            "tasks": loose,
        },
    )
    return sections


def _group_by_epic(tasks):
    """Bucket by the epic a task belongs to, alphabetical, no-epic last.

    This is the axis that answers "what is this effort made of" without
    leaving the list: an epic reaches across projects, so grouping by it
    puts work together that every other axis separates. Tasks outside any
    epic fall into one bucket at the end rather than disappearing — on a
    board where most work has no epic, that bucket is the majority.

    Args:
        tasks: Iterable of :class:`Task`.

    Returns:
        Ordered section dicts.
    """
    by_epic = {}
    loose = []
    for task in tasks:
        if task.epic_id is None:
            loose.append(task)
            continue
        by_epic.setdefault(task.epic_id, {"epic": task.epic, "tasks": []})
        by_epic[task.epic_id]["tasks"].append(task)
    ordered = sorted(by_epic.values(), key=lambda e: e["epic"].title.lower())
    sections = [
        {
            "key": str(entry["epic"].id),
            "label": entry["epic"].title,
            "tone": "zinc",
            "tasks": entry["tasks"],
        }
        for entry in ordered
    ]
    sections.append(
        {
            "key": "none",
            "label": str(_("No epic")),
            "tone": "zinc",
            "tasks": loose,
        },
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
