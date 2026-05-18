"""Sidebar navigation helpers.

Resolves the workspaces and projects rendered in the left rail across
every authenticated page. The sidebar lists **only the user's
favourited projects** (per ``User.favourite_projects`` M2M), grouped
under the workspace they belong to. When the user has no favourites,
templates show an empty-state CTA instead of every project — the rail
stays clean for users who pin a handful of active threads.

Used by:
* ``workspace_nav`` context processor (every authenticated page).
* ``toggle_project_favourite`` view for the OOB sidebar refresh.
"""

from django.db.models import Prefetch

from apps.projects.models import Project
from apps.workspaces.models import Workspace


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
