"""Template context processors for the ``web`` app.

Registered in ``acta/settings/base.py`` under
``TEMPLATES[0]["OPTIONS"]["context_processors"]``. Provides nav data
that nearly every page template needs (current user's workspaces and
the projects inside them) without forcing each view to recompute it.
"""

from apps.workspaces.models import Workspace


def workspace_nav(request):
    """Inject the request user's workspaces (with projects) into every template.

    Computes a single queryset with ``prefetch_related("projects")`` so
    sidebar rendering stays O(1) in workspace and project count. Empty
    dict for anonymous requests so login/error templates do not crash.

    Args:
        request: The current :class:`HttpRequest`.

    Returns:
        A context dict with ``nav_workspaces`` populated for
        authenticated users, empty otherwise.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    workspaces = list(
        Workspace.objects.filter(memberships__user=request.user)
        .prefetch_related("projects")
        .order_by("name")
        .distinct(),
    )
    return {"nav_workspaces": workspaces}
