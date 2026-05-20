# Project Layout

This spec describes the directory layout of the Acta repository and the responsibilities of each Django app. It is descriptive (what we will build) rather than prescriptive (a decision rationale) — the underlying decisions are in the ADRs.

## Directory tree

```
acta/
├── manage.py
├── acta/                       # Django project package
│   ├── __init__.py
│   ├── asgi.py
│   ├── wsgi.py
│   ├── urls.py                 # root URL conf
│   └── settings/
│       ├── __init__.py
│       ├── base.py             # shared
│       ├── dev.py              # local development
│       └── prod.py             # production
├── apps/
│   ├── __init__.py
│   ├── accounts/               # user model extensions, allauth wiring
│   ├── workspaces/             # Workspace, WorkspaceMember
│   ├── projects/               # Project, ProjectUpdate
│   ├── tasks/                  # Task, slug counter, bulk endpoints
│   ├── labels/                 # Label, LabelGroup
│   ├── comments/               # Comment (polymorphic: task OR project update, see ADR 0022)
│   ├── activity/               # ActivityLog model + log_event() helper
│   ├── notifications/          # Notification model + notify() per-user fan-out (ADR 0021/0023)
│   ├── common/                 # shared utils — markdown rendering + mention pipeline (markdown.py)
│   ├── mcp/                    # MCP server: read + write tools (ADR 0020)
│   └── web/                    # HTML page views, templates, static
├── templates/                  # global base templates (base.html, partials)
├── static/                     # global static (acta.css, acta.js, sortable.js post-MVP)
├── docs/                       # ADRs and specs (already in place)
├── requirements/
│   ├── base.txt
│   ├── dev.txt
│   └── prod.txt
├── docker-compose.yml          # prod stack (web + postgres)
├── docker-compose.dev.yml      # dev override (live reload, mounted volumes)
├── Dockerfile
├── .env.example
├── .gitignore
└── README.md
```

## App responsibilities

### `accounts`
- Custom `User` model extension (if needed beyond Django's `auth.User`).
- `django-allauth` configuration and Google OAuth provider setup.
- User profile fields (theme preference, display name, etc.).
- Signup blocked at the workspace level — see [0010](../decisions/0010-permissions.md).

### `workspaces`
- `Workspace`, `WorkspaceMember` models.
- Workspace membership management endpoints.
- Onboarding flow (post-login "no workspaces" screen, admin add-member endpoint).
- Workspace permissions classes (`IsWorkspaceMember`, `IsWorkspaceAdmin`, `IsWorkspaceOwner`).

### `projects`
- `Project`, `ProjectUpdate` models.
- Project CRUD + archive endpoints.
- Project update CRUD endpoints.
- Slug-prefix validation logic.

### `tasks`
- `Task` model + `next_task_number` counter logic with transactional locking.
- Single-task CRUD endpoints (`/api/v1/tasks/`, `/api/v1/tasks/{id}/`).
- Bulk endpoints (`PATCH /api/v1/tasks/bulk/`, `DELETE /api/v1/tasks/bulk/`).
- Subtask invariant enforcement (depth limit, project cascade).
- Search and filter logic on top of `django-filter`.

### `labels`
- `Label`, `LabelGroup` models.
- Label/group CRUD endpoints.
- Exclusive-group validation helper (used by tasks app when attaching labels).

### `comments`
- `Comment` model and CRUD endpoints. The model is **polymorphic**: a comment
  targets either a task or a project update, with one level of replies — see
  [0022](../decisions/0022-polymorphic-comments.md). The DRF `CommentViewSet`
  stays task-only by design; update comments are posted from the web app.
- Markdown rendering pipeline now lives in `apps/common/markdown.py` (shared by
  task descriptions, comments, and project-update bodies) — see below.

### `notifications`
- `Notification` model (one persistent row per recipient) + `notify()`, the
  single fan-out writer mirroring `log_event`. Per-user SSE broadcast over the
  `user-<id>` channel. See [0021](../decisions/0021-notification-inbox.md).
- Mention parsing helpers (`parse_mentioned_user_ids`, `notify_mentions`) for
  the `@user` pipeline — see [0023](../decisions/0023-mentions.md).

### `common`
- Realized as `apps/common/markdown.py`: the markdown render + bleach
  sanitization + mention-chip rewriting shared across surfaces. (The original
  "premature `common/` is an anti-pattern" caution below held until markdown
  rendering had three real call sites; at that point it was extracted here.)

### `mcp`
- MCP server exposing read + write tools over two transports. See
  [0020](../decisions/0020-mcp.md).

### `activity`
- `ActivityLog` model.
- `log_event(...)` helper — the single entry point for writing activity rows.
- Activity feed endpoints (workspace-wide and per-task).
- `bulk_id` grouping logic for feed queries.

### `web`
- HTML page views (Django function-based or class-based views that return rendered templates).
- All `templates/` for pages: login, dashboard, project, task detail, members, settings.
- Per-page JS modules under `static/js/pages/`.
- This is the only app that depends on `django.contrib.staticfiles` and template directories beyond `templates/`.

## Cross-app conventions

- **No circular imports.** App dependency graph is layered: `web` → `tasks`/`projects`/etc. → `workspaces` → `accounts`. `activity` is depended on by everything that writes events; it depends on `workspaces` only.
- **Shared utilities** (markdown rendering, slug helpers, queryset mixins) live in `apps/common/` if and when a real need arises. Premature `common/` is an anti-pattern; start by putting helpers in the owning app and extract only when reused.
- **Permissions classes** live in `apps/workspaces/permissions.py` and are imported wherever needed. Avoid duplicating membership checks.
- **API URLs** are wired in each app's `urls.py` and included from `acta/urls.py` under `/api/v1/`.
- **Templates** for an app live in `templates/{app_name}/` if the app owns pages; reusable partials live in `templates/partials/`.

## Settings split

- `base.py` — common settings: installed apps, middleware, DRF config, allauth providers, templates dirs.
- `dev.py` — `DEBUG=True`, SQLite or local Postgres, console email backend, relaxed CSP.
- `prod.py` — `DEBUG=False`, Postgres from env, secure cookies, ALLOWED_HOSTS, structured logging.

`DJANGO_SETTINGS_MODULE` picks the right file (default in `manage.py` → `acta.settings.dev`).

## Requirements split

- `base.txt` — Django, DRF, django-allauth, django-filter, psycopg, markdown, bleach.
- `dev.txt` — `-r base.txt` + django-debug-toolbar, ipython, pytest, pytest-django (if/when tests are added).
- `prod.txt` — `-r base.txt` + gunicorn, sentry-sdk (optional).

## Docker

- `Dockerfile` — single image used in dev and prod (different command per environment).
- `docker-compose.yml` — production-style stack: web + Postgres + (optional) Caddy/nginx reverse proxy. Detailed in a future deploy spec.
- `docker-compose.dev.yml` — override that mounts source as a volume, runs Django dev server, exposes ports for local debugging.

## What's NOT in MVP

- No `apps/integrations/`, `apps/webhooks/` — deferred until concrete need.
  (`apps/notifications/` was originally listed here as deferred; it has since
  shipped — see the app tree above and [0021](../decisions/0021-notification-inbox.md).)
- No `apps/api/` umbrella — each domain app owns its own API endpoints.
- A frontend build pipeline exists but is narrow: `package.json` + esbuild bundle
  the TipTap editor and compile Tailwind only (see
  [0014](../decisions/0014-frontend-architecture.md)). No webpack / vite, and the
  rest of the frontend stays on CDN.
- No Celery / Redis — synchronous request handling is sufficient for MVP load.
