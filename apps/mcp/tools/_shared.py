"""Helpers shared between the read and write tool modules.

Keep this tiny — it's just the bits both directions need (user scope,
slug lookup, payload shaper). Anything else lives in its own module.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.accounts.models import User
from apps.tasks.models import Task


def user_workspace_ids(user: User) -> list[int]:
    """Return the workspace ids the user belongs to.

    Computed once per tool call and used as ``workspace_id__in=…``
    instead of joining through ``workspace__memberships__user``. Two
    queries instead of one big JOIN, but each query is index-direct
    and the join chain in downstream filters drops by two levels —
    net win, especially because the deep JOIN forces a ``DISTINCT``
    pass (memberships can multiply rows).
    """
    return list(user.workspace_memberships.values_list("workspace_id", flat=True))


def resolve_project(user: User, slug_prefix: str):
    """Look up a project by ``slug_prefix``, scoped to the user's workspaces.

    Raises ``ValueError`` (not 404) — MCP wraps thrown exceptions as
    tool-call errors with a readable message for the client.
    """
    from apps.projects.models import Project

    try:
        return Project.objects.get(
            slug_prefix=slug_prefix,
            workspace_id__in=user_workspace_ids(user),
        )
    except Project.DoesNotExist:
        raise ValueError(f"Project {slug_prefix!r} not found or not accessible to this user.")
    except Project.MultipleObjectsReturned:
        # Prefixes are unique per workspace, not globally, so a user in
        # two workspaces that both have this prefix matches twice. Refuse
        # rather than guess — picking one silently would let a write tool
        # edit the wrong project.
        where = ", ".join(
            sorted(
                p.workspace.slug
                for p in Project.objects.filter(
                    slug_prefix=slug_prefix,
                    workspace_id__in=user_workspace_ids(user),
                ).select_related("workspace")
            )
        )
        raise ValueError(
            f"Project {slug_prefix!r} is ambiguous — that prefix exists in more than one "
            f"workspace you belong to ({where}). Slug prefixes are unique per workspace, "
            "not globally."
        )


SELF_USERNAME_ALIAS = "me"


def resolve_user_by_username(username: str):
    """Look up a User by username; raise ``ValueError`` if not found."""
    try:
        return User.objects.get(username=username)
    except User.DoesNotExist:
        raise ValueError(f"User {username!r} does not exist.")


def resolve_user_reference(actor: User, username: str):
    """Resolve a username argument, honouring the ``me`` self-alias.

    Write tools take collaborators by username, but an LLM asked to
    "assign it to me" has no way to turn that into one — it either
    calls ``acta_ping`` or guesses off the member roster, and guessing
    picks whoever sorts first (the workspace owner). Accepting ``me``
    removes the guess, and mirrors the ``assignee: "me"`` filter the
    read tools already support.

    ``me`` is a reserved word here: it wins over a real account that
    happens to be named ``me``, exactly as it does when filtering.

    Args:
        actor: The authenticated user behind this tool call.
        username: A username, or ``me`` for ``actor``.

    Returns:
        The resolved :class:`~apps.accounts.models.User`.

    Raises:
        ValueError: If no user with that username exists.
    """
    if username == SELF_USERNAME_ALIAS:
        return actor
    return resolve_user_by_username(username)


def resolve_workspace(user: User, slug: str):
    """Look up a workspace by ``slug``, scoped to the user's memberships.

    Raises ``ValueError`` (not 404) so the MCP layer surfaces it as a
    readable tool-call error.
    """
    from apps.workspaces.models import Workspace

    try:
        return Workspace.objects.get(slug=slug, id__in=user_workspace_ids(user))
    except Workspace.DoesNotExist:
        raise ValueError(f"Workspace {slug!r} not found or not accessible to this user.")


def is_workspace_admin(user: User, workspace) -> bool:
    """Return ``True`` if ``user`` is an owner or admin of ``workspace``.

    Mirrors the web's ``_user_is_workspace_admin`` gate so MCP-driven
    writes obey the same role matrix (see docs/decisions/0010-permissions.md).
    """
    from apps.workspaces.models import WorkspaceMember

    return WorkspaceMember.objects.filter(
        user=user,
        workspace=workspace,
        role__in=[
            WorkspaceMember.OWNER,
            WorkspaceMember.ADMIN,
        ],
    ).exists()


def as_cleared(value):
    """Return ``True`` when a nullable slug argument means "clear it".

    JSON ``null`` is the documented way, and the one the schema declares.
    Several MCP clients serialise it as the string ``"null"`` instead,
    which used to come back as ``Invalid task slug: 'null'`` — a refusal
    that reads like the caller's slug was wrong rather than their null.
    Accepting the string costs nothing: a slug is ``PREFIX-NUMBER``, so
    ``"null"`` can never name a real task.

    Args:
        value: The argument as the client sent it.

    Returns:
        ``True`` for ``None``, an empty string, ``"null"`` or ``"none"``.
    """
    if value is None:
        return True
    return isinstance(value, str) and value.strip().lower() in {"", "null", "none"}


def resolve_task(user: User, slug: str):
    """Look up a Task by ``PREFIX-NUMBER`` slug, scoped to the user's workspaces."""
    try:
        prefix, number = slug.rsplit("-", 1)
        number_int = int(number)
    except (ValueError, AttributeError):
        raise ValueError(f"Invalid task slug: {slug!r}. Expected 'PREFIX-NUMBER'.")
    try:
        return Task.objects.get(
            project__slug_prefix=prefix,
            number=number_int,
            project__workspace_id__in=user_workspace_ids(user),
        )
    except Task.DoesNotExist:
        raise ValueError(f"Task {slug!r} not found or not accessible to this user.")
    except Task.MultipleObjectsReturned:
        # See ``resolve_project`` — same per-workspace uniqueness trap.
        where = ", ".join(
            sorted(
                t.project.workspace.slug
                for t in Task.objects.filter(
                    project__slug_prefix=prefix,
                    number=number_int,
                    project__workspace_id__in=user_workspace_ids(user),
                ).select_related("project__workspace")
            )
        )
        raise ValueError(
            f"Task {slug!r} is ambiguous — that project prefix exists in more than one "
            f"workspace you belong to ({where}). Slug prefixes are unique per workspace, "
            "not globally."
        )


def task_url(task: Task) -> str | None:
    """Return the absolute URL of a task's detail page.

    MCP tools answer outside any request, so the origin can't come from
    ``request.build_absolute_uri`` — it comes from the deployment's
    ``ACTA_PUBLIC_BASE_URL``, the same setting Telegram notifications use
    for their links. Returns ``None`` when that setting is empty (local
    runs that never set it), rather than emitting a path that looks
    clickable but goes nowhere.

    Args:
        task: The task to link to.

    Returns:
        The absolute URL, or ``None`` if no public base URL is configured.
    """
    from apps.web.url_scoping import task_path

    base = getattr(settings, "ACTA_PUBLIC_BASE_URL", "")
    if not base:
        return None
    return base.rstrip("/") + task_path(task)


def serialize_task_summary(task: Task) -> dict[str, Any]:
    """Compact task-summary payload — matches ``acta_tasks_list`` rows.

    Write tools return this shape so LLM-driven workflows can chain
    create / update calls without restructuring the data each step.
    ``url`` lets a client hand the human a link straight to the task it
    just created, instead of making them reconstruct one from the slug.
    """
    return {
        "slug": task.slug,
        "url": task_url(task),
        "title": task.title,
        # Always present, including on a plain task: without it the
        # caller cannot tell a conversion that happened from one that was
        # ignored, which is how a silent no-op gets reported as success.
        "kind": task.kind,
        "epic_slug": task.epic.slug if task.epic_id else None,
        "status": task.epic_status if task.kind == Task.KIND_EPIC else task.status,
        "priority": task.priority,
        "size": task.size,
        "start_date": task.start_date.isoformat() if task.start_date else None,
        "end_date": task.end_date.isoformat() if task.end_date else None,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "assignee_username": task.assignee.username if task.assignee_id else None,
        "project_slug_prefix": task.project.slug_prefix,
        "workspace_slug": task.project.workspace.slug,
        "labels": [{"name": label.name, "color": label.color} for label in task.labels.all()],
        "updated_at": task.updated_at.isoformat(),
    }


class FakeRequest:
    """Minimal stand-in for ``rest_framework.request.Request`` so we can
    drive :class:`TaskSerializer` (which expects ``context["request"].user``)
    from an MCP tool without going through DRF's view layer.
    """

    def __init__(self, user: User):
        self.user = user
        self.query_params: dict[str, str] = {}


def resolve_milestone(user: User, milestone_id):
    """Load a milestone in one of the user's workspaces.

    Args:
        user: The authenticated MCP user.
        milestone_id: Primary key of the milestone.

    Returns:
        The :class:`~apps.milestones.models.Milestone`, scope prefetched.

    Raises:
        ValueError: If no such milestone is visible to this user.
    """
    from apps.milestones.models import Milestone

    try:
        return Milestone.objects.prefetch_related("projects").get(
            pk=milestone_id,
            workspace_id__in=user_workspace_ids(user),
        )
    except Milestone.DoesNotExist as exc:
        raise ValueError(f"Milestone {milestone_id} not found in your workspaces.") from exc


def milestone_at_risk(milestone, today=None):
    """Return the counted, unfinished work that will not make the date.

    Thin delegate to :func:`apps.milestones.services.at_risk` — the
    pages and these tools must answer "what is at risk" identically, and
    two copies of the rule is how a container once read ``1/3`` in one
    place and ``0/2`` in another.

    Args:
        milestone: The milestone to examine.
        today: Reference date; defaults to the local current date.

    Returns:
        A list of ``(task, days_over, why)`` tuples, worst first.
    """
    from apps.milestones.services import at_risk

    return at_risk(milestone, today=today)


def serialize_milestone(milestone, *, detail: bool = False):
    """Serialise a milestone for the MCP payloads.

    Args:
        milestone: The milestone to serialise.
        detail: Include the per-project and per-epic breakdown plus the
            at-risk list.

    Returns:
        A JSON-serialisable dict.
    """
    from django.utils import timezone

    from apps.tasks.models import Task

    today = timezone.localdate()
    done, total = milestone.counts()
    at_risk = milestone_at_risk(milestone, today=today)
    payload = {
        "id": milestone.id,
        "name": milestone.name,
        "goal": milestone.goal,
        "target_date": milestone.target_date.isoformat(),
        "days_left": (milestone.target_date - today).days,
        "state": milestone.state(today=today),
        "projects": sorted(p.slug_prefix for p in milestone.projects.all()),
        "owner_username": milestone.owner.username if milestone.owner else None,
        "done": done,
        "total": total,
        "at_risk": len(at_risk),
    }
    if not detail:
        return payload
    counted = milestone.counted_tasks().select_related("project", "epic")
    by_project: dict[str, dict] = {}
    by_epic: dict[str, dict] = {}
    for task in counted:
        key = task.project.slug_prefix
        row = by_project.setdefault(key, {"project": key, "done": 0, "total": 0})
        row["total"] += 1
        row["done"] += task.status == Task.STATUS_DONE
        ekey = task.epic.slug if task.epic_id else None
        erow = by_epic.setdefault(
            ekey or "",
            {"epic_slug": ekey, "epic_title": task.epic.title if task.epic_id else None, "done": 0, "total": 0},
        )
        erow["total"] += 1
        erow["done"] += task.status == Task.STATUS_DONE
    payload.update(
        {
            "description": milestone.description,
            "closed": milestone.is_closed,
            "by_project": sorted(by_project.values(), key=lambda row: row["project"]),
            "by_epic": sorted(by_epic.values(), key=lambda row: -row["total"]),
            "at_risk_tasks": [
                {
                    "slug": task.slug,
                    "title": task.title,
                    "status": task.status,
                    "due_date": task.due_date.isoformat() if task.due_date else None,
                    "days_over": days,
                    "why": why,
                }
                for task, days, why in at_risk[:50]
            ],
        },
    )
    return payload
