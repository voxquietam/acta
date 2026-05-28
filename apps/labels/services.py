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
