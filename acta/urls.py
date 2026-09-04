"""Root URL configuration for Acta."""

from django.conf import settings
from django.contrib import admin
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path, re_path

import django_eventstream

from apps.accounts.views import InviteAwareSignupView
from apps.web.url_scoping import workspace_scoped
from apps.web.urls import urlpatterns as web_urlpatterns

api_v1_patterns = [
    path("", include("apps.workspaces.urls")),
    path("", include("apps.projects.urls")),
    path("", include("apps.labels.urls")),
    path("", include("apps.tasks.urls")),
    path("", include("apps.comments.urls")),
    path("", include("apps.activity.urls")),
]

urlpatterns = [
    path("admin/", admin.site.urls),
    # Custom account routes (language switcher) come before allauth so
    # /accounts/set-language/ is owned by us. The signup view override
    # also lands here so the invite email pre-fills the form on GET
    # before allauth's plain SignupView would.
    path("accounts/", include("apps.accounts.urls", namespace="accounts")),
    path("accounts/signup/", InviteAwareSignupView.as_view(), name="account_signup"),
    path("accounts/", include("allauth.urls")),
    path("api/v1/", include((api_v1_patterns, "api_v1"))),
    # MCP HTTP transport — single endpoint, JSON-RPC over POST. See
    # apps/mcp/views.py for protocol notes; docs/mcp.md for client setup.
    path("mcp/", include("apps.mcp.urls", namespace="mcp")),
    path("telegram/", include("apps.telegram.urls", namespace="telegram")),
    # Real-time SSE — one stream per workspace. See ADR 0015. The
    # channel name is templated from the URL kwarg so a client
    # connecting to ``/events/workspace/3`` subscribes to the
    # ``workspace-3`` channel; broadcasting from the server uses
    # ``django_eventstream.send_event('workspace-3', ...)``.
    re_path(
        r"^events/workspace/(?P<workspace_id>\d+)",
        include(django_eventstream.urls),
        {"format-channels": ["workspace-{workspace_id}"]},
    ),
    # Per-user notification stream — the ``user-<id>`` channel only the
    # matching user may read (see ``WorkspaceChannelManager``). Carries
    # ``notification.created`` events for the live inbox badge + row.
    re_path(
        r"^events/user/(?P<user_id>\d+)",
        include(django_eventstream.urls),
        {"format-channels": ["user-{user_id}"]},
    ),
    path("", include("apps.web.urls", namespace="web")),
    # Canonical, workspace-scoped routes (ADR 0031):
    # ``/<workspace_slug>/projects/SER/2/``. Generated from the legacy
    # patterns above rather than written twice — see ``workspace_scoped``.
    #
    # Registered LAST on purpose. The workspace segment matches any single
    # path component, so mounting it earlier would swallow ``/projects/``
    # as the workspace named "projects". Reserved root slugs guard the
    # same collision from the other direction.
    path(
        "<slug:workspace>/",
        include((workspace_scoped(web_urlpatterns), "web_ws"), namespace="web_ws"),
    ),
]

if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()
