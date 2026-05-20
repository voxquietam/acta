"""Read-only MCP tools for Acta.

List / get / search endpoints — every callable here is pure SELECTs
through Django ORM, no side effects. Mirrors the same workspace-
membership scoping the web UI applies. Pair with
:mod:`apps.mcp.tools.write` for the create / update / delete side.
"""

from __future__ import annotations

import datetime
from typing import Any, Callable

from mcp.types import Tool

from apps.accounts.models import User
from apps.mcp.tools._shared import user_workspace_ids
from apps.tasks.models import Task


def workspaces_list(user: User, arguments: dict[str, Any]) -> Any:
    """List every workspace the calling user is a member of."""
    qs = user.workspaces.order_by("name").distinct()
    return [
        {
            "id": ws.id,
            "name": ws.name,
            "slug": ws.slug,
        }
        for ws in qs
    ]


def projects_list(user: User, arguments: dict[str, Any]) -> Any:
    """List projects the user can access, optionally scoped to one workspace."""
    from apps.projects.models import Project

    qs = Project.objects.filter(workspace_id__in=user_workspace_ids(user)).select_related("workspace", "lead")
    workspace_slug = (arguments or {}).get("workspace")
    if workspace_slug:
        qs = qs.filter(workspace__slug=workspace_slug)
    if not (arguments or {}).get("include_archived", False):
        qs = qs.filter(archived=False)
    return [
        {
            "id": p.id,
            "slug_prefix": p.slug_prefix,
            "name": p.name,
            "workspace_slug": p.workspace.slug,
            "workspace_name": p.workspace.name,
            "lead_username": p.lead.username if p.lead_id else None,
            "archived": p.archived,
        }
        for p in qs.order_by("workspace__name", "name")
    ]


def tasks_list(user: User, arguments: dict[str, Any]) -> Any:
    """List tasks the user can access, filtered by the supplied query."""
    args = arguments or {}
    qs = (
        Task.objects.filter(project__workspace_id__in=user_workspace_ids(user))
        .select_related("project__workspace", "assignee")
        .prefetch_related("labels")
    )
    project = args.get("project")
    if project:
        qs = qs.filter(project__slug_prefix=project)
    status = args.get("status")
    if isinstance(status, str):
        qs = qs.filter(status=status)
    elif isinstance(status, list):
        qs = qs.filter(status__in=status)
    priority = args.get("priority")
    if isinstance(priority, int):
        qs = qs.filter(priority=priority)
    elif isinstance(priority, list):
        qs = qs.filter(priority__in=priority)
    assignee = args.get("assignee")
    if assignee == "me":
        qs = qs.filter(assignee=user)
    elif assignee == "unassigned":
        qs = qs.filter(assignee__isnull=True)
    elif assignee:
        qs = qs.filter(assignee__username=assignee)
    q = args.get("q")
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    if not args.get("include_archived", False):
        qs = qs.filter(archived_at__isnull=True)

    limit = min(int(args.get("limit", 50)), 200)
    qs = qs.order_by("-updated_at")[:limit]

    return [
        {
            "slug": t.slug,
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "size": t.size,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "assignee_username": t.assignee.username if t.assignee_id else None,
            "project_slug_prefix": t.project.slug_prefix,
            "project_name": t.project.name,
            "workspace_slug": t.project.workspace.slug,
            "labels": [{"name": label.name, "color": label.color} for label in t.labels.all()],
            "updated_at": t.updated_at.isoformat(),
        }
        for t in qs
    ]


def activity_list(user: User, arguments: dict[str, Any]) -> Any:
    """Flat list of activity events the user can see, with rich filters.

    Designed for AI analytics: "summarise what happened in AUDIT last
    week", "who closed the most tasks in May", "how many status
    transitions on ACTA-128". One tool call returns up to 1000
    events — saves the LLM from making N per-task calls.
    """
    from apps.activity.models import ActivityLog

    args = arguments or {}
    qs = ActivityLog.objects.filter(workspace_id__in=user_workspace_ids(user)).select_related(
        "actor", "workspace", "project"
    )

    ws = args.get("workspace")
    if ws:
        qs = qs.filter(workspace__slug=ws)
    project_prefix = args.get("project")
    if project_prefix:
        qs = qs.filter(project__slug_prefix=project_prefix)

    task_slug = args.get("task")
    if task_slug:
        try:
            prefix, number = task_slug.rsplit("-", 1)
            number_int = int(number)
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid task slug: {task_slug!r}. Expected 'PREFIX-NUMBER'.")
        from django.db.models import Q

        task_id_subq = Task.objects.filter(project__slug_prefix=prefix, number=number_int).values_list("id", flat=True)[
            :1
        ]
        # Match events targeting the task itself OR comment events
        # whose payload.task_id points at it.
        qs = qs.filter(
            Q(target_type=ActivityLog.TARGET_TASK, target_id__in=task_id_subq)
            | Q(target_type=ActivityLog.TARGET_COMMENT, payload__task_id__in=list(task_id_subq))
        )

    event_type = args.get("event_type")
    if isinstance(event_type, str):
        qs = qs.filter(event_type=event_type)
    elif isinstance(event_type, list):
        qs = qs.filter(event_type__in=event_type)

    target_type = args.get("target_type")
    if target_type:
        qs = qs.filter(target_type=target_type)

    actor = args.get("actor")
    if actor:
        qs = qs.filter(actor__username=actor)

    since = args.get("since")
    if since:
        qs = qs.filter(created_at__gte=since)
    until = args.get("until")
    if until:
        qs = qs.filter(created_at__lte=until)

    limit = min(int(args.get("limit", 200)), 1000)
    qs = qs.order_by("-created_at")[:limit]

    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "target_type": e.target_type,
            "target_id": e.target_id,
            "workspace_slug": e.workspace.slug if e.workspace_id else None,
            "project_slug_prefix": e.project.slug_prefix if e.project_id else None,
            "actor_username": e.actor.username if e.actor_id else None,
            "actor_display_name": e.actor.display_name if e.actor_id else None,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
        }
        for e in qs
    ]


def comments_list(user: User, arguments: dict[str, Any]) -> Any:
    """Flat list of comments the user can see, with filters.

    Symmetric to ``activity_list`` but for prose. Useful for
    "summarise discussion in WEB last sprint", "what did Kate say
    about the migration", etc.
    """
    from apps.comments.models import Comment

    args = arguments or {}
    qs = Comment.objects.filter(task__project__workspace_id__in=user_workspace_ids(user)).select_related(
        "author", "task__project__workspace"
    )

    ws = args.get("workspace")
    if ws:
        qs = qs.filter(task__project__workspace__slug=ws)
    project_prefix = args.get("project")
    if project_prefix:
        qs = qs.filter(task__project__slug_prefix=project_prefix)

    task_slug = args.get("task")
    if task_slug:
        try:
            prefix, number = task_slug.rsplit("-", 1)
            number_int = int(number)
        except (ValueError, AttributeError):
            raise ValueError(f"Invalid task slug: {task_slug!r}. Expected 'PREFIX-NUMBER'.")
        qs = qs.filter(task__project__slug_prefix=prefix, task__number=number_int)

    author = args.get("author")
    if author:
        qs = qs.filter(author__username=author)

    q = args.get("q")
    if q:
        qs = qs.filter(body__icontains=q)

    since = args.get("since")
    if since:
        qs = qs.filter(created_at__gte=since)
    until = args.get("until")
    if until:
        qs = qs.filter(created_at__lte=until)

    limit = min(int(args.get("limit", 200)), 1000)
    qs = qs.order_by("-created_at")[:limit]

    return [
        {
            "id": c.id,
            "task_slug": c.task.slug,
            "project_slug_prefix": c.task.project.slug_prefix,
            "workspace_slug": c.task.project.workspace.slug,
            "author_username": c.author.username if c.author_id else None,
            "author_display_name": c.author.display_name if c.author_id else None,
            "body": c.body,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
            "edited": (c.updated_at - c.created_at) > datetime.timedelta(seconds=1),
        }
        for c in qs
    ]


def task_get(user: User, arguments: dict[str, Any]) -> Any:
    """Return the full payload for one task: meta + description + subtasks + comments + activity.

    Intended for AI workflows that need to reason over the complete
    history of a single task — correlations, status summaries,
    auto-triage. The web's task-detail view feeds the same surfaces;
    this just packages them into one JSON-friendly object.
    """
    from apps.activity.models import ActivityLog

    slug = (arguments or {}).get("slug")
    if not slug:
        raise ValueError("Argument 'slug' is required (e.g. 'ACTA-128').")
    try:
        prefix, number = slug.rsplit("-", 1)
        number_int = int(number)
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid slug format: {slug!r}. Expected 'PREFIX-NUMBER' (e.g. 'ACTA-128').")

    try:
        task = (
            Task.objects.filter(project__workspace_id__in=user_workspace_ids(user))
            .select_related("project__workspace", "assignee", "reporter", "parent")
            .prefetch_related(
                "labels",
                "subtasks__assignee",
                "blocked_by__project",
                "blocks__project",
                "related__project",
            )
            .get(project__slug_prefix=prefix, number=number_int)
        )
    except Task.DoesNotExist:
        raise ValueError(f"Task {slug!r} not found or not accessible to this user.")

    comments = [
        {
            "id": c.id,
            "author_username": c.author.username if c.author_id else None,
            "author_display_name": c.author.display_name if c.author_id else None,
            "body": c.body,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
            # ``auto_now`` and ``auto_now_add`` resolve at slightly
            # different microsecond instants on INSERT, so the two
            # timestamps aren't byte-equal even for a fresh row. Treat
            # "edited" as a non-trivial delta (>1s) so unedited
            # comments don't false-positive.
            "edited": (c.updated_at - c.created_at) > datetime.timedelta(seconds=1),
        }
        for c in task.comments.select_related("author").order_by("created_at", "id")
    ]

    from django.db.models import Q

    activity_qs = (
        ActivityLog.objects.filter(
            Q(target_type=ActivityLog.TARGET_TASK, target_id=task.id)
            | Q(target_type=ActivityLog.TARGET_COMMENT, payload__task_id=task.id),
        )
        .select_related("actor")
        .order_by("created_at")
    )
    activity = [
        {
            "id": e.id,
            "event_type": e.event_type,
            "target_type": e.target_type,
            "target_id": e.target_id,
            "actor_username": e.actor.username if e.actor_id else None,
            "actor_display_name": e.actor.display_name if e.actor_id else None,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
        }
        for e in activity_qs
    ]

    return {
        "slug": task.slug,
        "title": task.title,
        "description": task.description or "",
        "status": task.status,
        "priority": task.priority,
        "size": task.size,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
        "archived_at": task.archived_at.isoformat() if task.archived_at else None,
        "assignee_username": task.assignee.username if task.assignee_id else None,
        "assignee_display_name": task.assignee.display_name if task.assignee_id else None,
        "reporter_username": task.reporter.username if task.reporter_id else None,
        "reporter_display_name": task.reporter.display_name if task.reporter_id else None,
        "project_slug_prefix": task.project.slug_prefix,
        "project_name": task.project.name,
        "workspace_slug": task.project.workspace.slug,
        "workspace_name": task.project.workspace.name,
        "labels": [{"name": label.name, "color": label.color} for label in task.labels.all()],
        "parent_slug": task.parent.slug if task.parent_id else None,
        "subtasks": [
            {
                "slug": s.slug,
                "title": s.title,
                "status": s.status,
                "priority": s.priority,
                "assignee_username": s.assignee.username if s.assignee_id else None,
                "due_date": s.due_date.isoformat() if s.due_date else None,
            }
            for s in task.subtasks.order_by("number")
        ],
        "comments": comments,
        "activity": activity,
        "links": {
            "blocked_by": [{"slug": t.slug, "title": t.title, "status": t.status} for t in task.blocked_by.all()],
            "blocks": [{"slug": t.slug, "title": t.title, "status": t.status} for t in task.blocks.all()],
            "related": [{"slug": t.slug, "title": t.title, "status": t.status} for t in task.related.all()],
        },
        "is_blocked": task.is_blocked,
    }


TOOLS: list[Tool] = [
    Tool(
        name="acta_workspaces_list",
        description=(
            "List every Acta workspace the authenticated user is a member of. "
            "Returns ``[{id, name, slug}, …]``. Use this first to discover what "
            "scopes are available before drilling into projects or tasks."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_projects_list",
        description=(
            "List Acta projects the user can access. Optional ``workspace`` "
            "argument scopes to a single workspace by its slug; otherwise "
            "returns projects across every workspace the user belongs to. "
            "Set ``include_archived: true`` to include archived projects. "
            "Returns ``[{id, slug_prefix, name, workspace_slug, workspace_name, lead_username, archived}, …]``. "
            "``slug_prefix`` (e.g. ``ACTA``) is the identifier to pass into ``acta_tasks_list``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {
                    "type": "string",
                    "description": "Workspace slug to scope projects to. Omit for all accessible workspaces.",
                },
                "include_archived": {
                    "type": "boolean",
                    "description": "Include archived projects in the result (defaults to false).",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_activity_list",
        description=(
            "Flat list of activity events the user can see, with rich filters. "
            "Optimised for cross-task analytics — use this instead of looping "
            "``acta_task_get`` per task. "
            "Filters: ``workspace`` (slug), ``project`` (slug prefix), "
            "``task`` (slug like ACTA-128 — narrows to that task plus comment "
            "events on it), ``event_type`` (single or list, e.g. "
            "'task.status_changed' / 'task.archived' / 'comment.created'), "
            "``target_type`` (task/comment/project/workspace/member), "
            "``actor`` (username), ``since``/``until`` (ISO 8601 datetimes), "
            "``limit`` (default 200, max 1000). "
            "Returns ``[{id, event_type, target_type, target_id, workspace_slug, "
            "project_slug_prefix, actor_username, actor_display_name, payload, "
            "created_at}, …]`` sorted by most-recent first. ``payload`` is a "
            "JSON blob whose shape varies per event_type — see "
            "docs/decisions/0011-activity-log.md for the schema."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "project": {"type": "string", "description": "Project slug prefix (e.g. ACTA)."},
                "task": {"type": "string", "description": "Task slug (e.g. ACTA-128)."},
                "event_type": {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
                "target_type": {
                    "type": "string",
                    "enum": ["task", "comment", "project", "workspace", "member"],
                },
                "actor": {"type": "string", "description": "Username of the event's actor."},
                "since": {"type": "string", "description": "ISO 8601 datetime — events at or after this instant."},
                "until": {"type": "string", "description": "ISO 8601 datetime — events at or before this instant."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_comments_list",
        description=(
            "Flat list of comments the user can see, with filters. "
            "Symmetric to ``acta_activity_list`` but for prose. "
            "Filters: ``workspace`` (slug), ``project`` (slug prefix), "
            "``task`` (slug), ``author`` (username), ``q`` (case-insensitive "
            "search in comment body), ``since``/``until`` (ISO 8601), "
            "``limit`` (default 200, max 1000). "
            "Returns ``[{id, task_slug, project_slug_prefix, workspace_slug, "
            "author_username, author_display_name, body, created_at, updated_at, "
            "edited}, …]`` sorted by most-recent first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string"},
                "project": {"type": "string", "description": "Project slug prefix (e.g. ACTA)."},
                "task": {"type": "string", "description": "Task slug (e.g. ACTA-128)."},
                "author": {"type": "string", "description": "Username of comment author."},
                "q": {"type": "string", "description": "Case-insensitive substring search in body."},
                "since": {"type": "string", "description": "ISO 8601 datetime."},
                "until": {"type": "string", "description": "ISO 8601 datetime."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_task_get",
        description=(
            "Return the FULL payload for one Acta task — every field plus subtasks, "
            "comments, and the complete activity log. Use this when you need to reason "
            "about a single task in depth (correlations, status summary, auto-triage). "
            "``slug`` is mandatory, in the form ``PREFIX-NUMBER`` (e.g. ``ACTA-128``). "
            "Returns a single object with: "
            "``{slug, title, description, status, priority, size, due_date, created_at, "
            "updated_at, archived_at, assignee_username, assignee_display_name, "
            "reporter_username, reporter_display_name, project_slug_prefix, project_name, "
            "workspace_slug, workspace_name, parent_slug, labels: [{name, color}], "
            "subtasks: [{slug, title, status, priority, assignee_username, due_date}], "
            "comments: [{id, author_username, author_display_name, body, created_at, "
            "updated_at, edited}], "
            "activity: [{id, event_type, target_type, target_id, actor_username, "
            "actor_display_name, payload, created_at}]}``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Task slug, e.g. 'ACTA-128'."},
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_labels_list",
        description=(
            "List labels the user can see. Optional ``workspace`` slug to "
            "scope to one workspace; omit for all accessible workspaces. "
            "Returns ``[{id, name, color, workspace_slug, group_name}, …]`` "
            "sorted by workspace name then label name."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace slug to scope to."},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_tasks_list",
        description=(
            "List Acta tasks the user can access, with optional filters. "
            "Filters match the web UI: ``project`` (project slug prefix, e.g. ACTA), "
            "``status`` (one of planned/to-do/in-progress/in-review/done, or list), "
            "``priority`` (1=Urgent..4=Low, or list), ``assignee`` (username, ``me``, or ``unassigned``), "
            "``q`` (case-insensitive title/description search), "
            "``include_archived`` (default false), ``limit`` (default 50, max 200). "
            "Returns ``[{slug, title, status, priority, size, due_date, assignee_username, "
            "project_slug_prefix, project_name, workspace_slug, labels: [{name, color}], "
            "updated_at}, …]`` sorted by most-recently-updated first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug prefix (e.g. ACTA)."},
                "status": {
                    "oneOf": [
                        {
                            "type": "string",
                            "enum": ["planned", "to-do", "in-progress", "in-review", "done"],
                        },
                        {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["planned", "to-do", "in-progress", "in-review", "done"],
                            },
                        },
                    ],
                },
                "priority": {
                    "oneOf": [
                        {"type": "integer", "minimum": 0, "maximum": 4},
                        {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 4}},
                    ],
                },
                "assignee": {"type": "string", "description": "Username, ``me``, or ``unassigned``."},
                "q": {"type": "string", "description": "Case-insensitive search across title and description."},
                "include_archived": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
    ),
]


def labels_list(user: User, arguments: dict[str, Any]) -> Any:
    """List labels the user can see (across or within one workspace)."""
    from apps.labels.models import Label

    args = arguments or {}
    qs = Label.objects.filter(workspace_id__in=user_workspace_ids(user)).select_related("workspace", "group")
    ws = args.get("workspace")
    if ws:
        qs = qs.filter(workspace__slug=ws)
    return [
        {
            "id": label.id,
            "name": label.name,
            "color": label.color,
            "workspace_slug": label.workspace.slug,
            "group_name": label.group.name if label.group_id else None,
        }
        for label in qs.order_by("workspace__name", "name")
    ]


CALLABLES: dict[str, Callable[[User, dict[str, Any]], Any]] = {
    "acta_workspaces_list": workspaces_list,
    "acta_projects_list": projects_list,
    "acta_tasks_list": tasks_list,
    "acta_task_get": task_get,
    "acta_activity_list": activity_list,
    "acta_comments_list": comments_list,
    "acta_labels_list": labels_list,
}


__all__ = ["TOOLS", "CALLABLES"]
