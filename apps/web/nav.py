"""Sidebar navigation helpers.

Resolves the workspaces and projects rendered in the left rail across
every authenticated page. The sidebar lists **only the user's
favourited projects** (per ``User.favourite_projects`` M2M), grouped
under the workspace they belong to. When the user has no favourites,
templates show an empty-state CTA instead of every project — the rail
stays clean for users who pin a handful of active threads.

Starred tasks (``User.favourite_tasks``) ride alongside: each task whose
project is itself starred renders as a nested row under that project;
tasks whose project isn't starred surface in a separate "Issues"
section beneath the project list.

Used by:
* ``workspace_nav`` context processor (every authenticated page).
* ``toggle_project_favourite`` / ``toggle_task_favourite`` views for the
  OOB sidebar refresh.
"""

from django.db.models import Prefetch
from django.http import Http404

from apps.projects.models import Project
from apps.tasks.models import Task
from apps.web.url_scoping import request_workspace_slug
from apps.workspaces.models import Workspace

_ACTIVE_WS_CACHE = "_acta_active_workspace"


def resolve_active_workspace(request, members=None):
    """Return (and memoise on the request) the user's active workspace.

    Acta scopes All Tasks / Projects / My Work / Inbox / My Activity to a
    single *active* workspace chosen via the sidebar switcher. Resolution
    order:

    1. The workspace segment of a canonical URL (ADR 0031), when present.
       The URL is the most explicit statement of intent there is — a link
       to ``/ksu24/my-work/`` means that workspace, whatever the sidebar
       last remembered — so it also becomes the stored choice.
    2. The stored ``User.active_workspace``, while they are still a member.
    3. Their first workspace by name, persisted as a lazy init so freshly
       created / just-joined users land somewhere sensible.

    Returns ``None`` when the user belongs to no workspace.

    Raises:
        Http404: If the URL names a workspace that doesn't exist or that
            the user cannot reach. Deliberately not 403 — that would
            confirm the workspace exists to anyone guessing slugs.

    The result is cached on the request so the resolution (and any
    fallback write) runs at most once per request.

    Args:
        request: The active ``HttpRequest`` (``request.user`` must be set).
        members: Optional pre-fetched list of the user's workspaces,
            ordered by name (e.g. from :func:`get_nav_workspaces`). When
            given, the resolver reuses it instead of querying — the
            ``workspace_nav`` context processor passes it so every page
            doesn't pay a second membership query.

    Returns:
        The active :class:`Workspace`, or ``None``.
    """
    cached = getattr(request, _ACTIVE_WS_CACHE, "unset")
    if cached != "unset":
        return cached
    user = request.user
    if members is None:
        members = list(
            Workspace.objects.filter(memberships__user=user).order_by("name").distinct(),
        )
    active = None
    url_slug = request_workspace_slug(request)
    if url_slug:
        active = next((w for w in members if w.slug == url_slug), None)
        if active is None:
            raise Http404("No workspace matches the given query.")
        if active.pk != user.active_workspace_id:
            user.active_workspace = active
            user.save(update_fields=["active_workspace"])
        setattr(request, _ACTIVE_WS_CACHE, active)
        return active
    if user.active_workspace_id is not None:
        active = next((w for w in members if w.pk == user.active_workspace_id), None)
    if active is None:
        active = members[0] if members else None
        if active is not None and active.pk != user.active_workspace_id:
            user.active_workspace = active
            user.save(update_fields=["active_workspace"])
    setattr(request, _ACTIVE_WS_CACHE, active)
    return active


def set_active_workspace(request, workspace):
    """Persist ``workspace`` as the user's active one and refresh the cache.

    Used when viewing a project pulls its workspace into focus, so the
    sidebar / scoped views follow what the user is looking at. Persists
    only when it actually changed; always updates the per-request cache so
    the context processor (which runs after the view) reflects it.

    Args:
        request: The active ``HttpRequest``.
        workspace: The :class:`Workspace` to make active (caller must have
            already verified membership).
    """
    user = request.user
    if user.active_workspace_id != workspace.pk:
        user.active_workspace = workspace
        user.save(update_fields=["active_workspace"])
    setattr(request, _ACTIVE_WS_CACHE, workspace)


def get_nav_workspaces(user):
    """Workspaces the user is a member of, with favourited projects.

    Each returned ``Workspace`` instance carries a ``favourite_projects``
    attribute (populated via ``Prefetch.to_attr``) containing the
    user's starred projects in that workspace, ordered by name.
    Workspaces with no favourites still appear so the empty-state
    rendering can hang per-workspace if needed; the template chooses
    whether to hide them.

    Args:
        user: Authenticated :class:`User`.

    Returns:
        ``list[Workspace]`` ordered by workspace name.
    """
    starred_qs = Project.objects.filter(
        favourited_by=user,
        archived=False,
    ).order_by("name")
    return list(
        Workspace.objects.filter(memberships__user=user)
        .prefetch_related(
            Prefetch(
                "projects",
                queryset=starred_qs,
                to_attr="favourite_projects",
            ),
        )
        .order_by("name")
        .distinct(),
    )


def get_workspace_favourite_tasks(user, workspace):
    """Return the user's starred tasks within ``workspace``.

    Returns:
        ``list[Task]`` ordered by project name then number, with
        ``project`` joined so the template can render project context
        without a per-row lookup. Archived tasks are excluded from the
        rail (they live in the Archive tab, not the favourites list).
        Returns an empty list when ``workspace`` is ``None``.
    """
    if workspace is None:
        return []
    return list(
        Task.objects.filter(
            favourited_by_users=user,
            project__workspace=workspace,
            archived_at__isnull=True,
        )
        .select_related("project")
        .order_by("project__name", "number"),
    )
