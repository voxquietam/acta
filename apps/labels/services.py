"""Shared label-fetch helpers used by every label picker / filter in the UI.

Pickers (create-task modal, task detail dropdown, task / bulk context
menus) and the filter sidebar all want the same shape: labels organised
into groups, each group keeping its admin-set order, with an "Ungrouped"
trailing bucket for labels that don't belong to one. Until this helper
existed every surface re-derived that grouping inline; consolidating
keeps the surfaces consistent — same group ordering, same intra-group
ordering, same hiding rules — and stops the model knowledge leaking
into templates.

Two variants live here on purpose:

* :func:`grouped_labels` — for pickers / filter; derived from one
  ``Label`` query with ``select_related("group")``. Empty groups don't
  appear (no labels = nothing to pick). One query total.
* :func:`grouped_labels_with_empty` — for the management UI on workspace
  settings; needs every group to show even when it has zero labels so
  the team can add the first one. Two queries (Labels + LabelGroups).
"""

from __future__ import annotations

from typing import TypedDict

from apps.labels.models import Label, LabelGroup
from apps.tasks.models import Task


class GroupedLabelEntry(TypedDict):
    """One row in the ``grouped_labels`` result.

    ``group`` is ``None`` for the Ungrouped bucket so templates can branch
    on the falsy value without checking a string sentinel.
    """

    group: LabelGroup | None
    labels: list[Label]


def grouped_labels(workspace) -> list[GroupedLabelEntry]:
    """Return ``workspace``'s populated label buckets in picker order.

    Groups come out alphabetical by name; labels within a group come out
    by ``position`` then ``name`` — same order the management UI uses. The
    Ungrouped bucket trails everything else and is only included when it
    actually has labels (or the workspace has no groups at all, so the
    picker still has a single header-less bucket to render).

    Single query: ``Label`` joined with its ``group``. Empty groups are
    intentionally omitted — they'd be dead headers in a picker.

    Args:
        workspace: The :class:`~apps.workspaces.models.Workspace` to load
            labels for. Cross-workspace labels never appear.

    Returns:
        Ordered list of ``{"group", "labels"}`` entries.
    """
    labels = list(
        Label.objects.filter(workspace=workspace).select_related("group").order_by("position", "name"),
    )
    by_group_id: dict[int | None, dict] = {}
    for label in labels:
        gid = label.group_id
        if gid not in by_group_id:
            by_group_id[gid] = {"group": label.group, "labels": []}
        by_group_id[gid]["labels"].append(label)
    # Order: named groups alphabetical, then Ungrouped (if present).
    named = sorted(
        (entry for gid, entry in by_group_id.items() if gid is not None),
        key=lambda e: e["group"].name.lower(),
    )
    entries: list[GroupedLabelEntry] = list(named)
    if None in by_group_id:
        entries.append(by_group_id[None])
    elif not entries:
        entries.append({"group": None, "labels": []})
    return entries


def grouped_labels_with_empty(workspace) -> list[GroupedLabelEntry]:
    """Like :func:`grouped_labels` but keeps groups that have no labels.

    Used by the labels-management card on workspace settings, where the
    team needs every group visible so they can drop a first label into
    it. Pays one extra query (``LabelGroup``) compared to the picker
    helper — fine on the settings page, not on every task surface.
    """
    labels = list(
        Label.objects.filter(workspace=workspace).select_related("group").order_by("position", "name"),
    )
    by_group_id: dict[int | None, list[Label]] = {}
    for label in labels:
        by_group_id.setdefault(label.group_id, []).append(label)
    entries: list[GroupedLabelEntry] = []
    for group in LabelGroup.objects.filter(workspace=workspace).order_by("name"):
        entries.append({"group": group, "labels": by_group_id.get(group.id, [])})
    ungrouped = by_group_id.get(None, [])
    if ungrouped or not entries:
        entries.append({"group": None, "labels": ungrouped})
    return entries


def flat_labels(workspace) -> list[Label]:
    """Return the workspace's labels in picker order (``position``, ``name``).

    Convenience for surfaces that still want a flat list but the same
    ordering the grouped picker uses — keeps single-label dropdowns
    matching the management UI without forcing every caller to flatten
    :func:`grouped_labels` themselves.
    """
    return [label for entry in grouped_labels(workspace) for label in entry["labels"]]


# -----------------------------------------------------------------------------
# Exclusive-group enforcement
# -----------------------------------------------------------------------------
#
# ``LabelGroup.is_exclusive=True`` means "at most one label from this group
# applies to a task". Two helpers enforce this without each call site having
# to redo the lookup:
#
# * :func:`add_labels_to_tasks` — adds labels to N tasks. For every added
#   label from an exclusive group, drops the sibling labels of that group
#   from each task before attaching the new one (just-added wins). Used by
#   the per-task toggle and the bulk endpoint.
# * :func:`trim_exclusive_conflicts` — pure-Python pass that keeps only the
#   first label of each exclusive group in a list. Used at task-create
#   time, where the form lets users tick several pills from the same
#   group; we silently keep the first and drop the rest.


def add_labels_to_tasks(task_ids, added_label_ids) -> None:
    """Attach ``added_label_ids`` to every task in ``task_ids`` atomically.

    Bypasses Django's per-row M2M descriptor (one query per task) by writing
    straight to the through table — same shape the bulk endpoint already
    used. Before the attach, any sibling label from an exclusive group is
    detached from each task so the just-added label is the only survivor in
    that group.

    Args:
        task_ids: PKs of the tasks to attach to.
        added_label_ids: PKs of the labels to attach.

    Returns:
        None. M2M ``task.labels`` rows reflect the new state on commit.
    """
    if not task_ids or not added_label_ids:
        return
    through = Task.labels.through
    # Look up which added labels live in an exclusive group; one query.
    exclusive_groups = list(
        LabelGroup.objects.filter(
            is_exclusive=True,
            labels__pk__in=list(added_label_ids),
        )
        .distinct()
        .values_list("id", flat=True),
    )
    if exclusive_groups:
        # Every label in those groups EXCEPT the ones we're adding becomes
        # a "sibling" and must be detached from each affected task.
        sibling_ids = list(
            Label.objects.filter(group_id__in=exclusive_groups)
            .exclude(pk__in=list(added_label_ids))
            .values_list("id", flat=True),
        )
        if sibling_ids:
            through.objects.filter(
                task_id__in=list(task_ids),
                label_id__in=sibling_ids,
            ).delete()
    through.objects.bulk_create(
        [through(task_id=tid, label_id=lid) for tid in task_ids for lid in added_label_ids],
        ignore_conflicts=True,
    )


def trim_exclusive_conflicts(label_ids) -> list[int]:
    """Return ``label_ids`` with at most one label per exclusive group.

    For initial label assignment at task-create time: the form lets the
    user tick several pills at once, including two from the same exclusive
    group (the UI hasn't enforced single-select yet). Keep the first one we
    see (lowest ``position``, then alphabetical name) and drop the rest so
    the freshly-created task lands in a consistent state.

    Args:
        label_ids: Iterable of label PKs (typically straight from the
            create-task form).

    Returns:
        A list of PKs preserving input order *for non-exclusive labels*
        and emitting only the first hit per exclusive group.
    """
    ids = [int(i) for i in label_ids]
    if not ids:
        return []
    labels = {label.pk: label for label in Label.objects.filter(pk__in=ids).select_related("group")}
    seen_exclusive_groups: set[int] = set()
    out: list[int] = []
    for raw_id in ids:
        label = labels.get(raw_id)
        if label is None:
            continue
        if label.group and label.group.is_exclusive:
            if label.group_id in seen_exclusive_groups:
                continue
            seen_exclusive_groups.add(label.group_id)
        out.append(label.pk)
    return out
