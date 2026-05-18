# Changelog

All notable changes to Acta are documented in this file. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

From `v0.2.0` onward this file is generated from Conventional Commits
with `git-cliff` (see `Makefile` once introduced). `v0.1.0` is
hand-written.

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
