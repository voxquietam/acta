# Changelog

All notable changes to Acta are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are hand-written from the Conventional Commit history for now.
Automating this with `git-cliff` is deferred until `v1.0.0`.

## [0.4.0] — 2026-05-27

Backlog grooming, create-task-from-text, JSON export, a three-date task
model, account-settings polish, and a security hardening of password
rules — on top of a redesigned workspace-settings page.

### ⚠ Breaking / migrations

Run migrations after deploying. Existing accounts with weak passwords
keep working; only new passwords are validated.

- `Task.end_date` added — the timeline bar-end is now separate from the
  deadline (`due_date`).
- `Task.completed_at` added (+ backfill) to support date-range filtering
  on a selectable field.
- `AUTH_PASSWORD_VALIDATORS` enabled (standard Django set, min length 8).
  Weak passwords such as `123` are now rejected at signup and on
  password set/change.

### Added

- **Backlog grooming** — a dedicated Backlog tab with inline promote and
  a show-backlog toggle.
- **Create task from text** — spin a new task off a comment (auto-linked
  as related) or off any selected text in a comment or description; lands
  in a prefilled create modal.
- **Export filtered views as JSON** — All Tasks, My Work, project task
  lists, and the project overview export the currently filtered view
  (reactions + replies included in the overview shape).
- **Password set/change** in a modal, alongside a redesigned account
  settings page.

### Changed

- **Workspace settings** reworked into a two-column layout with
  scrollable member and invite lists.
- Backlog / archived toggles now resolve entirely client-side for instant
  feedback; non-active view panels load lazily.
- The Telegram webhook auto-registers on deploy from
  `ACTA_PUBLIC_BASE_URL`.

### Fixed

- Google signup now works when matched to a pending invite by email, and
  the blocked-signup page explains a missing invite.
- `serve_avatar` returns 404 (not 500) on a missing file.
- Timeline: stamp `start_date` for tasks created in-progress; refresh the
  bar + hover tooltip after a drag-resize; don't push the URL when
  opening a task modal from the timeline.
- Backlog tab stays populated regardless of the show-backlog toggle, and
  the toggle is respected in the tasks JSON export.
- Open a single SSE stream for the active workspace only.

### Performance

- Cache-bust avatar URLs with `?v=<version>` everywhere.
- Dropped the modal backdrop-blur that caused cursor lag on weak GPUs.

### Tests

- Integration coverage for every DRF viewset through `APIClient` (tasks,
  projects, project updates, comments, labels, label groups, workspaces,
  workspace members, activity log).

## [0.3.0] — 2026-05-24

Large feature release on top of v0.2.x: Telegram notifications, file
attachments + avatars everywhere, workspace cycles, scrumban (WIP limits,
aging, insights), an admin-managed job scheduler, single-active-workspace
scoping, a unified context menu, broadcast announcements, and Google login.

### ⚠ Breaking / migrations

Run migrations and rebuild the image (the scheduler adds a new compose
service; uploads need the `acta-media` volume + Pillow).

- New apps / models: `apps.attachments` (Attachment, content-addressed
  dedup, ref-counted delete), `apps.telegram` (account linking, per-kind
  message templates + delivery prefs), `apps.cycles` (workspace
  auto-rolling cycles); avatars on `User`; `User.active_workspace`.
- New task statuses: `ready` (replenishment buffer) and `cancelled`
  (terminal); tasks can move between projects.
- WIP limits on projects + workspaces (personal + column).
- Infra: recurring jobs run via a django-q2 `qcluster` service (no host
  cron). New `acta-media` volume.
- Auth: Google login — verified-email links existing accounts; social
  signup gated on a matching invite. New allauth settings.

### Added

- **Telegram notifications**: account-linking + `notify()` fan-out,
  admin-editable per-kind message templates, per-kind delivery prefs,
  localized bot replies, rich placeholders, markdown-cleaned previews.
- **File attachments**: task + comment attachments (upload / serve /
  delete), inline image paste/drop everywhere (description editor,
  comments, project updates + their comments, create-task modal),
  content-addressed dedup, lightbox gallery (arrow keys),
  alt-from-filename, orphan GC.
- **Avatars**: upload + square-crop + serve, in-browser downscale, shown
  across comments, members, overview, topbar, tables, lists, filters,
  kanban, timeline, project list, link search.
- **Cycles**: workspace-level auto-rolling cycles + assignment +
  dashboard, auto-rollover of unfinished tasks, start / approaching-end
  notifications.
- **Scrumban**: project + workspace WIP limits (personal + column), aging
  WIP, `ready` status, project insights (cycle / lead time + throughput),
  cumulative-flow + bottleneck dashboard.
- **Single active workspace** + sidebar switcher.
- **Unified context menu**: right-click + bulk task actions.
- **Admin-managed scheduler**: django-q2 schedules editable in `/admin/`.
- **Announcements**: broadcast to the workspace inbox (force-delivered).
- **Google login**: "Continue with Google" on login + signup.
- Cancel-task status, move task between projects, editable Size cell,
  size filter, project favourite-star, create-modal project prefill,
  flash-message toasts, email invites in Members.

### Fixed

- 29 fixes across kanban drag-and-drop, filters, timeline, notification
  previews, query counts, and UI polish. See `git log v0.2.1..v0.3.0`.

### Performance

- In-browser avatar downscale before upload; batched cycle dashboard
  summaries; draft-decode avatar processing.

## [0.2.1] — 2026-05-21

Patch release — sidebar version-link polish on top of v0.2.0.

### Fixed

- Sidebar version no longer wraps to its own line: the changelog link
  is split out of the dashboard link (nested `<a>` is invalid HTML and
  the browser broke it onto a new line).
- Dropped the version-link tooltip that clipped off the top of the
  window; the link keeps an `aria-label` for accessibility.
- The `[0.2.0]` changelog heading now links to its release tag.

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

[0.2.1]: https://github.com/voxquietam/acta/releases/tag/v0.2.1
[0.2.0]: https://github.com/voxquietam/acta/releases/tag/v0.2.0
[0.1.0]: https://github.com/voxquietam/acta/releases/tag/v0.1.0
