# ADR 0015: Real-Time Updates via SSE

**Status:** accepted
**Date:** 2026-05-15

## Context

The MVP commits to live kanban (cards update on every connected client without page reload) and live notification delivery (see [0014](0014-frontend-architecture.md), [0017](0017-notifications.md)). The transport choice — SSE vs WebSocket vs long-polling — and the server-side architecture both need pinning down.

Acta's real-time needs are one-way: server pushes to client, client never needs to push outside of a normal HTTP request. SSE matches this exactly and is far simpler to deploy than WebSocket.

## Decisions

### Transport — Server-Sent Events (SSE)

- One-way server → client streaming over plain HTTP.
- Built-in auto-reconnect in the browser's `EventSource`. HTMX's `hx-ext="sse"` adds attribute-level subscription on top.
- No WebSocket in MVP. Adds upgrade-handshake complexity and bi-directional plumbing we don't need.

### Deployment — **ASGI only, never WSGI**

This is the most important load-bearing constraint of this ADR. SSE on sync WSGI Gunicorn is a known failure mode: each open SSE connection ties up an entire worker process for the duration of the user's session. With 10 users that is 10 workers permanently busy doing nothing, leaving none free for normal HTTP requests.

- Production runs **Uvicorn** (ASGI), one worker, behind Caddy or nginx as reverse proxy.
- The reverse proxy must allow long-lived HTTP connections: `proxy_read_timeout` (nginx) or `flush_interval` (Caddy) tuned to keep SSE alive.
- A sync Django view inside ASGI is auto-wrapped with `sync_to_async`; no application code changes required.
- WSGI Gunicorn is explicitly forbidden for production. Local development can use `manage.py runserver` (works on ASGI when configured).

### Library — `django-eventstream`

- Pure SSE implementation for Django built on top of ASGI.
- Smaller surface than Django Channels (no consumer protocol, no routing layer to learn).
- One ASGI worker handles many concurrent idle SSE connections trivially (~5–50 KB RAM per connection). For 10 users, the entire SSE workload uses < 1 MB.
- Channels (the library) stays available if WebSocket becomes needed later; not required for MVP.

### Stream topology

- One SSE stream per workspace: `/sse/workspace/{workspace_id}/`.
- Clients subscribe on page load if the user is a member of that workspace; HTMX manages the connection lifecycle.
- Events on the stream are JSON envelopes:
  ```json
  {
    "event": "task.status_changed",
    "data": { "task_id": 101, "from": "to-do", "to": "in-progress", "actor_id": 5, "bulk_id": null, "occurred_at": "..." }
  }
  ```
- Events broadcast to a workspace stream are filtered server-side to **exclude the actor** — if Vox moved the card, Vox's own client doesn't get a duplicate event back (it already updated optimistically via the original `PATCH` response). All other connected members get it.

### Event types pushed

The SSE event type matches the corresponding `ActivityLog.event_type` 1:1 (see [0011](0011-activity-log.md)). Same naming, same payload shape. No translation layer.

Subset actually emitted to clients in MVP:

- `task.created`
- `task.status_changed`
- `task.assigned`
- `task.priority_changed`
- `task.due_changed`
- `task.labels_changed`
- `task.updated`
- `task.deleted`
- `comment.created`
- `project_update.created` (manual posts surface as notifications even though they're not in the activity log)

Member admin events (`member.added` / `removed` / `role_changed`) are pushed to admins on the members page only — out of MVP. Can be added later by reusing the same stream and filtering client-side.

### Client-side handling

- **Kanban page:** HTMX `sse-swap` attribute on each card. When `task.status_changed` arrives, the server-pushed payload includes pre-rendered HTML for the card in its new column, and HTMX swaps it.
- **Activity feed page:** HTMX `sse-swap` on the feed container. New events prepend.
- **Task detail page:** HTMX `sse-swap` for the timeline. New events append.
- **Notifications:** vanilla JS listener on the SSE source dispatches a custom `acta:notify` event for events relevant to the current user; an Alpine component shows the toast (see [0017](0017-notifications.md)).

### Server-side broadcast

- `log_event()` (the activity-log helper from [0011](0011-activity-log.md)) is the single broadcast point. After writing the `ActivityLog` row, it calls `eventstream.send_event(...)` to push to the workspace channel.
- The broadcast happens **after** the DB transaction commits — never before. If the transaction rolls back, no event is emitted. Implementation uses `transaction.on_commit(...)`.
- Pre-rendered card HTML for kanban swaps is built in the broadcast path: the server uses the same Django partial template (`task_card.html`) that the kanban page initially renders, so the swap is structurally identical to the first render.

### Authentication and authorization

- The SSE endpoint requires session authentication.
- A user can only subscribe to streams of workspaces they are members of. The endpoint validates membership on connect and on every event filter pass.
- A revoked membership (admin removed the user) terminates the stream connection on the next event filter pass; the client reconnects but is rejected.

### Reconnection and gaps

- `EventSource` auto-reconnects on transient disconnect.
- Each event includes an `occurred_at` timestamp and the underlying `ActivityLog.id`. On reconnect, HTMX sends the last seen event id; the server can replay missed events from the activity log table where `id > last_seen_id`.
- Replay is limited to events from the last 24 hours to bound query cost. Longer gaps mean the user reloads the page (full reconciliation).

### Scaling envelope

- MVP target: 10 concurrent users per workspace, single workspace. Tens of events per minute peak.
- One Uvicorn ASGI worker handles this trivially. In-memory pub/sub backend of `django-eventstream` is fine.
- **When to add Redis backend:**
  - Multiple Uvicorn workers needed (each worker has its own in-memory pub/sub; events emitted in worker A don't reach SSE clients connected to worker B without a shared backend).
  - Or: events need to survive a worker restart.
  - Migration is a config-only change (`EVENTSTREAM_STORAGE_CLASS = 'eventstream.storage.RedisStorage'`) plus adding a Redis service to docker-compose. Not in MVP.
- **When to consider a separate real-time service** (e.g. Mercure, Centrifugo): only if SSE traffic outgrows a single Python process meaningfully (50+ concurrent users sustained). Not in MVP.

## Why

- **SSE** is the simplest possible real-time transport — plain HTTP, auto-reconnect, browser-native. WebSocket is overkill for one-way push.
- **`django-eventstream`** keeps the dependency footprint small and the mental model close to "Django views that happen to stream." Channels is a great library but is a much bigger surface to learn.
- **Reusing `ActivityLog` event types** means there's no separate "real-time event catalog" to maintain. Activity is the source of truth; SSE is just a delivery channel.
- **Broadcasting after commit** avoids the entire class of "we sent the event, then the DB rolled back" bugs.
- **Excluding the actor from their own broadcast** prevents the double-update flash (response replaces card optimistically, then SSE event tries to replace it again).
- **Stream per workspace** is the natural authorization boundary; subscribing to a user channel would force every member to subscribe to N channels (one per task they care about), which is more state.

## Consequences

- Production deployment **must use ASGI (Uvicorn)**, not WSGI Gunicorn — see the dedicated section above. This is a documented departure from the `ksu24.back` deployment pattern.
- SSE connections are long-lived — connection count per worker matters. With `django-eventstream`'s default in-memory backend this is fine for MVP, but the reverse proxy must allow long-lived idle HTTP connections (nginx/Caddy `proxy_read_timeout` increased, gzip/buffering disabled on the SSE route).
- Local development must use `python manage.py runserver` (ASGI-compatible under Django 5) or `uvicorn acta.asgi:application --reload`. Running `gunicorn acta.wsgi:application` locally will silently break SSE — guarded against by not shipping a Gunicorn config.
- Pre-rendering card HTML server-side adds a small per-event cost. Acceptable; revisit only if profiling shows it.
- Replay logic for reconnects has a 24-hour bound; a user who closes their laptop for a week will see a fresh page on next load (no incremental update). Reasonable trade-off.
- If we ever add browser desktop notifications, they can ride on the same SSE event types — no protocol change.

## Open Questions

- Whether to also include `project.updated` and `project_update.created` in the broadcast set. Lean yes for `project_update.created` (it's a notification trigger). To be decided in [0017](0017-notifications.md).
- Whether to gzip/compress the SSE stream behind the reverse proxy. Defer; check after MVP profiling.
- Whether replay window should be configurable per workspace. Default 24h; tunable in settings without ADR change.
