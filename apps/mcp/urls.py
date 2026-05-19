"""URL routing for the MCP HTTP transport.

Mounted at ``/mcp/`` in ``acta/urls.py``. The single endpoint accepts
POST with a JSON-RPC body; see :mod:`apps.mcp.views` for protocol
details.
"""

from django.urls import path

from apps.mcp.views import mcp_http

app_name = "mcp"

urlpatterns = [
    path("", mcp_http, name="endpoint"),
]
