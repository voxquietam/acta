"""Helpers shared between the read and write tool modules.

Keep this tiny — it's just the bits both directions need (user scope,
slug lookup, payload shaper). Anything else lives in its own module.
"""

from __future__ import annotations

from typing import Any

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


def resolve_user_by_username(username: str):
    """Look up a User by username; raise ``ValueError`` if not found."""
    try:
        return User.objects.get(username=username)
    except User.DoesNotExist:
        raise ValueError(f"User {username!r} does not exist.")


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


def serialize_task_summary(task: Task) -> dict[str, Any]:
    """Compact task-summary payload — matches ``acta_tasks_list`` rows.

    Write tools return this shape so LLM-driven workflows can chain
    create / update calls without restructuring the data each step.
    """
    return {
        "slug": task.slug,
        "title": task.title,
        "status": task.status,
        "priority": task.priority,
        "size": task.size,
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
