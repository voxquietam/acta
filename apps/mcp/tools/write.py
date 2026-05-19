"""Write MCP tools for Acta — create / update / archive / comment.

Every mutation routes through Django models (and :class:`TaskSerializer`
where applicable) so MCP-driven writes obey the exact same validation
gates the web UI does — workspace membership, label-in-workspace,
assignee-must-be-an-active-member, subtask-depth-1.
"""

from __future__ import annotations

from typing import Any, Callable

from mcp.types import Tool

from apps.accounts.models import User
from apps.mcp.tools._shared import (
    FakeRequest,
    resolve_project,
    resolve_task,
    resolve_user_by_username,
    serialize_task_summary,
)


def task_create(user: User, arguments: dict[str, Any]) -> Any:
    """Create a new task in one of the user's projects.

    Validation flows through :class:`TaskSerializer` so MCP-driven
    creates obey every rule the web UI does — workspace membership,
    label-in-workspace, assignee-must-be-member, subtask-depth-1.

    Emits the same ``task.created`` activity event as the web's DRF
    ``perform_create`` so the audit log records who created what,
    regardless of which client surface (web UI or MCP) triggered it.
    """
    from apps.activity.models import ActivityLog
    from apps.activity.services import log_event
    from apps.labels.models import Label
    from apps.tasks.serializers import TaskSerializer

    args = arguments or {}
    project_slug = args.get("project")
    title = (args.get("title") or "").strip()
    if not project_slug or not title:
        raise ValueError("Arguments 'project' (slug prefix) and 'title' are required.")
    project = resolve_project(user, project_slug)

    data: dict[str, Any] = {
        "project": project.id,
        "title": title,
    }
    for field in ("description", "status", "size"):
        if args.get(field) is not None:
            data[field] = args[field]
    if args.get("priority") is not None:
        data["priority"] = args["priority"]
    if args.get("due_date"):
        data["due_date"] = args["due_date"]

    assignee = args.get("assignee_username")
    if assignee:
        data["assignee"] = resolve_user_by_username(assignee).id
    parent_slug = args.get("parent_slug")
    if parent_slug:
        data["parent"] = resolve_task(user, parent_slug).id

    label_names = args.get("label_names") or []
    if label_names:
        labels = list(Label.objects.filter(workspace=project.workspace, name__in=label_names))
        found_names = {lab.name for lab in labels}
        missing = [n for n in label_names if n not in found_names]
        if missing:
            raise ValueError(
                f"Labels not found in workspace {project.workspace.slug!r}: {missing}. "
                "Create them in admin or attach via acta_task_update after creation.",
            )
        data["labels"] = [lab.id for lab in labels]

    serializer = TaskSerializer(data=data, context={"request": FakeRequest(user)})
    if not serializer.is_valid():
        raise ValueError(f"Task validation failed: {serializer.errors}")
    task = serializer.save(reporter=user)
    log_event(
        workspace=task.project.workspace,
        project=task.project,
        actor=user,
        event_type="task.created",
        target_type=ActivityLog.TARGET_TASK,
        target_id=task.id,
        payload={
            "title": task.title,
            "project_id": task.project_id,
            "parent_id": task.parent_id,
        },
    )
    return serialize_task_summary(task)


def task_update(user: User, arguments: dict[str, Any]) -> Any:
    """Patch an existing task — title, status, priority, assignee, labels, etc.

    Same validation gates as create. Only the fields present in
    ``arguments`` are written; everything else is preserved. To clear
    an optional value (e.g. drop the assignee) pass it as ``null``.

    Snapshots the task before saving and walks the diff via
    :func:`emit_task_diff_events` so every watched field that changed
    produces its own ``ActivityLog`` row — matches the granular event
    emission the web UI's DRF ``perform_update`` does.
    """
    from apps.labels.models import Label
    from apps.tasks.events import emit_task_diff_events, snapshot_task
    from apps.tasks.serializers import TaskSerializer

    args = arguments or {}
    slug = args.get("slug")
    if not slug:
        raise ValueError("Argument 'slug' is required.")
    task = resolve_task(user, slug)

    data: dict[str, Any] = {}
    for field in ("title", "description", "status", "size"):
        if field in args:
            data[field] = args[field]
    if "priority" in args:
        data["priority"] = args["priority"]
    if "due_date" in args:
        data["due_date"] = args["due_date"]

    if "assignee_username" in args:
        assignee_value = args["assignee_username"]
        data["assignee"] = None if assignee_value is None else resolve_user_by_username(assignee_value).id

    if "label_names" in args:
        names = args["label_names"] or []
        labels = list(Label.objects.filter(workspace=task.project.workspace, name__in=names))
        found = {lab.name for lab in labels}
        missing = [n for n in names if n not in found]
        if missing:
            raise ValueError(f"Labels not found in workspace: {missing}.")
        data["labels"] = [lab.id for lab in labels]

    old_state = snapshot_task(task)
    serializer = TaskSerializer(instance=task, data=data, partial=True, context={"request": FakeRequest(user)})
    if not serializer.is_valid():
        raise ValueError(f"Task validation failed: {serializer.errors}")
    task = serializer.save()
    emit_task_diff_events(old_state=old_state, task=task, actor=user)
    return serialize_task_summary(task)


def task_archive(user: User, arguments: dict[str, Any]) -> Any:
    """Archive a task (soft delete — sets ``archived_at``).

    Goes through ``snapshot_task`` + ``emit_task_diff_events`` so the
    activity log gets a proper ``task.archived`` event with the actor
    set to the MCP-authenticated user. Idempotent: re-archiving an
    already-archived task is a no-op.
    """
    from django.utils import timezone

    from apps.tasks.events import emit_task_diff_events, snapshot_task

    args = arguments or {}
    slug = args.get("slug")
    if not slug:
        raise ValueError("Argument 'slug' is required.")
    task = resolve_task(user, slug)
    if task.archived_at is None:
        old_state = snapshot_task(task)
        task.archived_at = timezone.now()
        task.save(update_fields=["archived_at", "updated_at"])
        emit_task_diff_events(old_state=old_state, task=task, actor=user)
    return serialize_task_summary(task)


def comment_create(user: User, arguments: dict[str, Any]) -> Any:
    """Post a comment on a task.

    Emits a ``comment.created`` activity event with the MCP user as
    the actor — same payload shape the web's DRF comment view uses
    (``{task_id, body_preview}``) so the activity feed reads the
    same regardless of origin surface.
    """
    from apps.activity.models import ActivityLog
    from apps.activity.services import log_event
    from apps.comments.models import Comment

    args = arguments or {}
    slug = args.get("task")
    body = (args.get("body") or "").strip()
    if not slug or not body:
        raise ValueError("Arguments 'task' (slug) and 'body' (non-empty) are required.")
    task = resolve_task(user, slug)
    comment = Comment.objects.create(task=task, author=user, body=body)
    log_event(
        workspace=task.project.workspace,
        project=task.project,
        actor=user,
        event_type="comment.created",
        target_type=ActivityLog.TARGET_COMMENT,
        target_id=comment.id,
        payload={
            "task_id": comment.task_id,
            "body_preview": comment.body[:120],
        },
    )
    return {
        "id": comment.id,
        "task_slug": task.slug,
        "author_username": user.username,
        "body": comment.body,
        "created_at": comment.created_at.isoformat(),
    }


TOOLS: list[Tool] = [
    Tool(
        name="acta_task_create",
        description=(
            "Create a new task in one of the user's projects. "
            "Required: ``project`` (slug prefix), ``title``. "
            "Optional: ``description`` (Markdown), ``status`` (default to-do), "
            "``priority`` (0=none, 1=Urgent, 2=High, 3=Medium, 4=Low), "
            "``size`` (Fibonacci integer 1/2/3/5/8/13), ``due_date`` (ISO date), "
            "``assignee_username`` (must be a member of the project's workspace), "
            "``parent_slug`` (make this a subtask of an existing task — depth-1 limit), "
            "``label_names`` (list of label names; must exist in the workspace). "
            "Validation matches the web UI exactly: workspace membership, "
            "label-in-workspace, assignee-must-be-active-member, subtask-depth-1. "
            "Returns the same compact task object ``acta_tasks_list`` rows use."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug prefix, e.g. ACTA."},
                "title": {"type": "string"},
                "description": {"type": "string", "description": "Markdown body."},
                "status": {
                    "type": "string",
                    "enum": ["planned", "to-do", "in-progress", "in-review", "done"],
                },
                "priority": {"type": "integer", "minimum": 0, "maximum": 4},
                "size": {"type": "integer", "enum": [1, 2, 3, 5, 8, 13]},
                "due_date": {"type": "string", "description": "ISO date, e.g. '2026-05-30'."},
                "assignee_username": {"type": "string"},
                "parent_slug": {"type": "string", "description": "Parent task slug (e.g. ACTA-128)."},
                "label_names": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project", "title"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_task_update",
        description=(
            "Update an existing task (partial / PATCH-style). "
            "Required: ``slug``. Any combination of optional fields: "
            "``title``, ``description``, ``status``, ``priority``, ``size`` "
            "(Fibonacci 1/2/3/5/8/13), ``due_date``, ``assignee_username`` "
            "(pass ``null`` to clear), ``label_names`` (replaces the full "
            "label set). Same validation as create. Returns the updated task summary."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Task slug, e.g. ACTA-128."},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["planned", "to-do", "in-progress", "in-review", "done"],
                },
                "priority": {"type": "integer", "minimum": 0, "maximum": 4},
                "size": {"type": ["integer", "null"], "enum": [1, 2, 3, 5, 8, 13, None]},
                "due_date": {"type": ["string", "null"]},
                "assignee_username": {"type": ["string", "null"]},
                "label_names": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_task_archive",
        description=(
            "Archive a task (soft delete — sets ``archived_at``). The task stays "
            "in the database and is excluded from default lists, but can still be "
            "fetched with ``acta_task_get`` and surfaced with ``include_archived: "
            "true`` in ``acta_tasks_list``. Required: ``slug``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_comment_create",
        description=(
            "Post a Markdown comment on a task. Required: ``task`` (slug), "
            "``body`` (non-empty Markdown). The comment is owned by the calling "
            "user. Returns ``{id, task_slug, author_username, body, created_at}``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task slug, e.g. ACTA-128."},
                "body": {"type": "string"},
            },
            "required": ["task", "body"],
            "additionalProperties": False,
        },
    ),
]


CALLABLES: dict[str, Callable[[User, dict[str, Any]], Any]] = {
    "acta_task_create": task_create,
    "acta_task_update": task_update,
    "acta_task_archive": task_archive,
    "acta_comment_create": comment_create,
}


__all__ = ["TOOLS", "CALLABLES"]
