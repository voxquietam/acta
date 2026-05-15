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
│   ├── comments/               # Comment
│   ├── activity/               # ActivityLog model + log_event() helper
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
- `Comment` model and CRUD endpoints.
- Markdown rendering pipeline (shared with tasks/projects via a small util module here or in `apps/common/`).

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

- No `apps/notifications/`, `apps/integrations/`, `apps/webhooks/` — deferred until concrete need.
- No `apps/api/` umbrella — each domain app owns its own API endpoints.
- No frontend build pipeline (no `package.json`, no `webpack`, no `vite`).
- No Celery / Redis — synchronous request handling is sufficient for MVP load.
