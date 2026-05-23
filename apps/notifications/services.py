from __future__ import annotations

import re
from typing import Any, Iterable

from django.db import transaction

from .models import Notification

_COMMENT_PREVIEW_CHARS = 280


def _truncate_preview(body: str | None) -> str:
    """Cap a comment/update body for the stored preview, flagging truncation.

    Appends an ellipsis when the body exceeds :data:`_COMMENT_PREVIEW_CHARS`
    so every surface (inbox, Telegram) shows that the snippet was cut rather
    than ending mid-sentence with no signal.

    Args:
        body: The raw comment / update markdown body.

    Returns:
        The body unchanged when short enough, else the capped body plus ``…``.
    """
    body = body or ""
    if len(body) <= _COMMENT_PREVIEW_CHARS:
        return body
    return body[:_COMMENT_PREVIEW_CHARS].rstrip() + "…"


# Mention tokens are stored in Markdown as ``[@username](mention:<id>)``.
# Fan-out reads the user id straight from the token — no HTML parse.
_MENTION_TOKEN_RE = re.compile(r"\(mention:(\d+)\)")

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
    project_update=None,
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
        project_update: The target :class:`ProjectUpdate`, if any.
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
        project_update=project_update,
        preview=preview or "",
        payload=payload or {},
    )
    _broadcast_notification(notification)
    _mirror_to_telegram(notification)
    return notification


def _mirror_to_telegram(notification) -> None:
    """Fan a notification out to the recipient's Telegram chat, on commit.

    Best-effort: the Telegram send (a network call) is deferred to
    ``transaction.on_commit`` so a rolled-back transaction never DMs, and
    the lazy import keeps notifications → telegram a runtime edge.
    """

    def _send():
        from apps.telegram.services import notify_via_telegram

        notify_via_telegram(notification)

    transaction.on_commit(_send)


def _unread_count(recipient_id: int) -> int:
    """Return the recipient's active unread count for their active workspace.

    Scoped to the recipient's ``active_workspace`` and excludes
    ``PROJECT_UPDATE`` — matching the Notifications tab and the sidebar
    badge (see ``apps.web.views._inbox_base_qs`` /
    ``apps.web.context.workspace_nav``) so the live SSE badge stays
    consistent with a page reload. A notification for a workspace the
    recipient isn't currently in won't bump their badge.

    Args:
        recipient_id: The recipient user id.

    Returns:
        Count of non-archived, unread notifications in the recipient's
        active workspace (excluding project updates).
    """
    from django.contrib.auth import get_user_model

    active_id = get_user_model().objects.filter(pk=recipient_id).values_list("active_workspace_id", flat=True).first()
    return (
        Notification.objects.filter(
            recipient_id=recipient_id,
            archived_at__isnull=True,
            is_read=False,
            workspace_id=active_id,
        )
        .exclude(kind=Notification.Kind.PROJECT_UPDATE)
        .count()
    )


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

        row = (
            Notification.objects.select_related("task__project", "actor", "comment", "project_update__project")
            .filter(pk=pk)
            .first()
        )
        if row is None:
            return
        unread = _unread_count(recipient_id)
        payload = {
            "kind": row.kind,
            "workspace_id": row.workspace_id,
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


def parse_mentioned_user_ids(text: str | None) -> set[int]:
    """Return the user ids referenced by ``mention:`` tokens in Markdown.

    Args:
        text: Markdown source (comment body / task description).

    Returns:
        A set of user ids. Empty for falsy input. Membership is *not*
        validated here — :func:`notify_mentions` does that.
    """
    if not text:
        return set()
    return {int(m) for m in _MENTION_TOKEN_RE.findall(text)}


def notify_mentions(*, user_ids, actor, task, workspace_id, preview, comment=None) -> set[int]:
    """Send ``MENTION`` notifications to workspace members among ``user_ids``.

    Non-members are dropped (a mention can only reach someone who can see
    the task) and the actor is dropped by :func:`notify`'s self-rule.

    Args:
        user_ids: Candidate recipient ids parsed from the text.
        actor: The :class:`User` who wrote the mention.
        task: The :class:`Task` the mention lives on.
        workspace_id: Workspace the task belongs to.
        preview: Denormalized snippet for the inbox row.
        comment: The :class:`Comment` carrying the mention, if any.

    Returns:
        The set of user ids actually notified (validated members), so the
        comment fan-out can avoid sending them a duplicate ``COMMENT``.
    """
    if not user_ids:
        return set()
    from apps.workspaces.models import WorkspaceMember

    valid = set(
        WorkspaceMember.objects.filter(workspace_id=workspace_id, user_id__in=user_ids).values_list(
            "user_id", flat=True
        )
    )
    for recipient_id in valid:
        notify(
            recipient_id=recipient_id,
            actor=actor,
            kind=Notification.Kind.MENTION,
            workspace_id=workspace_id,
            task=task,
            comment=comment,
            preview=preview,
        )
    return valid


def notify_description_mentions(*, old_text, new_text, task, actor) -> None:
    """Notify users newly @-mentioned in a task description edit.

    Diffs the mention tokens so re-saving a description (or editing an
    unrelated field) never re-pings someone already mentioned.

    Args:
        old_text: Description Markdown before the edit.
        new_text: Description Markdown after the edit.
        task: The :class:`Task` (``project.workspace_id`` read without a
            query when ``project__workspace`` is select-related).
        actor: The :class:`User` who edited the description.
    """
    added = parse_mentioned_user_ids(new_text) - parse_mentioned_user_ids(old_text)
    if not added:
        return
    notify_mentions(
        user_ids=added,
        actor=actor,
        task=task,
        workspace_id=task.project.workspace_id,
        preview=task.title,
    )


def notify_comment_created(*, comment, actor) -> None:
    """Fan a new comment out to mentions, then assignee + reporter (+ parent author).

    Called right after the ``comment.created`` activity event at every
    surface that posts comments (web, DRF, MCP). ``@``-mentions in the
    body get a ``MENTION`` notification first; the assignee / reporter —
    and, when the comment is a reply, the parent comment's author — get a
    ``COMMENT`` notification, minus anyone already mentioned (so a
    mentioned recipient gets the higher-signal mention, not a duplicate).

    Args:
        comment: The freshly created :class:`Comment`.
        actor: The :class:`User` who wrote the comment.
    """
    task = comment.task
    workspace_id = task.project.workspace_id
    preview = _truncate_preview(comment.body)

    mentioned = notify_mentions(
        user_ids=parse_mentioned_user_ids(comment.body),
        actor=actor,
        task=task,
        workspace_id=workspace_id,
        preview=preview,
        comment=comment,
    )

    involved = {task.assignee_id, task.reporter_id}
    if comment.parent_id is not None:
        involved.add(comment.parent.author_id)
    involved.discard(None)
    involved -= mentioned
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


def notify_project_update_created(*, update, actor) -> None:
    """Fan a new project status update out to the workspace's members.

    Called right after a :class:`apps.projects.models.ProjectUpdate` is
    created. Every member of the update's workspace gets a
    ``PROJECT_UPDATE`` notification — the same audience that sees the
    update in the inbox Updates tab — and the author is dropped by
    :func:`notify`'s self-suppression.

    Args:
        update: The freshly created ``ProjectUpdate``.
        actor: The :class:`User` who posted it.
    """
    from apps.workspaces.models import WorkspaceMember

    workspace_id = update.project.workspace_id
    preview = _truncate_preview(update.body)
    member_ids = WorkspaceMember.objects.filter(workspace_id=workspace_id).values_list("user_id", flat=True)
    for recipient_id in member_ids:
        notify(
            recipient_id=recipient_id,
            actor=actor,
            kind=Notification.Kind.PROJECT_UPDATE,
            workspace_id=workspace_id,
            project_update=update,
            preview=preview,
        )
