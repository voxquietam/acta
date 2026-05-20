"""Template context processors for the ``web`` app.

Registered in ``acta/settings/base.py`` under
``TEMPLATES[0]["OPTIONS"]["context_processors"]``. Provides nav data
that nearly every page template needs (current user's workspaces and
the projects inside them) without forcing each view to recompute it.
"""

from apps.notifications.models import Notification
from apps.web.nav import get_nav_workspaces


def workspace_nav(request):
    """Inject the request user's workspaces (with favourite projects).

    Workspaces carry a ``favourite_projects`` attribute populated by
    :func:`apps.web.nav.get_nav_workspaces` — only starred, non-archived
    projects in each workspace, ordered by name. Empty dict for
    anonymous requests so login / error templates do not crash.

    Args:
        request: The current :class:`HttpRequest`.

    Returns:
        A context dict with ``nav_workspaces`` populated for
        authenticated users, empty otherwise.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    workspaces = get_nav_workspaces(request.user)
    # Pre-compute the "any favourite anywhere" flag so the sidebar
    # template doesn't fire a separate ``user.favourite_projects.all()``
    # query just to decide whether to render the empty-state CTA.
    return {
        "nav_workspaces": workspaces,
        "nav_has_favourites": any(ws.favourite_projects for ws in workspaces),
        "inbox_unread": Notification.objects.filter(
            recipient=request.user,
            archived_at__isnull=True,
            is_read=False,
        ).count(),
    }
