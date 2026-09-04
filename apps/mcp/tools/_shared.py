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


def resolve_user_by_username(username: str):
    """Look up a User by username; raise ``ValueError`` if not found."""
    try:
        return User.objects.get(username=username)
    except User.DoesNotExist:
        raise ValueError(f"User {username!r} does not exist.")


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
        "status": task.status,
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
