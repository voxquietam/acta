"""Template context processors for the ``web`` app.

Registered in ``acta/settings/base.py`` under
``TEMPLATES[0]["OPTIONS"]["context_processors"]``. Provides nav data
that nearly every page template needs (current user's workspaces and
the projects inside them) without forcing each view to recompute it.
"""

from apps.notifications.models import Notification
from apps.web.nav import get_nav_workspaces, resolve_active_workspace


def workspace_nav(request):
    """Inject the request user's workspaces + the active one.

    ``nav_workspaces`` is the full list (each carrying a
    ``favourite_projects`` attribute, see
    :func:`apps.web.nav.get_nav_workspaces`) — the switcher dropdown
    renders it. ``active_workspace`` is the one the user is scoped into;
    the favourites section and the unread badge are scoped to it. Empty
    dict for anonymous requests so login / error templates don't crash.

    Args:
        request: The current :class:`HttpRequest`.

    Returns:
        A context dict with ``nav_workspaces`` / ``active_workspace`` /
        ``nav_has_favourites`` / ``inbox_unread`` for authenticated
        users, empty otherwise.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    workspaces = get_nav_workspaces(request.user)
    # Reuse the already-fetched member list so we don't pay a second
    # membership query just to resolve the active workspace.
    active = resolve_active_workspace(request, members=workspaces)
    # Prefer the nav copy of the active workspace — it carries the
    # prefetched ``favourite_projects`` the sidebar renders.
    active_nav = next((w for w in workspaces if active and w.pk == active.pk), None) or active
    # Unread badge is scoped to the active workspace and excludes project
    # updates (they live in the Updates tab, never the Notifications list).
    unread = 0
    if active is not None:
        unread = (
            Notification.objects.filter(
                recipient=request.user,
                archived_at__isnull=True,
                is_read=False,
                workspace=active,
            )
            .exclude(kind=Notification.Kind.PROJECT_UPDATE)
            .count()
        )
    return {
        "nav_workspaces": workspaces,
        "active_workspace": active_nav,
        "nav_has_favourites": bool(active_nav and getattr(active_nav, "favourite_projects", None)),
        "nav_cycles_enabled": bool(active and active.cycle_config()["enabled"]),
        "inbox_unread": unread,
    }
