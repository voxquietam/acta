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
    user_workspace_ids,
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


def task_delete(user: User, arguments: dict[str, Any]) -> Any:
    """Hard-delete a task (irreversible — drops the row).

    Use sparingly — most flows want ``acta_task_archive`` instead,
    which keeps the row for audit / restore. Delete is for genuine
    cleanup of accidental tasks. Emits a ``task.deleted`` activity
    event with a snapshot of key fields so the audit trail survives
    the row's removal.
    """
    from apps.activity.models import ActivityLog
    from apps.activity.services import log_event

    args = arguments or {}
    slug = args.get("slug")
    if not slug:
        raise ValueError("Argument 'slug' is required.")
    task = resolve_task(user, slug)
    snapshot = {
        "title": task.title,
        "project_id": task.project_id,
        "number": task.number,
        "status": task.status,
    }
    workspace = task.project.workspace
    project = task.project
    task_id = task.id
    task.delete()
    log_event(
        workspace=workspace,
        project=project,
        actor=user,
        event_type="task.deleted",
        target_type=ActivityLog.TARGET_TASK,
        target_id=task_id or 0,
        payload={"snapshot": snapshot},
    )
    return {"deleted_slug": slug, "snapshot": snapshot}


def tasks_bulk_create(user: User, arguments: dict[str, Any]) -> Any:
    """Create multiple tasks in one tool call.

    Wraps every create in a single transaction — if ANY task fails
    validation, the whole batch rolls back. Each item in ``tasks``
    has the same shape as ``acta_task_create`` arguments. Returns
    ``{"created": [<task summary>, …]}`` on success.
    """
    from django.db import transaction

    args = arguments or {}
    items = args.get("tasks") or []
    if not isinstance(items, list) or not items:
        raise ValueError("Argument 'tasks' must be a non-empty list of task specs.")

    created: list[dict[str, Any]] = []
    with transaction.atomic():
        for idx, item in enumerate(items):
            try:
                created.append(task_create(user, item))
            except (ValueError, Exception) as exc:
                # Re-raise inside the transaction so the whole batch
                # rolls back. Wrap the message so the LLM sees which
                # index failed without losing the original cause.
                raise ValueError(f"Bulk create failed at index {idx}: {exc}") from exc
    return {"created": created, "count": len(created)}


def tasks_bulk_update(user: User, arguments: dict[str, Any]) -> Any:
    """Update multiple tasks in one tool call.

    Each item in ``updates`` has the same shape as ``acta_task_update``
    arguments (``slug`` + the fields to patch). Atomic — if any item
    fails validation, the whole batch rolls back. Returns
    ``{"updated": [<task summary>, …]}``.
    """
    from django.db import transaction

    args = arguments or {}
    items = args.get("updates") or []
    if not isinstance(items, list) or not items:
        raise ValueError("Argument 'updates' must be a non-empty list of patch specs.")

    updated: list[dict[str, Any]] = []
    with transaction.atomic():
        for idx, item in enumerate(items):
            try:
                updated.append(task_update(user, item))
            except (ValueError, Exception) as exc:
                raise ValueError(f"Bulk update failed at index {idx}: {exc}") from exc
    return {"updated": updated, "count": len(updated)}


def tasks_bulk_delete(user: User, arguments: dict[str, Any]) -> Any:
    """Hard-delete multiple tasks at once.

    Irreversible. Atomic — if any delete fails (e.g. slug not found),
    the whole batch rolls back. Each deletion emits a ``task.deleted``
    activity event with a snapshot. Returns ``{count, deleted: [<snapshot>, …]}``.
    """
    from django.db import transaction

    args = arguments or {}
    slugs = args.get("slugs") or []
    if not isinstance(slugs, list) or not slugs:
        raise ValueError("Argument 'slugs' must be a non-empty list of task slugs.")

    deleted: list[dict[str, Any]] = []
    with transaction.atomic():
        for idx, slug in enumerate(slugs):
            try:
                deleted.append(task_delete(user, {"slug": slug}))
            except (ValueError, Exception) as exc:
                raise ValueError(f"Bulk delete failed at index {idx} ({slug!r}): {exc}") from exc
    return {"deleted": deleted, "count": len(deleted)}


def tasks_bulk_archive(user: User, arguments: dict[str, Any]) -> Any:
    """Archive multiple tasks at once.

    ``slugs`` is a list of task slugs. Atomic. Already-archived tasks
    in the batch are a no-op (matches single-task behaviour). Returns
    ``{"archived": [<task summary>, …]}``.
    """
    from django.db import transaction

    args = arguments or {}
    slugs = args.get("slugs") or []
    if not isinstance(slugs, list) or not slugs:
        raise ValueError("Argument 'slugs' must be a non-empty list of task slugs.")

    archived: list[dict[str, Any]] = []
    with transaction.atomic():
        for idx, slug in enumerate(slugs):
            try:
                archived.append(task_archive(user, {"slug": slug}))
            except (ValueError, Exception) as exc:
                raise ValueError(f"Bulk archive failed at index {idx} ({slug!r}): {exc}") from exc
    return {"archived": archived, "count": len(archived)}


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
        name="acta_task_delete",
        description=(
            "HARD-delete a task (irreversible). Most flows want "
            "``acta_task_archive`` instead — archive keeps the row for "
            "audit / restore. Use delete only for genuine cleanup. "
            "Emits a ``task.deleted`` activity event with a snapshot of "
            "the key fields so the audit trail survives the row removal. "
            "Required: ``slug``."
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
        name="acta_tasks_bulk_delete",
        description=(
            "HARD-delete multiple tasks at once. Irreversible. Atomic — "
            "if any delete fails (e.g. slug not accessible), the whole "
            "batch rolls back. Each deletion emits a ``task.deleted`` "
            "event with a snapshot. Returns ``{count, deleted: [{deleted_slug, snapshot}, …]}``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slugs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            },
            "required": ["slugs"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_label_create",
        description=(
            "Create a label in a workspace the user belongs to. "
            "Required: ``workspace`` (slug), ``name``. Optional: ``color`` "
            "(hex string like ``#10b981``; defaults to neutral grey). "
            "Returns ``{id, name, color, workspace_slug}``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "name": {"type": "string"},
                "color": {"type": "string", "description": "Hex colour, e.g. '#10b981'."},
            },
            "required": ["workspace", "name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_label_update",
        description=(
            "Rename / recolor a label. Required: ``id``. Optional: "
            "``name``, ``color`` — at least one is needed. Returns the "
            "updated label."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "name": {"type": "string"},
                "color": {"type": "string"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_label_delete",
        description=(
            "Hard-delete a label. Cascades to drop all task associations "
            "(M2M). Irreversible. Returns ``{deleted_id, name, workspace_slug}``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_tasks_bulk_create",
        description=(
            "Create multiple tasks in one atomic call. ``tasks`` is a list of "
            "task specs — each has the same shape as ``acta_task_create`` "
            "arguments (project, title, etc.). If ANY task fails validation, "
            "the WHOLE batch rolls back — partial creates never persist. "
            "Returns ``{count, created: [<task summary>, …]}``. Activity events "
            "fire for each successful task once the transaction commits."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "project": {"type": "string"},
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["planned", "to-do", "in-progress", "in-review", "done"],
                            },
                            "priority": {"type": "integer", "minimum": 0, "maximum": 4},
                            "size": {"type": "integer", "enum": [1, 2, 3, 5, 8, 13]},
                            "due_date": {"type": "string"},
                            "assignee_username": {"type": "string"},
                            "parent_slug": {"type": "string"},
                            "label_names": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["project", "title"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["tasks"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_tasks_bulk_update",
        description=(
            "Update multiple tasks in one atomic call. ``updates`` is a list of "
            "patch specs — each has the same shape as ``acta_task_update`` "
            "arguments (``slug`` is required, every other field is optional). "
            "Atomic — if any patch fails, the whole batch rolls back. Returns "
            "``{count, updated: [<task summary>, …]}``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "updates": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "slug": {"type": "string"},
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
                },
            },
            "required": ["updates"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_tasks_bulk_archive",
        description=(
            "Archive multiple tasks at once. ``slugs`` is a list of task slugs "
            "(e.g. ['ACTA-128', 'ACTA-129']). Atomic — if any archive call "
            "fails, the whole batch rolls back. Already-archived tasks are "
            "left untouched (matches single-task behaviour). Returns "
            "``{count, archived: [<task summary>, …]}``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slugs": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string"},
                },
            },
            "required": ["slugs"],
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


def _resolve_workspace(user: User, slug: str):
    """Look up a workspace by slug, scoped to user's memberships."""
    from apps.workspaces.models import Workspace

    try:
        return Workspace.objects.get(slug=slug, id__in=user_workspace_ids(user))
    except Workspace.DoesNotExist:
        raise ValueError(f"Workspace {slug!r} not found or not accessible to this user.")


def _serialize_label(label) -> dict[str, Any]:
    return {
        "id": label.id,
        "name": label.name,
        "color": label.color,
        "workspace_slug": label.workspace.slug,
    }


def label_create(user: User, arguments: dict[str, Any]) -> Any:
    """Create a label in a workspace the user belongs to.

    Required: ``workspace`` (slug), ``name``. Optional: ``color`` (hex
    string like ``#10b981`` — defaults to a neutral grey if omitted).
    """
    from apps.labels.models import Label

    args = arguments or {}
    ws_slug = args.get("workspace")
    name = (args.get("name") or "").strip()
    if not ws_slug or not name:
        raise ValueError("Arguments 'workspace' (slug) and 'name' are required.")
    workspace = _resolve_workspace(user, ws_slug)
    color = (args.get("color") or "").strip() or "#9ca3af"
    label = Label.objects.create(workspace=workspace, name=name, color=color)
    return _serialize_label(label)


def label_update(user: User, arguments: dict[str, Any]) -> Any:
    """Rename / recolor an existing label."""
    from apps.labels.models import Label

    args = arguments or {}
    label_id = args.get("id")
    if not label_id:
        raise ValueError("Argument 'id' is required.")
    try:
        label = Label.objects.select_related("workspace").get(
            id=label_id,
            workspace_id__in=user_workspace_ids(user),
        )
    except Label.DoesNotExist:
        raise ValueError(f"Label id={label_id} not found or not accessible to this user.")

    updates: list[str] = []
    if "name" in args:
        new_name = (args["name"] or "").strip()
        if not new_name:
            raise ValueError("Label name must be non-empty.")
        label.name = new_name
        updates.append("name")
    if "color" in args:
        label.color = (args["color"] or "").strip() or label.color
        updates.append("color")
    if updates:
        label.save(update_fields=updates)
    return _serialize_label(label)


def label_delete(user: User, arguments: dict[str, Any]) -> Any:
    """Hard-delete a label. Removes all task associations (M2M cascade).

    Irreversible. Returns ``{deleted_id, name, workspace_slug}``.
    """
    from apps.labels.models import Label

    args = arguments or {}
    label_id = args.get("id")
    if not label_id:
        raise ValueError("Argument 'id' is required.")
    try:
        label = Label.objects.select_related("workspace").get(
            id=label_id,
            workspace_id__in=user_workspace_ids(user),
        )
    except Label.DoesNotExist:
        raise ValueError(f"Label id={label_id} not found or not accessible to this user.")
    snapshot = _serialize_label(label)
    label.delete()
    return {"deleted_id": snapshot["id"], "name": snapshot["name"], "workspace_slug": snapshot["workspace_slug"]}


CALLABLES: dict[str, Callable[[User, dict[str, Any]], Any]] = {
    "acta_task_create": task_create,
    "acta_task_update": task_update,
    "acta_task_archive": task_archive,
    "acta_task_delete": task_delete,
    "acta_comment_create": comment_create,
    "acta_tasks_bulk_create": tasks_bulk_create,
    "acta_tasks_bulk_update": tasks_bulk_update,
    "acta_tasks_bulk_archive": tasks_bulk_archive,
    "acta_tasks_bulk_delete": tasks_bulk_delete,
    "acta_label_create": label_create,
    "acta_label_update": label_update,
    "acta_label_delete": label_delete,
}


__all__ = ["TOOLS", "CALLABLES"]
