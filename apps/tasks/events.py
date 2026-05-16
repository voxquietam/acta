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

from django.db import transaction
from django.template.loader import render_to_string

from apps.activity.models import ActivityLog
from apps.activity.services import broadcast_event

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
    the ``old_state`` argument to :func:`build_diff_events`. Reads
    labels via ``.all()`` (not ``.values_list``) so that any
    ``prefetch_related("labels")`` on the source queryset is honoured
    instead of triggering a fresh query per task.

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
        "project_id": task.project_id,
        "number": task.number,
        "labels_ids": [label.id for label in task.labels.all()],
    }


def _iso_or_none(value):
    """Return an ISO-format date string for ``value`` or ``None``.

    Args:
        value: A :class:`datetime.date` instance or ``None``.

    Returns:
        The ISO 8601 date string, or ``None`` if input was falsy.
    """
    return value.isoformat() if value else None


def build_diff_events(
    *,
    old_state: dict[str, Any],
    task: Task,
    actor,
    bulk_id: UUID | None = None,
) -> list[ActivityLog]:
    """Compute one ``ActivityLog`` instance per changed watched field.

    Pure builder: does not write to the database. Caller is expected to
    persist the returned list via :func:`emit_task_diff_events` (single
    diff) or :meth:`ActivityLog.objects.bulk_create` (many diffs across
    a bulk operation, to amortize INSERT cost).

    Args:
        old_state: Dict produced by :func:`snapshot_task` before the
            mutation.
        task: The :class:`Task` after ``save()`` and after any M2M
            mutations on ``labels`` have committed.
        actor: The :class:`User` who performed the change. Set from
            ``request.user`` in the view layer.
        bulk_id: Shared UUID for events emitted from a bulk operation.
            ``None`` for single-task edits.

    Returns:
        A list of unsaved :class:`ActivityLog` instances, one per
        changed watched field plus a catch-all ``task.updated`` for
        text/size edits.
    """
    workspace = task.project.workspace
    project = task.project
    common = dict(
        workspace=workspace,
        project=project,
        actor=actor,
        target_type=ActivityLog.TARGET_TASK,
        target_id=task.id,
        bulk_id=bulk_id,
    )
    events: list[ActivityLog] = []

    if old_state["status"] != task.status:
        events.append(
            ActivityLog(
                event_type="task.status_changed",
                payload={"from": old_state["status"], "to": task.status},
                **common,
            ),
        )

    if old_state["assignee_id"] != task.assignee_id:
        events.append(
            ActivityLog(
                event_type="task.assigned",
                payload={
                    "from_user_id": old_state["assignee_id"],
                    "to_user_id": task.assignee_id,
                },
                **common,
            ),
        )

    if old_state["due_date"] != task.due_date:
        events.append(
            ActivityLog(
                event_type="task.due_changed",
                payload={
                    "from": _iso_or_none(old_state["due_date"]),
                    "to": _iso_or_none(task.due_date),
                },
                **common,
            ),
        )

    if old_state["priority"] != task.priority:
        events.append(
            ActivityLog(
                event_type="task.priority_changed",
                payload={"from": old_state["priority"], "to": task.priority},
                **common,
            ),
        )

    if old_state["parent_id"] != task.parent_id:
        events.append(
            ActivityLog(
                event_type="task.parent_changed",
                payload={
                    "from_task_id": old_state["parent_id"],
                    "to_task_id": task.parent_id,
                },
                **common,
            ),
        )

    old_labels = set(old_state.get("labels_ids") or [])
    new_labels = {label.id for label in task.labels.all()}
    added = sorted(new_labels - old_labels)
    removed = sorted(old_labels - new_labels)
    if added or removed:
        events.append(
            ActivityLog(
                event_type="task.labels_changed",
                payload={"added_ids": added, "removed_ids": removed},
                **common,
            ),
        )

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
    if old_state.get("project_id") != task.project_id:
        changes["project"] = {"old": old_state.get("project_id"), "new": task.project_id}
    if old_state.get("number") != task.number:
        changes["number"] = {"old": old_state.get("number"), "new": task.number}
    if changes:
        events.append(
            ActivityLog(
                event_type="task.updated",
                payload={"changes": changes},
                **common,
            ),
        )

    return events


def broadcast_task_events(events: list[ActivityLog], tasks_by_id: dict[int, Task], actor) -> None:
    """Queue SSE broadcasts for a batch of just-persisted activity rows.

    Shared by the single-task path (:func:`emit_task_diff_events`) and
    the bulk endpoint (``apps.tasks.bulk._run_bulk_update`` and
    ``_run_bulk_delete``). Each event reaches the workspace SSE
    channel via :func:`apps.activity.services.broadcast_event`. For
    events whose ``target_id`` is in ``tasks_by_id`` the broadcast
    payload carries pre-rendered card HTML so connected kanban
    clients swap in place; deletion events omit ``card_html`` (the
    task is gone) and clients remove the card.

    Args:
        events: The :class:`ActivityLog` rows about to be broadcast.
            Each row's ``workspace_id`` decides which channel it lands on.
        tasks_by_id: Mapping of task pk → fresh :class:`Task` with
            ``select_related('project', 'assignee')`` and
            ``prefetch_related('labels')`` so the card template
            renders without extra queries.
        actor: The acting :class:`User`. Embedded in every payload as
            ``actor_id`` for client-side self-event filtering.
    """
    if not events:
        return
    actor_id = actor.id if actor else None
    card_html_by_task: dict[int, str] = {}
    priority_labels = dict(Task.PRIORITY_CHOICES)
    for task_id, task in tasks_by_id.items():
        card_html_by_task[task_id] = render_to_string(
            "web/projects/_task_card.html",
            {"task": task, "priority_labels": priority_labels},
        )
    for ev in events:
        workspace_id = ev.workspace_id
        payload = {
            "target_type": ev.target_type,
            "target_id": ev.target_id,
            "project_id": ev.project_id,
            "bulk_id": str(ev.bulk_id) if ev.bulk_id else None,
            **(ev.payload or {}),
        }
        card_html = card_html_by_task.get(ev.target_id)
        if card_html:
            payload["card_html"] = card_html
        event_type = ev.event_type
        transaction.on_commit(
            lambda wid=workspace_id, et=event_type, p=payload, aid=actor_id: broadcast_event(wid, et, p, aid),
        )


def emit_task_diff_events(
    *,
    old_state: dict[str, Any],
    task: Task,
    actor,
    bulk_id: UUID | None = None,
) -> int:
    """Build and persist diff events for a single task in one INSERT.

    Thin wrapper over :func:`build_diff_events` that calls
    ``ActivityLog.objects.bulk_create`` so all events from one diff
    commit in a single SQL statement (versus one INSERT per event).
    Also fans the events out to the workspace SSE stream via
    :func:`broadcast_task_events`.

    Args:
        old_state: Dict produced by :func:`snapshot_task` before the
            mutation.
        task: The :class:`Task` instance after the mutation.
        actor: The :class:`User` who performed the change.
        bulk_id: Shared UUID for events from a bulk operation. ``None``
            for single-task edits.

    Returns:
        The number of activity log rows written for this diff.
    """
    events = build_diff_events(old_state=old_state, task=task, actor=actor, bulk_id=bulk_id)
    if events:
        ActivityLog.objects.bulk_create(events)
        task_for_render = Task.objects.select_related("project", "assignee").prefetch_related("labels").get(pk=task.pk)
        broadcast_task_events(events, {task.pk: task_for_render}, actor)
    return len(events)
