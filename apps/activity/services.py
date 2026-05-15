"""Activity log writers.

Per docs/decisions/0011-activity-log.md, :func:`log_event` is the single
entry point for writing activity rows. It is always called from the view
layer where ``request.user`` is available — never from signals — and runs
inside the same DB transaction as the change it records.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .models import ActivityLog


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
    """Write a single activity log row.

    The caller is responsible for the surrounding ``transaction.atomic()``
    block so the event commits together with the change it describes. For
    SSE broadcast, the caller wraps the post-commit hook (see Stage 6).

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
    return ActivityLog.objects.create(
        workspace=workspace,
        project=project,
        target_type=target_type,
        target_id=target_id,
        actor=actor,
        event_type=event_type,
        payload=payload or {},
        bulk_id=bulk_id,
    )
