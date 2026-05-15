"""Diff-based activity event emission for :class:`Task` mutations.

The functions here are the single chokepoint that turns a before/after
task pair into a stream of granular :class:`ActivityLog` rows.
Per docs/decisions/0011-activity-log.md, the activity log uses a fixed
set of granular event types for watched fields (status, assignee,
due_date, priority, labels, parent) plus a catch-all ``task.updated``
for the remaining text/size edits. All events from a single bulk
operation share a ``bulk_id`` so the UI can collapse them in the feed.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from apps.activity.models import ActivityLog
from apps.activity.services import log_event

from .models import Task

# Fields that get their own event_type, recognized by anyone consuming
# the activity feed (UI filters, future webhooks).
WATCHED_EVENT_FIELDS = (
    "status",
    "assignee",
    "due_date",
    "priority",
    "parent",
    "labels",
)


def snapshot_task(task: Task) -> dict[str, Any]:
    """Capture the diff-relevant fields of a task before mutation.

    Called with a freshly-loaded instance whose ``.labels`` M2M has not
    been touched in this transaction yet. The returned dict is used as
    the ``old_state`` argument to :func:`emit_task_diff_events`.

    Args:
        task: The :class:`Task` instance to snapshot.

    Returns:
        A dict containing the previous values of every diff-tracked
        attribute, including a list of label IDs.
    """
    return {
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "size": task.size,
        "due_date": task.due_date,
        "assignee_id": task.assignee_id,
        "parent_id": task.parent_id,
        "labels_ids": list(task.labels.values_list("id", flat=True)),
    }


def _iso_or_none(value):
    """Return an ISO-format date string for ``value`` or ``None``.

    Args:
        value: A :class:`datetime.date` instance or ``None``.

    Returns:
        The ISO 8601 date string, or ``None`` if input was falsy.
    """
    return value.isoformat() if value else None


def emit_task_diff_events(
    *,
    old_state: dict[str, Any],
    task: Task,
    actor,
    bulk_id: UUID | None = None,
) -> int:
    """Emit one ``ActivityLog`` row per changed watched field on a task.

    Compares the pre-save ``old_state`` to the freshly-saved ``task`` and
    emits the appropriate ``task.*`` events. Unchanged fields produce no
    events. Multiple field changes on the same task produce multiple
    events that share the same ``bulk_id``.

    Args:
        old_state: Dict produced by :func:`snapshot_task` before the
            mutation.
        task: The :class:`Task` instance after ``save()`` completed and
            after any M2M operations on ``labels`` ran.
        actor: The :class:`User` who performed the change. Set from
            ``request.user`` in the view layer.
        bulk_id: Shared UUID for events emitted from a bulk operation.
            ``None`` for single-task edits.

    Returns:
        The number of activity log rows written for this diff.
    """
    workspace = task.project.workspace
    project = task.project
    common = {
        "workspace": workspace,
        "project": project,
        "actor": actor,
        "target_type": ActivityLog.TARGET_TASK,
        "target_id": task.id,
        "bulk_id": bulk_id,
    }
    count = 0

    if old_state["status"] != task.status:
        log_event(
            event_type="task.status_changed",
            payload={"from": old_state["status"], "to": task.status},
            **common,
        )
        count += 1

    if old_state["assignee_id"] != task.assignee_id:
        log_event(
            event_type="task.assigned",
            payload={
                "from_user_id": old_state["assignee_id"],
                "to_user_id": task.assignee_id,
            },
            **common,
        )
        count += 1

    if old_state["due_date"] != task.due_date:
        log_event(
            event_type="task.due_changed",
            payload={
                "from": _iso_or_none(old_state["due_date"]),
                "to": _iso_or_none(task.due_date),
            },
            **common,
        )
        count += 1

    if old_state["priority"] != task.priority:
        log_event(
            event_type="task.priority_changed",
            payload={"from": old_state["priority"], "to": task.priority},
            **common,
        )
        count += 1

    if old_state["parent_id"] != task.parent_id:
        log_event(
            event_type="task.parent_changed",
            payload={
                "from_task_id": old_state["parent_id"],
                "to_task_id": task.parent_id,
            },
            **common,
        )
        count += 1

    old_labels = set(old_state.get("labels_ids") or [])
    new_labels = set(task.labels.values_list("id", flat=True))
    added = sorted(new_labels - old_labels)
    removed = sorted(old_labels - new_labels)
    if added or removed:
        log_event(
            event_type="task.labels_changed",
            payload={"added_ids": added, "removed_ids": removed},
            **common,
        )
        count += 1

    # Catch-all for remaining text/size edits.
    changes: dict[str, dict[str, Any]] = {}
    if old_state["title"] != task.title:
        changes["title"] = {"old": old_state["title"], "new": task.title}
    if old_state["description"] != task.description:
        changes["description"] = {
            "old_len": len(old_state["description"] or ""),
            "new_len": len(task.description or ""),
        }
    if old_state["size"] != task.size:
        changes["size"] = {"old": old_state["size"], "new": task.size}
    if changes:
        log_event(
            event_type="task.updated",
            payload={"changes": changes},
            **common,
        )
        count += 1

    return count
