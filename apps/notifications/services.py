from __future__ import annotations

from typing import Any, Iterable

from django.db import transaction

from .models import Notification

_COMMENT_PREVIEW_CHARS = 280

_TASK_EVENT_KINDS = {
    "task.assigned": Notification.Kind.ASSIGNED,
    "task.status_changed": Notification.Kind.STATUS_CHANGE,
    "task.priority_changed": Notification.Kind.PRIORITY_CHANGE,
    "task.due_changed": Notification.Kind.DUE,
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
    notification = Notification.objects.create(
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
    _broadcast_notification(notification)
    return notification


def _unread_count(recipient_id: int) -> int:
    """Return the recipient's active unread notification count.

    Args:
        recipient_id: The recipient user id.

    Returns:
        Count of non-archived, unread notifications.
    """
    return Notification.objects.filter(
        recipient_id=recipient_id,
        archived_at__isnull=True,
        is_read=False,
    ).count()


def _broadcast_notification(notification: Notification) -> None:
    """Push a ``notification.created`` event to the recipient's SSE channel.

    Queued on ``transaction.on_commit`` so a rolled-back request never
    emits a phantom notification. The payload carries pre-rendered row +
    badge HTML so the browser updates the inbox and the sidebar badge
    with no extra round-trips (mirrors ``broadcast_task_events``). The
    per-user ``user-<id>`` channel is private — only the recipient may
    read it (see ``apps.workspaces.sse.WorkspaceChannelManager``), so no
    self-filter is needed: notifications never reach their own actor.

    Args:
        notification: The freshly created :class:`Notification`.
    """
    recipient_id = notification.recipient_id
    pk = notification.pk

    def _send() -> None:
        from django.template.loader import render_to_string

        import django_eventstream

        row = Notification.objects.select_related("task__project", "actor", "comment").filter(pk=pk).first()
        if row is None:
            return
        unread = _unread_count(recipient_id)
        payload = {
            "kind": row.kind,
            "unread": unread,
            "row_html": render_to_string("web/_notification_row.html", {"n": row}),
            "badge_html": render_to_string("web/_inbox_badge.html", {"inbox_unread": unread}),
        }
        django_eventstream.send_event(f"user-{recipient_id}", "notification.created", payload)

    transaction.on_commit(_send)


def notify_for_task_diff(*, events: Iterable, task, actor) -> None:
    """Fan a task's diff events out to per-user inbox notifications.

    Called from :func:`apps.tasks.events.emit_task_diff_events`, the one
    path every single-task edit (web inline edit, DRF viewset, MCP
    tools) funnels through. Only the watched kinds in
    ``_TASK_EVENT_KINDS`` produce notifications; the rest (labels, due,
    parent, text edits) are intentionally skipped — see ADR 0021.

    Recipients:
        * ``task.assigned`` → both the new assignee (``to_user_id``) and
          the previous one (``from_user_id``) — the latter so a person
          learns a task was taken off their plate. The actor is still
          dropped by :func:`notify`'s self-suppression.
        * ``task.status_changed`` / ``task.priority_changed`` /
          ``task.due_changed`` → the task's current assignee and reporter.

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
            payload = event.payload or {}
            recipients = {payload.get("to_user_id"), payload.get("from_user_id")}
            recipients.discard(None)
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
