# Acta MCP server

Acta ships a [Model Context Protocol](https://modelcontextprotocol.io/)
server so AI clients (Claude Desktop, Cursor, Cline, any MCP-aware
tool) can read **and** write everything the authenticated Acta user
can do in the web UI — list / create / update / archive / delete
tasks, post comments, manage labels, query the activity log for
analytics.

The MCP server runs as a stdio subprocess of the AI client. Every
tool call re-authenticates from the `ACTA_API_TOKEN` env var (set in
the client's MCP config), so token revocation in
`/accounts/settings/` takes effect immediately on the next call.


## Quick setup — Claude Desktop

1. **Generate an API token.**
   Open `/accounts/settings/` in the web UI, scroll to the **API
   tokens** section, give the token a name (e.g. `Claude Desktop`),
   click **Generate**. Copy the token *now* — it's shown only once.

2. **Edit Claude Desktop's MCP config.**
   On macOS it lives at
   `~/Library/Application Support/Claude/claude_desktop_config.json`.
   Add an entry under `mcpServers`:

   ```json
   {
     "mcpServers": {
       "acta": {
         "command": "docker",
         "args": [
           "compose",
           "-f", "/path/to/acta/docker-compose.dev.yml",
           "exec", "-T",
           "-e", "ACTA_API_TOKEN",
           "web",
           "python", "manage.py", "mcp_serve"
         ],
         "env": {
           "ACTA_API_TOKEN": "<paste-from-settings>"
         }
       }
     }
   }
   ```

   Adjust the `-f` path to your `docker-compose.dev.yml`. For a
   non-Docker install, swap the `command` / `args` for a direct
   `/path/to/venv/bin/python /path/to/manage.py mcp_serve` and keep
   the `env` block.

3. **Restart Claude Desktop.**
   Cmd-Q (not just close window) — the config is only re-read on
   process start. The first time Claude calls a tool from a new
   server it asks you to **Always allow / Deny**.

4. **Try it.**
   In any chat: *"what acta workspaces do I have?"*, *"summarise
   what changed in AUDIT last week"*, *"create a task in ACTA called
   'wire up sentry' assigned to me"*.


## Cursor / Cline / others

The wire protocol is the same. Point the client's MCP config at the
same command, set the same `ACTA_API_TOKEN` env var. Refer to your
client's own docs for the config file location (Cursor stores its
MCP config inside its Settings UI under "Tools & MCP").


## Tools

| Tool                          | Purpose                                                    |
|-------------------------------|------------------------------------------------------------|
| **read**                                                                                   |
| `acta_ping`                   | Connection / auth check. Returns version + username.       |
| `acta_workspaces_list`        | List workspaces the user can see.                          |
| `acta_projects_list`          | List projects (filter: workspace, archived).               |
| `acta_tasks_list`             | List tasks with filters (status, priority, assignee, etc.) |
| `acta_task_get`               | Full payload for one task — meta + subtasks + comments + activity. |
| `acta_activity_list`          | Flat activity events with filters (workspace, project, task, type, actor, time range). Use for analytics. |
| `acta_comments_list`          | Flat comments with filters (workspace, project, task, author, search). |
| `acta_labels_list`            | List labels.                                               |
| **write**                                                                                  |
| `acta_task_create`            | Create one task. Validation matches the web UI.            |
| `acta_task_update`            | PATCH one task (partial; pass `null` to clear).            |
| `acta_task_archive`           | Soft-delete (set `archived_at`).                           |
| `acta_task_delete`            | Hard-delete (irreversible — prefer archive).               |
| `acta_comment_create`         | Post a Markdown comment.                                   |
| `acta_label_create` / `_update` / `_delete` | Label CRUD.                                  |
| `acta_tasks_bulk_create`      | Create N tasks atomically (any failure rolls back).        |
| `acta_tasks_bulk_update`      | Update N tasks atomically.                                 |
| `acta_tasks_bulk_archive`     | Archive N tasks atomically.                                |
| `acta_tasks_bulk_delete`      | Delete N tasks atomically.                                 |

Every write tool emits the same activity-log events the web UI
emits, with `actor = the authenticated user`. The web's SSE stream
picks MCP-driven changes up automatically — open the page in a
browser and ask Claude to add a comment; the comment appears live.


## Security

- **Token = full account access.** The MCP token authenticates as
  the user who minted it. Anything that user can do in the web UI,
  the MCP client can do. Treat the token like a password.
- **Storage.** Only the SHA-256 hash + an 8-char prefix are stored
  server-side. The plain secret is shown once on creation. Lost a
  token? Revoke it in `/accounts/settings/` and mint a new one.
- **Rate limit.** Each token is capped at 60 requests per minute
  (cache-backed counter). Adjust via the
  `ACTA_MCP_RATE_LIMIT_PER_MINUTE` env var on the server. The cap
  is a sanity bound — it blocks runaway loops, not careful AI
  workflows.
- **Revocation.** Click **Revoke** on the token row in
  `/accounts/settings/`. Effect is immediate — the next MCP call
  fails with `Token has been revoked` and Claude/Cursor surfaces
  that to the user.


## Troubleshooting

- **"ACTA_API_TOKEN env var is missing"** — the `env` block in the
  MCP client config didn't get the variable. Re-check JSON
  formatting (no comments, no trailing commas), restart the client.
- **"Invalid token"** — the secret in the env doesn't match any
  stored hash. Most common cause: pasted with surrounding whitespace
  or quote marks. Re-generate and re-paste.
- **"Token has been revoked"** — exactly what it says. Mint a new
  one.
- **"Rate limit exceeded"** — you (or a runaway agent loop) sent more
  than 60 calls in the same minute. Wait a bit, or bump
  `ACTA_MCP_RATE_LIMIT_PER_MINUTE` on the server.
- **Tool not appearing in Claude Desktop's tool picker** — make sure
  Claude was fully quit (Cmd-Q) before the config edit. Tools are
  read on first server handshake; the handshake only fires on
  process start.
- **`docker compose exec` hangs** — the container isn't running.
  Bring it up with `docker compose up -d` (or whatever your deploy
  workflow uses), then restart the MCP client.

For deeper protocol issues, run the smoke test by hand:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize",
  "params":{"protocolVersion":"2024-11-05","capabilities":{},
  "clientInfo":{"name":"cli","version":"1"}}}' \
  | docker compose exec -T -e ACTA_API_TOKEN=$TOKEN web \
  python manage.py mcp_serve
```

You should get back a JSON response with `"serverInfo": {"name":
"acta", ...}`. If you don't, the server isn't booting cleanly and
the issue is on the Acta side; check the web container logs.
