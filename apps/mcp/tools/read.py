"""Read-only MCP tools for Acta.

List / get / search endpoints — every callable here is pure SELECTs
through Django ORM, no side effects. Mirrors the same workspace-
membership scoping the web UI applies. Pair with
:mod:`apps.mcp.tools.write` for the create / update / delete side.
"""

from __future__ import annotations

import datetime
import re
from typing import Any, Callable

from mcp.types import Tool

from apps.accounts.models import User
from apps.mcp.tools._shared import (
    resolve_milestone,
    resolve_project,
    resolve_task,
    resolve_workspace,
    serialize_milestone,
    user_workspace_ids,
)
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
            "description": p.description or "",
            "workspace_slug": p.workspace.slug,
            "workspace_name": p.workspace.name,
            "lead_username": p.lead.username if p.lead_id else None,
            "archived": p.archived,
        }
        for p in qs.order_by("workspace__name", "name")
    ]


def project_get(user: User, arguments: dict[str, Any]) -> Any:
    """Return the full payload for one project: meta + members + task counts.

    Complements ``acta_projects_list`` (which omits the description and
    membership) for flows that need to read or reason about a single
    project in depth — its Markdown description, lead, member roster, and
    open/closed task tallies. Scoped to the user's workspaces.
    """
    from django.db.models import Count, Q

    from apps.projects.models import Project
    from apps.tasks.models import Task

    slug_prefix = (arguments or {}).get("slug_prefix")
    if not slug_prefix:
        raise ValueError("Argument 'slug_prefix' is required (e.g. 'ACTA').")
    try:
        project = (
            Project.objects.filter(workspace_id__in=user_workspace_ids(user))
            .select_related("workspace", "lead")
            .prefetch_related("members")
            .get(slug_prefix=slug_prefix)
        )
    except Project.DoesNotExist:
        raise ValueError(f"Project {slug_prefix!r} not found or not accessible to this user.")

    counts = (
        Task.objects.work()
        .filter(project=project, archived_at__isnull=True)
        .aggregate(
            total=Count("id"),
            done=Count("id", filter=Q(status=Task.STATUS_DONE)),
            cancelled=Count("id", filter=Q(status=Task.STATUS_CANCELLED)),
        )
    )
    open_count = counts["total"] - counts["done"] - counts["cancelled"]

    return {
        "id": project.id,
        "slug_prefix": project.slug_prefix,
        "name": project.name,
        "description": project.description or "",
        "icon": project.icon or "",
        "icon_color": project.icon_color or "",
        "workspace_slug": project.workspace.slug,
        "workspace_name": project.workspace.name,
        "lead_username": project.lead.username if project.lead_id else None,
        "lead_display_name": project.lead.display_name if project.lead_id else None,
        "archived": project.archived,
        "notify_members_only": project.notify_members_only,
        "created_at": project.created_at.isoformat(),
        "members": [
            {
                "username": m.username,
                "display_name": m.display_name,
            }
            for m in project.members.all()
        ],
        "task_counts": {
            "total": counts["total"],
            "open": open_count,
            "done": counts["done"],
            "cancelled": counts["cancelled"],
        },
    }


def workspace_members_list(user: User, arguments: dict[str, Any]) -> Any:
    """List the members of one workspace with their roles.

    Closes the gap that forced assignee discovery to scrape distinct
    usernames off task rows. Required: ``workspace`` (slug). Returns the
    roster sorted lead-agnostic by role rank (owner, admin, member) then
    display name — handy for ranking assignee pickers.

    Each row carries ``is_you``, true on the caller's own membership.
    Without it a client resolving "assign this to me" off the roster
    has nothing to match against and picks whoever sorts first — which
    is always the workspace owner.
    """
    from apps.workspaces.models import WorkspaceMember

    args = arguments or {}
    workspace = resolve_workspace(user, args.get("workspace") or "")

    role_rank = {
        WorkspaceMember.OWNER: 0,
        WorkspaceMember.ADMIN: 1,
        WorkspaceMember.MEMBER: 2,
    }
    members = (
        WorkspaceMember.objects.filter(workspace=workspace)
        .select_related("user")
        .order_by("role", "user__first_name", "user__username")
    )
    rows = [
        {
            "username": m.user.username,
            "display_name": m.user.display_name,
            "role": m.role,
            "is_active": m.user.is_active,
            "is_you": m.user_id == user.id,
            "joined_at": m.joined_at.isoformat(),
        }
        for m in members
    ]
    rows.sort(key=lambda r: (role_rank.get(r["role"], 9), r["display_name"].lower()))
    return rows


def project_updates_list(user: User, arguments: dict[str, Any]) -> Any:
    """List the Linear-style status updates posted on a project.

    These are the ``ProjectUpdate`` posts (health signal + Markdown body
    + optional frozen stats), distinct from the activity log. Required:
    ``project`` (slug prefix). Optional ``limit`` (default 20, max 100).
    """
    from apps.projects.models import Project, ProjectUpdate

    args = arguments or {}
    project_prefix = args.get("project")
    if not project_prefix:
        raise ValueError("Argument 'project' is required (slug prefix, e.g. 'ACTA').")
    if not Project.objects.filter(
        slug_prefix=project_prefix,
        workspace_id__in=user_workspace_ids(user),
    ).exists():
        raise ValueError(f"Project {project_prefix!r} not found or not accessible to this user.")

    limit = min(int(args.get("limit", 20)), 100)
    qs = (
        ProjectUpdate.objects.filter(project__slug_prefix=project_prefix)
        .select_related("author")
        .order_by("-created_at", "-id")[:limit]
    )
    return [
        {
            "id": u.id,
            "health": u.health,
            "body": u.body,
            "stats": u.stats or {},
            "author_username": u.author.username if u.author_id else None,
            "author_display_name": u.author.display_name if u.author_id else None,
            "edited": u.was_edited,
            "created_at": u.created_at.isoformat(),
            "updated_at": u.updated_at.isoformat(),
        }
        for u in qs
    ]


def tasks_list(user: User, arguments: dict[str, Any]) -> Any:
    """List tasks the user can access, filtered by the supplied query."""
    args = arguments or {}
    qs = (
        Task.objects.filter(project__workspace_id__in=user_workspace_ids(user))
        .select_related("project__workspace", "assignee", "epic__project", "milestone")
        .prefetch_related("labels")
    )
    # Epics are hidden unless asked for, the same convention this tool
    # already uses for cancelled: an agent told to count the work must
    # not count the umbrellas over it. ``kind="epic"`` lists only epics,
    # ``kind="all"`` lists both. See docs/decisions/0036-epics.md.
    kind = (args.get("kind") or "").strip().lower()
    if kind == Task.KIND_EPIC:
        qs = qs.epics()
    elif kind != "all":
        qs = qs.work()
    project = args.get("project")
    if project:
        qs = qs.filter(project__slug_prefix=project)
    epic = args.get("epic")
    if epic:
        prefix, _, number = str(epic).rpartition("-")
        qs = qs.filter(epic__project__slug_prefix=prefix, epic__number=number or 0)
    status = args.get("status")
    if isinstance(status, str):
        qs = qs.filter(status=status)
    elif isinstance(status, list):
        qs = qs.filter(status__in=status)
    else:
        # No explicit status filter — hide the terminal ``cancelled`` state
        # by default, matching the web list. Naming it in ``status`` still
        # surfaces cancelled tasks.
        qs = qs.exclude(status=Task.STATUS_CANCELLED)
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
            "kind": t.kind,
            # An epic's own state is read off the tasks it collects, so
            # the row carries the rollup rather than the stored status.
            "progress": (lambda c: {"done": c[0], "total": c[1]})(t.epic_counts) if t.kind == Task.KIND_EPIC else None,
            "epic_slug": t.epic.slug if t.epic_id else None,
            "status": t.epic_status if t.kind == Task.KIND_EPIC else t.status,
            "priority": t.priority,
            "size": t.size,
            "start_date": t.start_date.isoformat() if t.start_date else None,
            "end_date": t.end_date.isoformat() if t.end_date else None,
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
    qs = qs.order_by("-created_at", "-id")[:limit]

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
    qs = qs.order_by("-created_at", "-id")[:limit]

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
        "start_date": task.start_date.isoformat() if task.start_date else None,
        "end_date": task.end_date.isoformat() if task.end_date else None,
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
        "kind": task.kind,
        "epic_slug": task.epic.slug if task.epic_id else None,
        # Present only on an epic, and computed: progress, the date span
        # and the status are read off the tasks it collects.
        "epic": (
            {
                "done": task.epic_counts[0],
                "total": task.epic_counts[1],
                "status": task.epic_status,
                "start_date": task.epic_span[0].isoformat() if task.epic_span[0] else None,
                "end_date": task.epic_span[1].isoformat() if task.epic_span[1] else None,
                # The members, with who is carrying each — the same
                # fields ``subtasks`` below carries, so a caller does not
                # have to fetch every task to learn who is on them.
                "tasks": [
                    {
                        "slug": m.slug,
                        "title": m.title,
                        "status": m.status,
                        "project": m.project.slug_prefix,
                        "assignee_username": m.assignee.username if m.assignee_id else None,
                    }
                    for m in task.epic_members()
                    .select_related("project", "assignee")
                    .order_by("project__slug_prefix", "number")
                ],
            }
            if task.kind == Task.KIND_EPIC
            else None
        ),
        "subtasks": [
            {
                "slug": s.slug,
                "title": s.title,
                "status": s.status,
                "priority": s.priority,
                "assignee_username": s.assignee.username if s.assignee_id else None,
                "start_date": s.start_date.isoformat() if s.start_date else None,
                "end_date": s.end_date.isoformat() if s.end_date else None,
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


#: How many words out of the text get a literal search of their own.
#: Four covers the distinctive part of a title without turning one tool
#: call into a dozen queries.
_LITERAL_PROBES = 4

#: Shortest word worth searching for literally. Below this they are
#: prepositions and the search returns the whole board.
_LITERAL_MIN_LENGTH = 5

#: How alike two words have to be, by trigrams, to count as the same
#: word. A one-letter typo in a long word lands around 0.8 and an
#: inflected ending higher still, while unrelated words sit under 0.3 —
#: so this sits between them with room on both sides.
_LITERAL_MIN_SIMILARITY = 0.5


def _literal_matches(text: str, workspace_id: int, limit: int, exclude_ids) -> dict:
    """Return tasks whose text literally carries the query's rarest words.

    The vector side answers "something like this", which is what it is
    for — but it answers it about the *shape* of a sentence. A long
    enumerated title averages out into a vector near nothing in
    particular, and the one word that carried the meaning drowns. Asking
    the database for that word finds the task named almost exactly it.

    Matched by trigrams rather than by substring, because a typo and an
    inflected ending are the same kind of difference and neither survives
    an exact match: the query says "кваліфікацій" where the board says
    "кваліфікації", and a misspelling of either matches nothing at all.
    The embedding cannot cover this — it knows what words mean, not how
    they are spelled.

    One query, with a similarity column per probe, so the caller learns
    both how close the best word came and how many of them landed.

    Args:
        text: What the new task would say.
        workspace_id: The workspace to look in.
        limit: How many rows to return.
        exclude_ids: Tasks to leave out — the anchor, usually.

    Returns:
        ``{task_id: {"task": Task, "words": int, "similarity": float}}``.
    """
    from django.contrib.postgres.search import TrigramWordSimilarity
    from django.db.models import Q

    words = {
        word.lower()
        for word in re.findall(r"\w+", text, re.UNICODE)
        if len(word) >= _LITERAL_MIN_LENGTH and not word.isdigit()
    }
    if not words:
        return {}
    # Longest first: in these titles the long word is the rare one, and
    # the rare one is what tells two tasks apart.
    probes = sorted(words, key=len, reverse=True)[:_LITERAL_PROBES]
    columns = {f"word_{index}": TrigramWordSimilarity(word, "title") for index, word in enumerate(probes)}
    close_enough = Q()
    for column in columns:
        close_enough |= Q(**{f"{column}__gte": _LITERAL_MIN_SIMILARITY})
    rows = (
        Task.objects.work()
        .filter(project__workspace_id=workspace_id, archived_at__isnull=True, recurrence__isnull=True)
        .exclude(id__in=list(exclude_ids))
        .annotate(**columns)
        .filter(close_enough)
        .select_related("project__workspace", "assignee")
        .order_by()[: limit * 2]
    )
    found = {}
    for task in rows:
        scores = [getattr(task, column) or 0.0 for column in columns]
        found[task.id] = {
            "task": task,
            "words": sum(1 for score in scores if score >= _LITERAL_MIN_SIMILARITY),
            "similarity": round(max(scores), 3),
        }
    return found


def tasks_find_similar(user: User, arguments: dict[str, Any]) -> Any:
    """Find the tasks that already say something close to this text.

    Meant to be called BEFORE creating a task, and it searches twice
    because one search is not enough. A multilingual embedding knows
    that "аудит сегментации сети" and *Network segmentation audit* are
    the same job, which no substring search can. It also averages a long
    enumerated title into a vector near nothing in particular, which is
    how a board holding "Ingest: імпорт кваліфікації особи" answered
    "nothing found" to a query about importing qualifications. The
    literal pass covers that: the rare words of the query, stemmed,
    straight against the titles.

    Either ``text`` (what the new task would say) or ``slug`` (neighbours
    of an existing task) is required. Returns candidates with a
    ``score`` between 0 and 1 — a ranking, not a verdict. Deciding
    whether two tasks are the same piece of work is the caller's job.

    An empty ``matches`` arrives with a ``note`` whenever the lookup
    could not actually run — no host configured, the host unreachable,
    or the workspace carrying no vectors yet. Without that distinction a
    caller reads silence as "no duplicates" and creates the duplicate it
    was asked to prevent. No ``note`` means the comparison ran and
    nothing was close.
    """
    from apps.tasks import similarity

    args = arguments or {}
    text = (args.get("text") or "").strip()
    slug = (args.get("slug") or "").strip()
    if not text and not slug:
        raise ValueError("Pass 'text' (what the task would say) or 'slug' (an existing task).")

    limit = min(int(args.get("limit") or 5), 25)
    workspace_ids = user_workspace_ids(user)

    if slug:
        anchor = resolve_task(user, slug)
        workspace_id = anchor.project.workspace_id
        text = similarity.text_for(anchor)
        exclude = [anchor.pk]
    else:
        workspace = args.get("workspace")
        if workspace:
            workspace_id = resolve_workspace(user, workspace).id
        elif args.get("project"):
            project = resolve_project(user, args["project"])
            workspace_id = project.workspace_id
        elif len(workspace_ids) == 1:
            workspace_id = workspace_ids[0]
        else:
            raise ValueError("Pass 'workspace' or 'project' — you are a member of more than one workspace.")
        exclude = []

    # Two searches, because they fail in opposite directions. The vector
    # one knows that "аудит сегментации сети" and "Network segmentation
    # audit" are the same job, and misses a task named almost exactly the
    # query when the query is a long enumeration — the distinctive word
    # averages away. The literal one has no idea about languages and
    # cannot miss a word that is right there. Running only the first is
    # how a duplicate check reports "nothing found" about a task called
    # almost the same thing.
    by_meaning, meaning_failed = similarity.neighbours_with_reason(
        text,
        workspace_id=workspace_id,
        limit=limit,
        exclude_ids=exclude,
    )
    by_words = _literal_matches(text, workspace_id, limit, exclude)
    tasks = {
        task.pk: task
        for task in Task.objects.filter(pk__in=[task_id for task_id, _ in by_meaning]).select_related(
            "project__workspace",
            "assignee",
        )
    }
    tasks.update({task_id: row["task"] for task_id, row in by_words.items()})

    rows = []
    scored = dict(by_meaning)
    for task_id, task in tasks.items():
        score = scored.get(task_id)
        words = by_words.get(task_id, {}).get("words", 0)
        rows.append(
            {
                "slug": task.slug,
                "title": task.title,
                "status": task.status,
                "project_slug_prefix": task.project.slug_prefix,
                "assignee_username": task.assignee.username if task.assignee_id else None,
                "updated_at": task.updated_at.isoformat(),
                "score": round(score, 3) if score is not None else None,
                # Why this row is here, so the caller can weigh it: a task
                # found both ways is the strongest candidate there is.
                "via": "both" if score is not None and words else ("meaning" if score is not None else "words"),
                "words_matched": words or None,
            },
        )
    order = {"both": 0, "meaning": 1, "words": 2}
    rows.sort(key=lambda row: (order[row["via"]], -(row["score"] or 0), -(row["words_matched"] or 0)))
    rows = rows[:limit]

    # The note rides along even when the word side found something: a
    # caller that was told "here is what matched" has no way to know one
    # of the two searches never ran, and half a duplicate check reads
    # exactly like a whole one.
    if meaning_failed:
        return {"matches": rows, "note": meaning_failed}
    return {"matches": rows}


def milestones_list(user: User, arguments: dict[str, Any]) -> Any:
    """List milestones, newest date last, with progress and risk."""
    from apps.milestones.models import Milestone

    args = arguments or {}
    qs = Milestone.objects.filter(workspace_id__in=user_workspace_ids(user))
    if args.get("workspace"):
        qs = qs.filter(workspace=resolve_workspace(user, args["workspace"]))
    if args.get("project"):
        qs = qs.filter(projects=resolve_project(user, args["project"]))
    rows = [serialize_milestone(m) for m in qs.prefetch_related("projects").distinct()]
    state = args.get("state")
    if state:
        rows = [row for row in rows if row["state"] == state]
    return rows


def milestone_get(user: User, arguments: dict[str, Any]) -> Any:
    """Read one milestone with its breakdown and what is at risk."""
    args = arguments or {}
    milestone_id = args.get("milestone_id")
    if not milestone_id:
        raise ValueError("Argument 'milestone_id' is required.")
    return serialize_milestone(resolve_milestone(user, milestone_id), detail=True)


TOOLS: list[Tool] = [
    Tool(
        name="acta_milestones_list",
        description=(
            "List milestones — the dates work aims at. A milestone is a POINT, not a "
            "span: one target date by which something must be true, scoped to one or "
            "more projects. One project is a local checkpoint; several is a shared "
            "commitment every one of them shows. "
            "Optional: ``workspace`` (slug), ``project`` (slug prefix — milestones that "
            "cover it), ``state`` (``open``, ``today``, ``overdue``, ``complete``, "
            "``closed``). "
            "Each row carries ``target_date``, ``days_left`` (negative once past), "
            "``state``, ``done``/``total`` over the work that counts, ``at_risk`` and "
            "the project scope. Progress excludes cancelled work, counts archived work "
            "that is done, and drops archived work that is not."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace slug."},
                "project": {"type": "string", "description": "Project slug prefix, e.g. ACTA."},
                "state": {
                    "type": "string",
                    "enum": ["open", "today", "overdue", "complete", "closed"],
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_milestone_get",
        description=(
            "Read one milestone in full: its goal, scope, progress, the breakdown by "
            "project and by epic, and ``at_risk_tasks`` — the unfinished work that will "
            "not make the date. Risk has two sides: a task whose own due date falls "
            "after the milestone's date contradicts the plan, and once the date has "
            "passed every still-open task is late whatever its due date says. "
            "Required: ``milestone_id`` (from ``acta_milestones_list``)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"milestone_id": {"type": "integer"}},
            "required": ["milestone_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_tasks_find_similar",
        description=(
            "Find tasks that already say what you are about to say. CALL THIS "
            "BEFORE ``acta_task_create``. Pass ``text`` (what the new task would "
            "say) or ``slug`` (neighbours of an existing task), plus "
            "``workspace`` or ``project`` when you are a member of more than one "
            "workspace. "
            "It searches TWO ways and merges the result, because each way misses "
            "what the other catches: by meaning, which knows that 'аудит "
            "сегментации сети' and 'Network segmentation audit' are one job, and "
            "by word, which cannot miss a term that is literally in both titles "
            "even when a long enumerated title drowns it for the embedding. "
            "``via`` says which found the row: ``both`` is the strongest "
            "candidate there is, ``meaning`` carries a ``score`` from 0 to 1, "
            "``words`` carries ``words_matched`` instead. None of them is a "
            "verdict — read the titles and decide. "
            "An empty ``matches`` with a ``note`` means the check could not run "
            "(no host, host down, no vectors): that is NOT 'no duplicates', and "
            "creating on the strength of it is how duplicates get filed. "
            "Archived tasks and the copies generated by a recurring rule are "
            "left out."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Title (plus description) of the task you are about to create.",
                },
                "slug": {"type": "string", "description": "Existing task whose neighbours you want, e.g. ACTA-128."},
                "workspace": {"type": "string", "description": "Workspace slug or name to search in."},
                "project": {
                    "type": "string",
                    "description": "Project slug prefix; narrows to that project's workspace.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Maximum matches (default 5).",
                },
            },
            "additionalProperties": False,
        },
    ),
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
            "Returns ``[{id, slug_prefix, name, description, workspace_slug, workspace_name, "
            "lead_username, archived}, …]``. ``slug_prefix`` (e.g. ``ACTA``) is the "
            "identifier to pass into ``acta_tasks_list``. For the member roster + task "
            "counts of a single project use ``acta_project_get``."
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
        name="acta_project_get",
        description=(
            "Return the FULL payload for one project — meta, Markdown description, "
            "lead, member roster, and open/done/cancelled task counts. Use this when "
            "``acta_projects_list`` (which omits description + members) isn't enough. "
            "Required: ``slug_prefix`` (e.g. ``ACTA``). Returns ``{id, slug_prefix, "
            "name, description, icon, icon_color, workspace_slug, workspace_name, "
            "lead_username, lead_display_name, archived, notify_members_only, "
            "created_at, members: [{username, display_name}], task_counts: "
            "{total, open, done, cancelled}}``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "slug_prefix": {"type": "string", "description": "Project slug prefix, e.g. 'ACTA'."},
            },
            "required": ["slug_prefix"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_workspace_members_list",
        description=(
            "List the members of one workspace with their roles. Use this to "
            "discover who can be assigned tasks or to rank an assignee picker — "
            "instead of scraping distinct usernames off task rows. Required: "
            "``workspace`` (slug). Returns ``[{username, display_name, role "
            "(owner/admin/member), is_active, is_you, joined_at}, …]`` ordered "
            "owner → admin → member, then by display name. ``is_you`` marks the "
            "caller's own row — use it, or the ``me`` alias the write tools "
            "accept, instead of guessing which member the user means by 'me'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "workspace": {"type": "string", "description": "Workspace slug."},
            },
            "required": ["workspace"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="acta_project_updates_list",
        description=(
            "List the Linear-style status updates posted on a project (the "
            "``ProjectUpdate`` posts — health signal + Markdown body + optional "
            "frozen stats), NEWEST first. Distinct from ``acta_activity_list`` "
            "(the audit log) — these are deliberate human-written status posts. "
            "Required: ``project`` (slug prefix). Optional: ``limit`` (default 20, "
            "max 100). Returns ``[{id, health (on_track/at_risk/off_track/"
            "completed), body, stats, author_username, author_display_name, edited, "
            "created_at, updated_at}, …]``. To post one, use ``acta_project_post_update``."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug prefix (e.g. ACTA)."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["project"],
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
            "actor_display_name, payload, created_at}]}``. "
            "``epic_slug`` names the epic holding this task, if any. On an EPIC the "
            "``epic`` block carries its whole state: ``{done, total, status, start_date, "
            "end_date, tasks}`` — all computed from the work it collects, and ``tasks`` "
            "IS the member list, one entry per task it holds "
            "``{slug, title, status, project, assignee_username}``. A plain task has "
            "``epic: null``."
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
        name="acta_label_groups_list",
        description=(
            "List label groups the user can see. Optional ``workspace`` slug to "
            "scope to one workspace. Returns ``[{id, name, description, "
            "is_exclusive, workspace_slug, label_count}, …]`` sorted by workspace "
            "name then group name. Pair with ``acta_label_group_create`` (write)."
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
            "``status`` (one of planned/ready/to-do/in-progress/in-review/done/cancelled, or "
            "list; cancelled tasks are hidden unless you ask for them explicitly), "
            "``priority`` (1=Urgent..4=Low, or list), ``assignee`` (username, ``me``, or ``unassigned``), "
            "``q`` (case-insensitive title/description search), "
            "``include_archived`` (default false), ``limit`` (default 50, max 200). "
            "``kind``: epics are an umbrella over work, not work, so they are LEFT OUT by "
            "default — pass ``kind='epic'`` to list only epics or ``kind='all'`` for both. "
            "``epic`` (an epic's slug) narrows to the tasks that epic collects. "
            "Returns ``[{slug, title, kind, status, progress, epic_slug, priority, size, due_date, "
            "assignee_username, project_slug_prefix, project_name, workspace_slug, "
            "labels: [{name, color}], updated_at}, …]`` sorted by most-recently-updated first. "
            "On an epic row ``status`` and ``progress`` ({done, total}) are computed from the "
            "tasks it collects, never stored; ``progress`` is null on a plain task."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project slug prefix (e.g. ACTA)."},
                "status": {
                    "oneOf": [
                        {
                            "type": "string",
                            "enum": ["planned", "ready", "to-do", "in-progress", "in-review", "done", "cancelled"],
                        },
                        {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["planned", "ready", "to-do", "in-progress", "in-review", "done", "cancelled"],
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
                "kind": {
                    "type": "string",
                    "enum": ["task", "epic", "all"],
                    "description": "Default 'task' — epics are left out unless asked for.",
                },
                "epic": {"type": "string", "description": "Epic slug; lists the tasks it collects."},
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


def label_groups_list(user: User, arguments: dict[str, Any]) -> Any:
    """List label groups the user can see (across or within one workspace)."""
    from django.db.models import Count

    from apps.labels.models import LabelGroup

    args = arguments or {}
    qs = (
        LabelGroup.objects.filter(workspace_id__in=user_workspace_ids(user))
        .select_related("workspace")
        .annotate(label_count=Count("labels"))
    )
    ws = args.get("workspace")
    if ws:
        qs = qs.filter(workspace__slug=ws)
    return [
        {
            "id": g.id,
            "name": g.name,
            "description": g.description,
            "is_exclusive": g.is_exclusive,
            "workspace_slug": g.workspace.slug,
            "label_count": g.label_count,
        }
        for g in qs.order_by("workspace__name", "name")
    ]


CALLABLES: dict[str, Callable[[User, dict[str, Any]], Any]] = {
    "acta_workspaces_list": workspaces_list,
    "acta_projects_list": projects_list,
    "acta_project_get": project_get,
    "acta_workspace_members_list": workspace_members_list,
    "acta_project_updates_list": project_updates_list,
    "acta_tasks_list": tasks_list,
    "acta_task_get": task_get,
    "acta_tasks_find_similar": tasks_find_similar,
    "acta_activity_list": activity_list,
    "acta_comments_list": comments_list,
    "acta_labels_list": labels_list,
    "acta_label_groups_list": label_groups_list,
    "acta_milestones_list": milestones_list,
    "acta_milestone_get": milestone_get,
}


__all__ = ["TOOLS", "CALLABLES"]
