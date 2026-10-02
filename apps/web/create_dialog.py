"""The payload the create-task rail renders from.

The create dialog is two columns (design "Create Task Rethink", artboard
1a): content on the left, a 280px property rail on the right, so the
modal and the task-detail page read as one interface. Every property is
a row, and an unset one says "Add" rather than hiding.

Eleven rows of options cannot be markup. A workspace with two hundred
labels would otherwise render two hundred rows into every dialog open,
nearly all of them never looked at, and the same again for members. So
the rail ships as one ``json_script`` and an Alpine component draws the
row that is open — the same split the filter dock uses (see
:mod:`apps.web.filters`).

The dialog stays a projection over the real ``<form>``: the component
writes hidden inputs under the field names the POST handler already
parses (``status`` / ``priority`` / ``size`` / ``due_date`` /
``assignee`` / ``cycle`` / ``labels``), so nothing server-side had to
learn the new layout.
"""

from __future__ import annotations

from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from apps.tasks.models import Task
from apps.web.templatetags.web_extras import priority_text_classes, status_dot_classes

# Rows in rail order, grouped the way the task page groups them. The
# heading travels with the first row of its group rather than as its own
# payload entry — the rail is a flat list and a group is a label on a
# row, which keeps the Alpine side free of nesting.
GROUP_PROPERTIES = _("Properties")
GROUP_SCHEDULE = _("Schedule")
GROUP_RELATIONS = _("Relations")
GROUP_CONTEXT = _("Context")

# Priority 1..4 keep the chevron the rest of the app draws for them;
# "no priority" is the dashed circle the table cell uses.
PRIORITY_ICONS = {
    1: "chevrons-up",
    2: "chevron-up",
    3: "minus",
    4: "chevron-down",
    0: "circle-dashed",
}


def _status_options(selected):
    """Status rows for the rail popover.

    ``cancelled`` is deliberately absent: it is the terminal "won't do"
    state and a task is not born in it (same rule the old ``<select>``
    enforced).

    Args:
        selected: The currently picked status key.

    Returns:
        A list of option dicts for the Alpine popover.
    """
    return [
        {
            "v": value,
            "n": str(label),
            "cls": status_dot_classes(value),
            "on": value == selected,
        }
        for value, label in Task.STATUS_LABELS.items()
        if value != Task.STATUS_CANCELLED
    ]


def _priority_options(selected):
    """Priority rows, chevron and colour matching every other surface.

    Args:
        selected: The currently picked priority integer.

    Returns:
        A list of option dicts for the Alpine popover.
    """
    return [
        {
            "v": str(value),
            "n": str(label),
            "icon": PRIORITY_ICONS.get(value, "circle-dashed"),
            "fg": priority_text_classes(value),
            "empty": value == Task.NO_PRIORITY,
            "on": value == selected,
        }
        for value, label in Task.PRIORITY_CHOICES
    ]


def _size_options(selected):
    """Size rows — the Fibonacci scale plus "no size".

    Args:
        selected: The currently picked size, or ``None``.

    Returns:
        A list of option dicts for the Alpine popover.
    """
    options = [
        {
            "v": "",
            "n": str(_("No size")),
            "empty": True,
            "on": selected is None,
        },
    ]
    options.extend(
        {
            "v": str(value),
            "n": str(value),
            "on": value == selected,
        }
        for value in Task.SIZE_VALUES
    )
    return options


def _member_options(members, selected_id):
    """Assignee rows carrying whatever the avatar needs to draw itself.

    The avatar partial decides photo-versus-initial for every other
    surface, but a popover row is drawn by Alpine from JSON, so the
    choice is made here: ``img`` when there is a photo, else the
    deterministic colour and the initial.

    Args:
        members: Users in the selected project's workspace.
        selected_id: The currently picked assignee id, or ``None``.

    Returns:
        A list of option dicts for the Alpine popover, "Unassigned"
        first.
    """
    options = [
        {
            "v": "",
            "n": str(_("Unassigned")),
            "empty": True,
            "on": selected_id is None,
        },
    ]
    for user in members:
        name = user.display_name or user.username
        options.append(
            {
                "v": str(user.pk),
                "n": name,
                "img": (
                    f"{reverse('accounts:serve_avatar', kwargs={'user_id': user.pk})}"
                    f"?v={user.avatar_version}&size=64"
                    if user.avatar
                    else None
                ),
                "ini": name[:1].upper(),
                "bg": user.avatar_color,
                "on": user.pk == selected_id,
            },
        )
    return options


def _label_options(label_groups, selected_ids):
    """Label rows, each tagged with the group it belongs to.

    Args:
        label_groups: ``grouped_labels()`` output for the workspace.
        selected_ids: Ids already picked.

    Returns:
        A list of option dicts for the Alpine popover.
    """
    options = []
    for entry in label_groups:
        group = entry["group"]
        for label in entry["labels"]:
            options.append(
                {
                    "v": str(label.id),
                    "n": label.name,
                    "c": label.color,
                    "g": group.name if group else "",
                    "on": label.id in selected_ids,
                },
            )
    return options


def _cycle_options(cycles, selected_id):
    """Cycle rows — active first, each showing its span.

    Args:
        cycles: ``_workspace_cycles()`` output (active first).
        selected_id: The currently picked cycle id, or ``""``.

    Returns:
        A list of option dicts for the Alpine popover.
    """
    options = [
        {
            "v": "",
            "n": str(_("Backlog (no cycle)")),
            "empty": True,
            "on": not selected_id,
        },
    ]
    for cycle in cycles:
        options.append(
            {
                "v": str(cycle.id),
                "n": cycle.display_name,
                "sub": f"{date_format(cycle.start_date, 'M j')} – {date_format(cycle.end_date, 'M j')}",
                "c": "#10b981" if cycle.is_active else "",
                "on": str(cycle.id) == str(selected_id),
            },
        )
    return options


def _repeat_options(selected):
    """Cadence rows for the Repeat popover.

    "Repeat" is not a field on the task — a :class:`RecurringTask` is a
    rule that spawns tasks — so these four presets name what
    ``rule_from_task`` builds. Anything finer is a trip to the Recurring
    page, which owns the full schedule.

    Args:
        selected: The currently picked preset key, or ``""``.

    Returns:
        A list of option dicts for the Alpine popover.
    """
    rows = [
        ("", _("Does not repeat")),
        ("daily", _("Every day")),
        ("weekly", _("Every week")),
        ("biweekly", _("Every 2 weeks")),
        ("monthly", _("Every month")),
    ]
    return [
        {
            "v": value,
            "n": str(label),
            "empty": value == "",
            "on": value == selected,
        }
        for value, label in rows
    ]


def _meeting_options(meetings, selected_id):
    """Recent meetings in the workspace, newest first.

    Shipped in the payload rather than behind an endpoint: a workspace
    logs a handful of calls a week, and the dialog only ever offers the
    recent ones — a task filed out of a call is filed right after it.

    Args:
        meetings: Recent :class:`~apps.meetings.models.Meeting` rows.
        selected_id: The currently picked meeting id, or ``""``.

    Returns:
        A list of option dicts for the Alpine popover.
    """
    options = [
        {
            "v": "",
            "n": str(_("No meeting")),
            "empty": True,
            "on": not selected_id,
        },
    ]
    for meeting in meetings:
        options.append(
            {
                "v": str(meeting.id),
                "n": meeting.title,
                "sub": date_format(timezone.localtime(meeting.happened_at), "M j"),
                "on": str(meeting.id) == str(selected_id),
            },
        )
    return options


def _project_groups(projects, selected):
    """Projects for the header combobox, grouped by workspace.

    Args:
        projects: Projects the user can file into, workspace-ordered.
        selected: The currently picked project, or ``None``.

    Returns:
        A list of ``{"n": workspace name, "items": [...]}`` groups.
    """
    groups: list[dict] = []
    for project in projects:
        name = project.workspace.name
        if not groups or groups[-1]["n"] != name:
            groups.append({"n": name, "items": []})
        groups[-1]["items"].append(
            {
                "v": project.slug_prefix,
                "n": project.name,
                "slug": project.slug_prefix,
                "icon": project.icon,
                "c": project.icon_color,
                "on": selected is not None and project.pk == selected.pk,
            },
        )
    return groups


def build_create_task_data(
    *,
    projects,
    selected_project,
    members,
    label_groups,
    workspace_cycles,
    pre_status,
    pre_priority,
    pre_size,
    pre_assignee_id,
    pre_label_ids,
    pre_due_date,
    pre_cycle_id,
    meetings,
    pre_parent,
    pre_epic,
    pre_links,
    pre_meeting_id,
    pre_repeat,
    kind,
):
    """Assemble the rail payload for one render of the create dialog.

    Every field carries its own options and its own current value, so the
    Alpine component needs no second source — and a field whose axis does
    not exist for this workspace (cycles, with cadence off) is simply
    absent rather than present and empty.

    Args:
        projects: Projects the user can file into.
        selected_project: The project the dialog is currently on.
        members: Users in that project's workspace.
        label_groups: ``grouped_labels()`` output for that workspace.
        workspace_cycles: Cycles for that workspace (empty when cadence
            is off).
        pre_status: Status key to start on.
        pre_priority: Priority integer to start on.
        pre_size: Size to start on, or ``None``.
        pre_assignee_id: Assignee id to start on, or ``None``.
        pre_label_ids: Label ids to start on.
        pre_due_date: ``YYYY-MM-DD`` to start on, or ``""``.
        pre_cycle_id: Cycle id to start on, or ``""``.
        meetings: Recent meetings in that workspace.
        pre_parent: ``{"v", "n", "cls"}`` for the parent, or ``None``.
        pre_epic: The same for the epic, or ``None``.
        pre_links: Rows for the links row, each ``{"v", "n", "kind"}``.
        pre_meeting_id: Meeting id to start on, or ``""``.
        pre_repeat: Repeat preset to start on, or ``""``.
        kind: ``task`` or ``epic`` — an epic drops the rows it derives.

    Returns:
        A JSON-serialisable dict with ``fields``, ``projects``,
        ``project`` and ``sprite``.
    """
    fields = [
        {
            "key": "status",
            "name": str(_("Status")),
            "icon": "circle-dot",
            "hotkey": "S",
            "input": "status",
            "clear": False,
            "group": str(GROUP_PROPERTIES),
            "options": _status_options(pre_status),
        },
        {
            "key": "priority",
            "name": str(_("Priority")),
            "icon": "flag",
            "hotkey": "P",
            "input": "priority",
            "options": _priority_options(pre_priority),
        },
        {
            "key": "size",
            "name": str(_("Size")),
            "icon": "ruler",
            "hotkey": "Z",
            "input": "size",
            "options": _size_options(pre_size),
        },
        {
            "key": "assignee",
            "name": str(_("Assignee")),
            "icon": "user",
            "hotkey": "A",
            "input": "assignee",
            "search": True,
            "options": _member_options(members, pre_assignee_id),
        },
        {
            "key": "labels",
            "name": str(_("Labels")),
            "icon": "tags",
            "hotkey": "L",
            "input": "labels",
            "multi": True,
            "search": True,
            "options": _label_options(label_groups, set(pre_label_ids)),
        },
        {
            "key": "due",
            "name": str(_("Due")),
            "icon": "flag-triangle-right",
            "hotkey": "D",
            "input": "due_date",
            "group": str(GROUP_SCHEDULE),
            "date": True,
            "value": pre_due_date,
        },
    ]
    if workspace_cycles:
        fields.append(
            {
                "key": "cycle",
                "name": str(_("Cycle")),
                "icon": "iteration-cw",
                "hotkey": "C",
                "input": "cycle",
                "options": _cycle_options(workspace_cycles, pre_cycle_id),
            },
        )
    fields.extend(
        [
            {
                "key": "repeat",
                "name": str(_("Repeat")),
                "icon": "repeat",
                "hotkey": "R",
                "input": "repeat",
                "options": _repeat_options(pre_repeat),
            },
            {
                # A parent is one task, searched rather than listed: a
                # project can hold a thousand of them.
                "key": "parent",
                "name": str(_("Parent")),
                "icon": "corner-left-up",
                "hotkey": "⇧P",
                "group": str(GROUP_RELATIONS),
                "input": "parent",
                "task": True,
                "value": pre_parent,
            },
            {
                # Separate from Parent, and deliberately so: a subtask
                # keeps its parent and may still belong to an epic, and
                # the epic may live in another project. See ADR 0036.
                "key": "epic",
                "name": str(_("Epic")),
                "icon": "square-kanban",
                "hotkey": "E",
                "input": "epic",
                "task": True,
                "value": pre_epic,
            },
            {
                # One picker for every kind of link: the type is a switch
                # above the search, so "blocked by" and "related" are two
                # answers to one question rather than two rows.
                "key": "links",
                "name": str(_("Links")),
                "icon": "link",
                "hotkey": "K",
                "links": True,
                "value": list(pre_links),
                # Order is the default: "related" is the link people reach
                # for, and a dependency is the deliberate one.
                "kinds": [
                    # ``icon`` and not ``i``: the sprite build scans this
                    # module for ``"icon": "…"``, and a name it cannot see
                    # renders as nothing.
                    {"v": "related", "n": str(_("Related")), "icon": "link"},
                    {"v": "blocked_by", "n": str(_("Blocked by")), "icon": "ban"},
                    {"v": "blocks", "n": str(_("Blocks")), "icon": "octagon-alert"},
                ],
            },
            {
                "key": "meeting",
                "name": str(_("Meeting")),
                "icon": "video",
                "hotkey": "M",
                "group": str(GROUP_CONTEXT),
                "input": "meeting",
                "search": True,
                "options": _meeting_options(meetings, pre_meeting_id),
            },
        ],
    )
    # A workspace that turned epics off gets no Epic row at all, rather
    # than one that refuses every pick.
    if selected_project is None or not selected_project.workspace.epics_enabled:
        fields = [f for f in fields if f["key"] != "epic"]
    if kind == Task.KIND_EPIC:
        # An epic takes its dates and size from its tasks, never joins a
        # cycle, is never a subtask and never belongs to another epic. A
        # row for any of those would be a pick the save throws away.
        dropped = {"due", "size", "cycle", "parent", "epic", "repeat"}
        fields = [f for f in fields if f["key"] not in dropped]
        # The group heading rides on the first row of its group, so it
        # has to move when that row is the one dropped.
        seen = set()
        for field in fields:
            group = field.get("group")
            if group:
                seen.add(group)
        if fields and not fields[0].get("group"):
            fields[0]["group"] = str(GROUP_PROPERTIES)
    groups = _project_groups(projects, selected_project)
    return {
        "fields": fields,
        "projects": groups,
        "project": next((item for group in groups for item in group["items"] if item["on"]), None),
        "kind": kind,
        "sprite": static("sprites/lucide.svg"),
        "url": reverse("web:create_task"),
        "search_url": reverse("web:create_task_search"),
        # Strings the Alpine side writes into rows it builds itself.
        # They travel in the payload rather than inside an ``x-text``
        # expression because a translation carrying an apostrophe would
        # end the attribute (see the note in ``_links_panel.html``).
        "text": {
            "add": str(_("Add")),
            "frozen": str(_("On leaving backlog")),
            "pick_project": str(_("Pick a project")),
            "none": str(_("Nothing found.")),
        },
    }
