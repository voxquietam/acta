"""Mirror the web URL patterns under a workspace segment (ADR 0031).

Every workspace-specific section is reachable two ways:

- canonical — ``/<workspace_slug>/projects/SER/2/``
- legacy — ``/projects/SER/2/``, kept forever because six months of links
  are already out in Telegram messages, bookmarks and chat

Rather than writing 125 patterns twice, the canonical set is generated from
the legacy one at import time. Each view is wrapped so the extra
``workspace`` URL kwarg is absorbed before it reaches the view — views keep
their existing signatures — and stashed on the request, where the resolver
helpers pick it up to disambiguate a slug that exists in two workspaces.

The mirrored patterns must be registered **after** the legacy ones: the
workspace segment matches any single path component, so ``/projects/``
would otherwise resolve as the workspace named "projects" hitting the
dashboard. Reserved root slugs (see ``RESERVED_WORKSPACE_SLUGS``) close the
same trap from the other side.
"""

from __future__ import annotations

from functools import wraps

from django.urls import URLPattern, URLResolver

#: Attribute the absorbed workspace slug is stashed under.
REQUEST_WORKSPACE_ATTR = "url_workspace_slug"


def _absorb_workspace(view):
    """Wrap ``view`` so it tolerates the ``workspace`` URL kwarg.

    The canonical route carries a workspace segment that the views know
    nothing about. Instead of threading a new argument through 125 view
    signatures, the wrapper pops it off and records it on the request.

    Args:
        view: The view callable from the legacy pattern.

    Returns:
        A callable with the same signature minus ``workspace``.
    """

    @wraps(view)
    def wrapper(request, *args, workspace=None, **kwargs):
        setattr(request, REQUEST_WORKSPACE_ATTR, workspace)
        return view(request, *args, **kwargs)

    return wrapper


def workspace_scoped(patterns):
    """Return a copy of ``patterns`` whose views absorb a workspace kwarg.

    Args:
        patterns: The legacy ``urlpatterns`` list.

    Returns:
        A parallel list, safe to mount under ``<slug:workspace>/``. Nested
        resolvers are recursed into so the mirror stays complete if the
        URLconf ever grows an ``include()``.
    """
    mirrored = []
    for entry in patterns:
        if isinstance(entry, URLResolver):
            mirrored.append(
                URLResolver(
                    entry.pattern,
                    workspace_scoped(entry.url_patterns),
                    entry.default_kwargs,
                    entry.app_name,
                    entry.namespace,
                )
            )
        elif isinstance(entry, URLPattern):
            mirrored.append(
                URLPattern(
                    entry.pattern,
                    _absorb_workspace(entry.callback),
                    entry.default_args,
                    entry.name,
                )
            )
    return mirrored


def request_workspace_slug(request):
    """Return the workspace slug from the URL, or ``None`` on a legacy path.

    Args:
        request: The active ``HttpRequest``.

    Returns:
        The slug the canonical route carried, else ``None``.
    """
    return getattr(request, REQUEST_WORKSPACE_ATTR, None)


def task_path(task):
    """Return the canonical, workspace-scoped path of a task.

    The Python-side counterpart of the ``task_url`` template tag, for the
    places that build a URL outside a template: HX-Location payloads after
    a create or a move, Telegram notification links, command-palette rows.

    Args:
        task: The :class:`Task` to link to. ``project__workspace`` should be
            loaded, or this costs a query.

    Returns:
        The path, e.g. ``/ksu24/projects/SER/2/``.
    """
    from django.urls import reverse

    return reverse(
        "web_ws:task_detail",
        kwargs={
            "workspace": task.project.workspace.slug,
            "slug_prefix": task.project.slug_prefix,
            "number": task.number,
        },
    )


def project_path(project):
    """Return the canonical, workspace-scoped path of a project.

    Args:
        project: The :class:`Project` to link to; ``workspace`` should be
            loaded.

    Returns:
        The path, e.g. ``/ksu24/projects/SER/``.
    """
    from django.urls import reverse

    return reverse(
        "web_ws:project_detail",
        kwargs={
            "workspace": project.workspace.slug,
            "slug_prefix": project.slug_prefix,
        },
    )
