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
| **HTTP** (recommended, multi-user) | Anyone with a deployed Acta. Point the client at the URL — no SSH, no Docker on the client side. | [Claude Code](#quick-setup--claude-code-http) · [Claude Desktop](#quick-setup--claude-desktop-custom-connector) |
| **stdio** (local-admin only) | Hacking on Acta from the same laptop the dev server runs on. Client launches `manage.py mcp_serve` as a subprocess. | [Quick setup — stdio](#quick-setup--stdio-local-dev) |

**The two Claude clients authenticate differently, and mixing them up is
the single most common setup failure.** Claude Code takes a URL and an
`Authorization: Token <secret>` header you paste yourself. Claude Desktop
takes only a URL, as a **Custom Connector**, and works the rest out over
OAuth — you approve a consent screen in the browser and never see a
token. Same server, same tools, two ways in.

There is also a third, older path for Desktop: the `mcp-remote` bridge,
which needs Node.js installed. It still works and is documented below,
but the Custom Connector supersedes it — reach for the bridge only if
your Desktop build predates connector support.

Every tool call **re-authenticates** the token, so revoking a token in
`/accounts/settings/` takes effect on the very next call — no caching,
no session state to flush.


## Quick setup — Claude Code (HTTP)

The short path: no Node.js, no bridge, no hand-edited JSON.

1. **Generate an API token.**
   Open `/accounts/settings/` in the web UI, scroll to **API tokens**,
   give it a name (e.g. `Claude Code`), click **Generate**. Copy the
   secret *now* — it's shown exactly once.

2. **Register the server.**

   ```bash
   claude mcp add --transport http --scope user acta https://actaspace.com/mcp/ --header "Authorization: Token <paste-from-settings>"
   ```

   `--scope user` makes it available in every project; the default
   (`local`) binds it to the current directory only. The header takes a
   normal space after the colon here — the CLI passes the argument
   through whole.

   Replace `https://actaspace.com` with whatever public hostname your
   Acta instance is reachable at. The trailing slash on `/mcp/` is
   important — Django will 301 without it, and some clients drop POST
   bodies across a redirect.

3. **Check it registered.**

   ```bash
   claude mcp list
   ```

   Inside a session, `/mcp` shows the live connection state.

4. **Try it.**
   In any chat: *"what acta workspaces do I have?"*, *"summarise what
   changed in AUDIT last week"*, *"create a task in ACTA called 'wire
   up sentry' assigned to me"*.

To edit it by hand instead, the entry lives in `~/.claude.json`
(`%USERPROFILE%\.claude.json` on Windows):

```json
"acta": {
  "type": "http",
  "url": "https://actaspace.com/mcp/",
  "headers": { "Authorization": "Token <paste-from-settings>" }
}
```

The key is **`type`**, not `transport` — the CLI *flag* is
`--transport`, the config *key* is `type`, and writing `transport` in
the file leaves the server silently unusable.


## Quick setup — Claude Desktop (Custom Connector)

The short path for Desktop: one URL, no Node.js, no config file, no
token to copy.

1. In Claude Desktop open **Settings → Connectors → Add custom
   connector**.
2. Paste the MCP endpoint URL — `https://actaspace.com/mcp/` — and
   confirm.
3. A browser window opens on Acta. Sign in if you are not already, then
   read the consent screen: it names the app asking and what it will be
   able to do. Press **Approve**.
4. Desktop closes the window by itself. The Acta tools are available in
   the next conversation.

**What just happened.** Desktop asked the endpoint who guards it, found
the OAuth metadata, registered itself, and sent you to approve the
grant. It then received an access token that lives an hour and renews
itself in the background. You never handle a secret, which is the point:
nothing to paste, nothing to leak, nothing to lose.

**To disconnect**, go to `/accounts/settings/` → **API tokens** and
revoke the entry marked *connected app*. Revocation is immediate — every
tool call re-authenticates — and it also stops the renewal, so the
connector cannot quietly mint itself a replacement.

The grant shows one line per connected app, not one per hour: the secret
rotates in place. Its short prefix therefore changes over time, which is
expected and not a sign that anything broke.

### Troubleshooting

**"Could not connect" straight after pasting the URL.** The URL must end
in `/mcp/` and be reachable over HTTPS from your machine. Open it in a
browser: you should get a JSON error saying the endpoint speaks POST
only — that means it is alive.

**The browser window opens but shows the login page every time.** Your
Acta session cookie is blocked or expired. Sign in to Acta normally in
the same browser first, then retry.

**"redirect_uri does not match this client's registration."** Desktop
re-registered against a different origin than the one you pasted — most
often `http://` versus `https://`, or a trailing-slash difference.
Remove the connector and add it again with the exact URL.


## Quick setup — Claude Desktop (legacy, HTTP via mcp-remote)

**Superseded by the Custom Connector above.** Use this only if your
Desktop build has no connector support.

Claude Desktop's config file only knows how to spawn a local process, so
an HTTP server has to be reached through the `mcp-remote` bridge.
**This requires Node.js on the machine** — that is the whole reason
Node enters the picture.

1. **Install Node.js (LTS)** from [nodejs.org](https://nodejs.org/), or
   `choco install nodejs-lts -y` in an elevated PowerShell. Verify in a
   *new* terminal:

   ```bash
   node -v && npx -v
   ```

2. **Generate an API token** — same as above.

3. **Edit the config.** Settings → Developer → Edit Config, or directly:

   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`

   ```json
   {
     "mcpServers": {
       "acta": {
         "command": "npx",
         "args": [
           "-y",
           "mcp-remote@latest",
           "https://actaspace.com/mcp/",
           "--transport",
           "http-only",
           "--header",
           "Authorization:${ACTA_TOKEN}"
         ],
         "env": {
           "ACTA_TOKEN": "Token <paste-from-settings>"
         }
       }
     }
   }
   ```

   Three details that each break the connection on their own:

   - `--transport http-only` keeps the bridge from probing for an SSE
     stream. Acta's endpoint answers **POST only**, so a `GET /mcp/`
     probe gets a 405 and some bridge versions stall on it.
   - The header argument has **no space** after the colon
     (`Authorization:${ACTA_TOKEN}`). Desktop splits `args` on spaces,
     so `"Authorization: ${ACTA_TOKEN}"` arrives torn in half. The space
     between `Token` and the secret comes from inside the env var.
   - If you already have other servers, add `"acta"` *inside* the
     existing `mcpServers` object — a second `mcpServers` block is
     invalid JSON and the whole file is ignored.

4. **Quit Claude Desktop completely** — Cmd-Q on macOS, right-click the
   tray icon → Quit on Windows. Closing the window is not enough: the
   config *and* the `PATH` are read only at process start, which is why
   a fresh Node install appears missing until a full restart.

5. **Try it** — same prompts as above.

**Windows: `spawn npx ENOENT`.** Desktop didn't inherit a `PATH`
containing Node. Find the real path with `where npx` and spell it out,
including the `.cmd` extension:

```json
"command": "C:\\Program Files\\nodejs\\npx.cmd"
```

**First run times out.** `npx -y mcp-remote@latest` downloads the
package on first use, which can outrun the 60-second handshake timeout.
Run it once by hand in a terminal to warm the cache, or install it
globally and call it directly:

```bash
npm i -g mcp-remote
```

Then `"command"` becomes the path from `where mcp-remote` (typically
`C:\Users\<you>\AppData\Roaming\npm\mcp-remote.cmd`) with the `-y` and
package-name arguments dropped.

**Bridge tries to open a browser for login.** That's `mcp-remote`
falling back to OAuth because it didn't get a usable header. Acta does
not implement OAuth — it only accepts `Authorization: Token <secret>`.
Re-check the header argument, and delete the bridge's cached auth state
at `~/.mcp-auth` (`C:\Users\<you>\.mcp-auth`) before retrying.


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
| `acta_task_get`                            | Full payload for one task — meta + subtasks + comments + activity + links. |
| `acta_activity_list`                       | Flat activity events with filters (workspace, project, task, type, actor, time range). |
| `acta_comments_list`                       | Flat comments with filters (workspace, project, task, author, search). |
| `acta_labels_list`                         | List labels.                                               |
| **write**                                                                                               |
| `acta_task_create`                         | Create one task. Validation matches the web UI.            |
| `acta_task_update`                         | PATCH one task (partial; pass `null` to clear).            |
| `acta_task_archive`                        | Soft-delete (set `archived_at`).                           |
| `acta_task_link`                           | Link two tasks (`blocks` / `blocked_by` / `related`).      |
| `acta_task_unlink`                         | Remove a link between two tasks.                           |
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
  user. Revoking a *connected app* also kills its renewal, so it cannot
  quietly mint itself a replacement.
- **Connected apps (OAuth).** A Custom Connector never receives a secret
  you could leak: it gets an access token that lives one hour and rotates
  in place, plus a refresh token that is retired the moment it is used
  (rotation — a stolen copy dies at the connector's next refresh). Client
  registration is open to anyone, which grants nothing on its own: a
  registration is a name and a redirect URI, and every token still
  requires a signed-in human to press **Approve**. Redirect URIs are
  matched exactly, never by prefix, and PKCE with `S256` is mandatory —
  a public client has no secret, so the verifier is the only proof that
  the party redeeming a code is the one that requested it.
- **The consent screen names the app, but the app names itself.** The
  string shown is whatever the client registered, so the page frames it
  as a claim. Approve a connection only if you started it.
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
- **MCP client reports an empty tool list** — in Claude Code run
  `claude mcp list`; in Claude Desktop check Help → Show developer
  tools → Console. The usual causes are the wrong config key
  (`transport` instead of `type` in `~/.claude.json`) or a URL that
  404s (missing trailing slash, wrong host).
- **`404 Not Found` on `/mcp`** — Django's `APPEND_SLASH` redirects
  `/mcp` → `/mcp/`. Some clients don't follow redirects on POST. Use
  the trailing-slash form everywhere.
- **`405` on a `GET`** — expected. The endpoint is POST-only; the
  response body says so in JSON. Browsers and SSE-probing bridges hit
  this. Pass `--transport http-only` to `mcp-remote`.

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
