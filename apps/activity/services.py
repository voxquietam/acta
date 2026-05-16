"""Activity log writers.

Per docs/decisions/0011-activity-log.md, :func:`log_event` is the single
entry point for writing activity rows. It is always called from the view
layer where ``request.user`` is available — never from signals — and runs
inside the same DB transaction as the change it records.

After the surrounding transaction commits, the same call also pushes the
event onto the workspace SSE stream (see ADR 0015). Broadcast is hooked
on ``transaction.on_commit`` so a rollback never produces a phantom event.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db import transaction

import django_eventstream

from .models import ActivityLog


def broadcast_event(workspace_id: int, event_type: str, payload: dict[str, Any], actor_id: int | None) -> None:
    """Push one event to the workspace's SSE stream.

    Public helper used both internally by :func:`log_event` and by
    callers that bulk-persist activity rows themselves (the bulk
    endpoint in ``apps.tasks.bulk`` and the diff-event batcher in
    ``apps.tasks.events``). The payload carries ``actor_id`` so
    connected clients can ignore events they triggered themselves
    (they already updated their UI optimistically from the original
    HTTP response).

    Args:
        workspace_id: Channel partition. Maps 1:1 to the
            ``workspace-<id>`` SSE channel.
        event_type: Same string as the underlying
            ``ActivityLog.event_type`` (see ADR 0011).
        payload: Event-specific dict, JSON-serialised by
            django_eventstream.
        actor_id: User id of the originator, or ``None`` for system
            events. Embedded into the broadcast payload as-is.
    """
    django_eventstream.send_event(
        f"workspace-{workspace_id}",
        event_type,
        {**payload, "actor_id": actor_id},
    )


def log_event(
    *,
    workspace,
    actor,
    event_type: str,
    target_type: str,
    target_id: int,
    payload: dict[str, Any] | None = None,
    project=None,
    bulk_id: UUID | None = None,
) -> ActivityLog:
    """Write a single activity log row and broadcast it on commit.

    The caller is responsible for the surrounding ``transaction.atomic()``
    block so the event commits together with the change it describes.
    SSE broadcast is queued via ``transaction.on_commit`` — a rolled-back
    transaction never reaches the stream.

    Args:
        workspace: The :class:`Workspace` the event belongs to. Required so
            the workspace feed query stays index-only.
        actor: The :class:`User` who performed the action, taken from
            ``request.user``. ``None`` for system-initiated events.
        event_type: Event category in ``{owner_target_type}.{verb}``
            form, e.g. ``"task.status_changed"``.
        target_type: One of the ``ActivityLog.TARGET_*`` constants:
            ``"task"``, ``"comment"``, ``"project"``, ``"workspace"``,
            ``"member"``.
        target_id: Numeric ID of the target object. Not a foreign key —
            the row survives deletion of the target.
        payload: Event-specific JSON-serializable details (diff, denormalized
            snapshot, metadata). Defaults to ``{}``.
        project: Optional :class:`Project` for project-scoped events. Null
            for workspace- or member-level events.
        bulk_id: UUID shared across all events emitted from one bulk
            endpoint call. Allows UI to group ``N`` events into a single
            feed entry while keeping per-task timelines intact.

    Returns:
        The newly created :class:`ActivityLog` instance.
    """
    row = ActivityLog.objects.create(
        workspace=workspace,
        project=project,
        target_type=target_type,
        target_id=target_id,
        actor=actor,
        event_type=event_type,
        payload=payload or {},
        bulk_id=bulk_id,
    )
    broadcast_payload = {
        "activity_id": row.id,
        "target_type": target_type,
        "target_id": target_id,
        "project_id": project.id if project else None,
        "bulk_id": str(bulk_id) if bulk_id else None,
        "occurred_at": row.created_at.isoformat() if row.created_at else None,
        **(payload or {}),
    }
    actor_id = actor.id if actor else None
    transaction.on_commit(
        lambda: broadcast_event(workspace.id, event_type, broadcast_payload, actor_id),
    )
    return row
