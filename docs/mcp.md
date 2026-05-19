# Acta MCP server

Acta ships a [Model Context Protocol](https://modelcontextprotocol.io/)
server so AI clients (Claude Desktop, Cursor, Cline, any MCP-aware
tool) can read **and** write everything the authenticated Acta user
can do in the web UI — list / create / update / archive / delete
tasks, post comments, manage labels, query the activity log for
analytics.

There are two transports — pick the one that matches your situation.

| Transport | When to use | Setup |
|-----------|-------------|-------|
| **HTTP** (recommended, multi-user) | Anyone with a deployed Acta and an API token. Just point the client at the URL — no SSH, no Docker on the client side. | [Quick setup — HTTP](#quick-setup--http-claude-desktop) |
| **stdio** (local-admin only) | Hacking on Acta from the same laptop the dev server runs on. Client launches `manage.py mcp_serve` as a subprocess. | [Quick setup — stdio](#quick-setup--stdio-local-dev) |

Every tool call **re-authenticates** the token, so revoking a token in
`/accounts/settings/` takes effect on the very next call — no caching,
no session state to flush.


## Quick setup — HTTP (Claude Desktop)

1. **Generate an API token.**
   Open `/accounts/settings/` in the web UI, scroll to **API tokens**,
   give it a name (e.g. `Claude Desktop`), click **Generate**. Copy the
   secret *now* — it's shown exactly once.

2. **Edit Claude Desktop's MCP config.**
   On macOS it lives at
   `~/Library/Application Support/Claude/claude_desktop_config.json`.
   Add an entry under `mcpServers`:

   ```json
   {
     "mcpServers": {
       "acta": {
         "transport": "http",
         "url": "https://actaspace.com/mcp/",
         "headers": {
           "Authorization": "Token <paste-from-settings>"
         }
       }
     }
   }
   ```

   Replace `https://actaspace.com` with whatever public hostname your
   Acta instance is reachable at. The trailing slash on `/mcp/` is
   important — Django will 301 without it.

3. **Restart Claude Desktop.**
   Cmd-Q (not just close the window) — the config is only re-read on
   process start. The first time Claude calls a tool from a new
   server it asks you to **Always allow / Deny**.

4. **Try it.**
   In any chat: *"what acta workspaces do I have?"*, *"summarise what
   changed in AUDIT last week"*, *"create a task in ACTA called 'wire
   up sentry' assigned to me"*.


## Quick setup — stdio (local dev)

This path runs the MCP server as a subprocess of your AI client, talking
JSON-RPC over stdin/stdout. There's no network exposure — it's the safest
way to test Acta MCP work-in-progress against your local dev DB.

1. **Generate an API token** (same as HTTP, but against your local
   `http://localhost:8001/accounts/settings/`).

2. **Add to Claude Desktop config:**

   ```json
   {
     "mcpServers": {
       "acta-local": {
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

   For a non-Docker install swap the `command` / `args` for a direct
   `/path/to/venv/bin/python /path/to/manage.py mcp_serve` and keep the
   `env` block.

3. **Restart Claude Desktop** (Cmd-Q).


## Cursor / Cline / other MCP clients

The wire protocol is identical — both HTTP and stdio are MCP-standard.
Look up your client's MCP config location and use the same shape:

- HTTP: a URL plus an `Authorization: Token <secret>` header.
- stdio: a command + args + env that boot `manage.py mcp_serve`.

Cursor stores its MCP config in **Settings → Tools & MCP**.


## Tools

| Tool                                       | Purpose                                                    |
|--------------------------------------------|------------------------------------------------------------|
| **read**                                                                                                |
| `acta_ping`                                | Connection / auth check. Returns version + username.       |
| `acta_workspaces_list`                     | List workspaces the user can see.                          |
| `acta_projects_list`                       | List projects (filter: workspace, archived).               |
| `acta_tasks_list`                          | List tasks with filters (status, priority, assignee, etc.) |
| `acta_task_get`                            | Full payload for one task — meta + subtasks + comments + activity. |
| `acta_activity_list`                       | Flat activity events with filters (workspace, project, task, type, actor, time range). |
| `acta_comments_list`                       | Flat comments with filters (workspace, project, task, author, search). |
| `acta_labels_list`                         | List labels.                                               |
| **write**                                                                                               |
| `acta_task_create`                         | Create one task. Validation matches the web UI.            |
| `acta_task_update`                         | PATCH one task (partial; pass `null` to clear).            |
| `acta_task_archive`                        | Soft-delete (set `archived_at`).                           |
| `acta_task_delete`                         | Hard-delete (irreversible — prefer archive).               |
| `acta_comment_create`                      | Post a Markdown comment.                                   |
| `acta_label_create` / `_update` / `_delete`| Label CRUD.                                                |
| `acta_tasks_bulk_create`                   | Create N tasks atomically (any failure rolls back).        |
| `acta_tasks_bulk_update`                   | Update N tasks atomically.                                 |
| `acta_tasks_bulk_archive`                  | Archive N tasks atomically.                                |
| `acta_tasks_bulk_delete`                   | Delete N tasks atomically.                                 |

Every write tool emits the same activity-log events the web UI emits,
with `actor = the authenticated user`. The web's SSE stream picks
MCP-driven changes up automatically — open the page in a browser and
ask Claude to add a comment; the comment appears live.


## Security

- **Token = full account access.** The MCP token authenticates as the
  user who minted it. Anything that user can do in the web UI, the MCP
  client can do. Treat the token like a password.
- **Storage.** Only the SHA-256 hash + an 8-char prefix are stored
  server-side. The plain secret is shown once on creation. Lost a
  token? Revoke it in `/accounts/settings/` and mint a new one.
- **Rate limit.** Each token is capped at 60 requests per minute
  (cache-backed counter). Adjust via the
  `ACTA_MCP_RATE_LIMIT_PER_MINUTE` env var on the server. The cap is a
  sanity bound — it blocks runaway loops, not careful AI workflows.
- **Revocation.** Click **Revoke** on the token row in
  `/accounts/settings/`. Effect is immediate — the next MCP call fails
  with `Token has been revoked` and Claude/Cursor surfaces that to the
  user.
- **HTTPS only.** The HTTP transport is meant to live behind TLS — the
  Acta prod stack is fronted by Traefik with Let's Encrypt. Never
  expose `/mcp/` over plain HTTP across an untrusted network; the
  token travels in the Authorization header on every request.


## Troubleshooting

### HTTP transport

- **`401 Missing or malformed Authorization header`** — the client
  didn't send `Authorization: Token <secret>`. Check the `headers`
  block in your MCP config.
- **`401 Invalid token`** — the secret in your headers doesn't match
  any stored hash. Most common cause: pasted with surrounding
  whitespace or quote marks. Re-generate and re-paste.
- **`401 Token has been revoked`** — exactly what it says. Mint a new
  one.
- **`429 Rate limit exceeded`** — over 60 calls in one minute. Wait,
  or bump `ACTA_MCP_RATE_LIMIT_PER_MINUTE` on the server.
- **MCP client reports an empty tool list** — check Claude Desktop's
  developer logs (Help → Show developer tools → Console). The most
  common cause is `transport: "http"` typos or a URL that 404s
  (missing trailing slash, wrong host).
- **`404 Not Found` on `/mcp`** — Django's `APPEND_SLASH` redirects
  `/mcp` → `/mcp/`. Some clients don't follow redirects on POST. Use
  the trailing-slash form everywhere.

### stdio transport

- **`"ACTA_API_TOKEN env var is missing"`** — the `env` block in the
  MCP client config didn't get the variable. Re-check JSON formatting
  (no comments, no trailing commas), restart the client.
- **`docker compose exec` hangs** — the container isn't running.
  Bring it up with `docker compose up -d` (or whatever your dev
  workflow uses), then restart the MCP client.
- **Tool not appearing in the picker** — make sure Claude was fully
  quit (Cmd-Q) before the config edit. Tools are read on first server
  handshake; the handshake only fires on process start.

### Smoke test by hand

HTTP — handshake with `curl`:

```bash
TOKEN="<your token>"
curl -s -X POST https://actaspace.com/mcp/ \
  -H "Authorization: Token $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

A working server returns `{"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "acta", ...}}}`.

stdio — pipe a JSON-RPC request into `mcp_serve`:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"cli","version":"1"}}}' \
  | docker compose exec -T -e ACTA_API_TOKEN=$TOKEN web \
  python manage.py mcp_serve
```

You should get back a JSON response with `"serverInfo": {"name": "acta", ...}`.
If you don't, the server isn't booting cleanly — check the web
container logs.
