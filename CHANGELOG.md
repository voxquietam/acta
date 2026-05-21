# Changelog

All notable changes to Acta are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are hand-written from the Conventional Commit history for now.
Automating this with `git-cliff` is deferred until `v1.0.0`.

## [0.2.0] — 2026-05-21

Second release. Adds collaboration (reactions, threaded comments,
mentions, notifications), an MCP server + API tokens for automation,
invite-based signup, a timeline/Gantt view, and a full design-system
rebrand on top of the v0.1.0 MVP.

### ⚠ Breaking / migrations

Deployers should run migrations and review the items below — each
changes a model, a public surface, or prod settings:

- New models / fields: `Reaction` (polymorphic), `Task.start_date`,
  task links, persistent notifications/inbox, comments on project
  updates, API tokens, workspace invites.
- API: comments became polymorphic (DRF comment API stays task-only);
  new reaction toggle, task link, and MCP endpoints.
- Signup is now invite-only (workspace tokens, expiring + one-use).
- Prod compose closes the Postgres host port and pins container names
  (multi-stage build with a node stage).
- Brand palette swapped from lavender to indigo-blue.

### Added

- **MCP server** — Model Context Protocol integration over stdio and
  HTTP (`/mcp/`): read tools (task/activity/comments/links), write
  tools (create / update / archive / comment / link), label CRUD with
  auto-create, bulk + delete, rate limiting, and N+1 guards.
- **API tokens** — token auth for non-browser clients with a settings
  management UI (create / revoke / delete) and an MCP setup snippet.
- **Invite-based signup** — expiring, one-use workspace invite tokens,
  invite UI on settings, SMTP email delivery, and a custom signup page
  that prefills + locks the invited email.
- **Emoji reactions** — reaction bars on tasks, comments, and project
  updates (generic `Reaction` model + toggle endpoint, vendored
  emoji-picker).
- **Comments** — edit / delete / in-place edit, relative timestamps,
  one-level replies on task comments, plus comments and replies on
  project updates.
- **Project updates** — compose from the overview with health chips,
  edit, and delete.
- **Notifications & Inbox** — persistent `/inbox/` with server-side
  fan-out, live SSE arrival on a per-user channel, mention fan-out, and
  notifications on `project_update.created`.
- **Mentions** — `@`-picker in the TipTap editor with chips, hover
  cards, and task-modal open; backend mention search + fan-out.
- **Task links** — link / unlink with autocomplete and live
  blocked / blocking badges.
- **Timeline (Gantt) view** — third project tab with `start_date`
  scheduling, drag-to-reschedule, day / week / month zoom, open-ended
  bars for partially-dated tasks, client-side filters, lazy-loaded.
- **My Activity** — my-comments + activity tabs grouped by task, with
  diffs, links, previews, and load-more.
- **Activity** — filters + search, load-more counter, comment clamp.
- **Workspaces** — create-workspace / create-project / settings flows.
- **i18n** — Ukrainian translations for the create flows and the
  timeline view.

### Changed

- **Rebrand** — indigo-blue brand palette, a `midnight` theme variant,
  a custom logo mark + favicons (replacing the text "A"), and
  Inter + JetBrains Mono webfonts.
- **Design system** — redesigned task cards / rows, kanban headers +
  substatus row, table (merged slug + priority, sticky header, label
  popover), project cards (progress bar, breakdown chips), bulk bar
  (glass surface, brand glow), create-task modal, segmented view tabs,
  task-detail topbar, modal scrim, priority-picker shortcuts, and
  extracted button / input primitives.
- **Navigation** — sidebar reordered (inbox first, dashboard in the
  footer) with an active stripe and version kicker; boosted-nav fixes
  remove full-page reloads.
- **Performance** — project overview collapsed to a single aggregate
  query (21 → ≤15), and the timeline panel is lazy-loaded.
- **Deploy** — `make deploy BRANCH=…`, `make ci-check`, deployment
  docs, and a multi-stage Docker build.

### Fixed

- HTMX history navigation restores the full page — Back no longer
  drops the shell (timeline no longer renders full-screen).
- Reaction tooltip stays inside `overflow-hidden` comment cards.
- Timeline: scroll bounds + overscroll, full-width rows, uniform week
  columns, today-line layered behind bars, theme-reactive header, and
  a full-title hover card.
- Live per-section filter counts; dropped redundant chip tooltips.
- Comment timestamp moved to the top-right; DRF comment API kept
  task-only under the polymorphic model.
- SSE applies MCP-driven events even when the actor is the current
  user.
- Misc: `EMAIL_*` env fallback, admin invite nav, focus-outline reset,
  deduped label hover tooltips, and activity diff em-dash cleanup.

## [0.1.0] — 2026-05-18 — Initial MVP

First production release. Self-hosted on Debian behind a Traefik edge
proxy at `actaspace.com`.

### Added

- **Workspaces, projects, tasks, subtasks** with human-readable slug
  prefixes (`ABC-123`) and per-project task number allocation.
- **HTMX-driven UI**: server-rendered Django templates + Alpine.js for
  local state, no client-side framework. Table view, Kanban board, and
  grouped list with stackable axes.
- **Rich text descriptions** via TipTap (bundled with esbuild). Inline
  toolbar appears on focus; supports lists, code, highlight, task
  lists, links.
- **Task detail in modal** (click) or full page (Ctrl+click / direct
  URL) with a unified comments + activity timeline.
- **Activity log** with per-field diff capture on every watched
  attribute; written exclusively by `apps.activity.services.log_event`
  from viewset `perform_*` hooks (ADR 0011).
- **Bulk operations** via a single universal endpoint
  `PATCH /api/v1/tasks/bulk/` with all-or-nothing transactionality
  (ADR 0012). Confirmation modal for bulk archive.
- **Real-time updates** over Server-Sent Events using
  `django-eventstream`; ASGI-only with Uvicorn (ADR 0015).
- **Filters**: status / priority / labels / assignee / project, both
  client-side and server-side; sidebar state persisted.
- **Labels** with hex colour, workspace-scoped, pills shared across
  filter sidebar, task row, table cell, task detail, create-task
  modal.
- **Project icons + colour picker** from a curated Lucide subset and
  21-colour Tailwind palette; live updates across sidebar / list /
  detail surfaces.
- **Favourite projects** — star toggle on the project list; the
  sidebar nav lists only starred projects.
- **User settings** page: first/last name, email (read-only), language.
- **Internationalization** — English (source) and Ukrainian, 255 / 255
  strings translated. `.po` committed, `.mo` built at deploy.
- **Dark / light theme** driven by CSS variables; pre-paint script in
  `<head>` prevents FOUC.
- **Custom login + closed signup** — Tailwind-branded
  `templates/account/login.html`, `is_open_for_signup` returns `False`
  for both the password and social adapters. Admins create accounts via
  Django admin.
- **Global HTMX error toast** with auto-dismiss.
- **Deployment plumbing**: production `Dockerfile`, `docker-entrypoint.sh`
  that runs `migrate` + `compilemessages` + `collectstatic` before
  starting Uvicorn, `CSRF_TRUSTED_ORIGINS` read from the environment,
  HSTS / secure cookies / `SECURE_PROXY_SSL_HEADER` for Traefik.

### Architecture

Decisions captured in `docs/decisions/0001-0019`. Headline:

- ASGI + Uvicorn (no WSGI Gunicorn — SSE on sync workers fails).
- Server-rendered templates + HTMX + Alpine + Chart.js + sortable.js;
  no React, no build step except TipTap and Tailwind.
- `request.user` is the only source of truth for activity-log actor.
- `master` is the deployable branch, `dev` is integration, history is
  linear (rebase, no merge commits).

[0.1.0]: https://github.com/voxquietam/acta/releases/tag/v0.1.0
