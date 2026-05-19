from django.apps import AppConfig


class McpConfig(AppConfig):
    """MCP (Model Context Protocol) server for Acta.

    Exposes Acta's data and operations to AI clients (Claude Desktop,
    Cursor, etc.) over the MCP standard. Authenticated via the
    per-user ``ApiToken`` model in ``apps.accounts``; tool calls run
    as the token's owner and respect all the same DRF permission
    classes the web UI uses.

    See ``project_todo_mcp_server`` in memory for the full design.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.mcp"
    verbose_name = "MCP server"
