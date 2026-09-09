"""URL routing for the MCP HTTP transport and its OAuth flow.

``/mcp/`` is the JSON-RPC endpoint (see :mod:`apps.mcp.views`). The
``oauth/*`` routes below are what let Claude Desktop connect from a bare
URL instead of through a Node bridge; the two ``.well-known`` documents
that point at them are mounted at the site root in ``acta/urls.py``,
because that is where discovery requires them to live.
"""

from django.urls import path

from apps.mcp.oauth import authorize, register, switch_account, token
from apps.mcp.views import mcp_http

app_name = "mcp"

urlpatterns = [
    path("", mcp_http, name="endpoint"),
    path("oauth/register/", register, name="oauth_register"),
    path("oauth/authorize/", authorize, name="oauth_authorize"),
    path("oauth/token/", token, name="oauth_token"),
    path("oauth/switch-account/", switch_account, name="oauth_switch_account"),
]
