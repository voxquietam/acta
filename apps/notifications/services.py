from __future__ import annotations

from typing import Any, Iterable

from .models import Notification

_COMMENT_PREVIEW_CHARS = 280

_TASK_EVENT_KINDS = {
    "task.assigned": Notification.Kind.ASSIGNED,
    "task.status_changed": Notification.Kind.STATUS_CHANGE,
    "task.priority_changed": Notification.Kind.PRIORITY_CHANGE,
}


def notify(
    *,
    recipient_id: int | None,
    actor,
    kind: str,
    workspace_id: int,
    task=None,
    comment=None,
    activity=None,
    preview: str = "",
    payload: dict[str, Any] | None = None,
) -> Notification | None:
    """Create one inbox notification, suppressing self-notifications.

    The single writer for :class:`apps.notifications.models.Notification`,
    mirroring the role :func:`apps.activity.services.log_event` plays for
    the activity log. Never notifies the actor about their own action —
    the same self-exclusion the SSE stream applies (ADR 0015/0017).

    Args:
        recipient_id: User id to deliver to. ``None`` is a no-op.
        actor: The :class:`User` who triggered the event, or ``None`` for
            system events.
        kind: One of :class:`Notification.Kind`.
        workspace_id: Workspace the notification is scoped to.
        task: The target :class:`Task`, if any.
        comment: The triggering :class:`Comment`, if any.
        activity: The source :class:`ActivityLog` row, if any.
        preview: Denormalized snippet for the inbox list.
        payload: Event-specific JSON details (status diff, tint, etc.).

    Returns:
        The created :class:`Notification`, or ``None`` when the row was
        suppressed (no recipient, or recipient is the actor).
    """
    if recipient_id is None:
        return None
    actor_id = actor.id if actor is not None else None
    if actor_id is not None and recipient_id == actor_id:
        return None
    return Notification.objects.create(
        recipient_id=recipient_id,
        actor=actor,
        workspace_id=workspace_id,
        kind=kind,
        task=task,
        comment=comment,
        activity=activity,
        preview=preview or "",
        payload=payload or {},
    )


def notify_for_task_diff(*, events: Iterable, task, actor) -> None:
    """Fan a task's diff events out to per-user inbox notifications.

    Called from :func:`apps.tasks.events.emit_task_diff_events`, the one
    path every single-task edit (web inline edit, DRF viewset, MCP
    tools) funnels through. Only the watched kinds in
    ``_TASK_EVENT_KINDS`` produce notifications; the rest (labels, due,
    parent, text edits) are intentionally skipped — see ADR 0021.

    Recipients:
        * ``task.assigned`` → the new assignee only (``to_user_id``).
        * ``task.status_changed`` / ``task.priority_changed`` → the task's
          current assignee and reporter.

    Args:
        events: The persisted :class:`ActivityLog` rows for this diff.
        task: The :class:`Task` after mutation. ``assignee_id`` /
            ``reporter_id`` are read without extra queries.
        actor: The :class:`User` who made the change.
    """
    involved = {task.assignee_id, task.reporter_id}
    involved.discard(None)
    for event in events:
        kind = _TASK_EVENT_KINDS.get(event.event_type)
        if kind is None:
            continue
        if event.event_type == "task.assigned":
            to_user_id = (event.payload or {}).get("to_user_id")
            recipients = {to_user_id} if to_user_id else set()
        else:
            recipients = set(involved)
        for recipient_id in recipients:
            notify(
                recipient_id=recipient_id,
                actor=actor,
                kind=kind,
                workspace_id=event.workspace_id,
                task=task,
                activity=event,
                preview=task.title,
                payload=event.payload or {},
            )


def notify_comment_created(*, comment, actor) -> None:
    """Fan a new comment out to the task's assignee and reporter.

    Called right after the ``comment.created`` activity event at every
    surface that posts comments (web, DRF, MCP). Mentions inside the body
    are a separate, later notification path; this only covers the
    "discussion progressed on a task I'm involved with" trigger.

    Args:
        comment: The freshly created :class:`Comment`.
        actor: The :class:`User` who wrote the comment.
    """
    task = comment.task
    involved = {task.assignee_id, task.reporter_id}
    involved.discard(None)
    if not involved:
        return
    workspace_id = task.project.workspace_id
    preview = (comment.body or "")[:_COMMENT_PREVIEW_CHARS]
    for recipient_id in involved:
        notify(
            recipient_id=recipient_id,
            actor=actor,
            kind=Notification.Kind.COMMENT,
            workspace_id=workspace_id,
            task=task,
            comment=comment,
            preview=preview,
        )
