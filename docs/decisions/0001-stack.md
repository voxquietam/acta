# ADR 0001: Technology Stack

**Status:** accepted
**Date:** 2026-05-15

## Context

Acta is a task tracker — an alternative to Kaneo. The author's pain points with Kaneo: TS/Node monorepo (unfamiliar stack), sluggish REST API, no bulk operations, webhook `actor` reporting the assignee instead of the real change author. MVP target: 3-4 weeks of focused vibe-coding with an AI pair.

The author (Vox) is a backend dev at KSU24 with deep Django/Python experience. A parallel project, `ksu24.back`, already runs on Django.

## Options

1. **Django + DRF + Postgres + HTML/Tailwind/vanilla JS** — the stack the author knows best.
2. **FastAPI + SQLAlchemy + React** — modern, async, but everything from scratch: migrations (Alembic), auth, admin, signals.
3. **Forking Kaneo (TS/Node)** — rejected upfront: foreign monorepo, foreign stack.

Within Django, **DRF** vs **Django Ninja** was considered. Ninja is newer (Pydantic, type hints, async-first) but CRUD in Ninja requires ~6 functions per resource vs DRF's single `ModelViewSet`. For AI-driven vibe-coding, DRF wins on velocity.

## Decision

- **Backend:** Django 5 + Django REST Framework, running on **ASGI** (Uvicorn).
- **Database:** PostgreSQL (containerized).
- **Frontend:** Django templates + Tailwind CSS + HTMX + Alpine.js + Chart.js + `sortable.js` (no React, no build step). See [0014](0014-frontend-architecture.md).
- **Deployment:** Docker Compose (separate stack on the server, sitting next to Kaneo). Uvicorn behind Caddy or nginx; **never sync WSGI Gunicorn** — would break SSE per [0015](0015-real-time.md).
- **Auth:** see [0002-auth.md](0002-auth.md).

## Why

- **DRF:** `ModelViewSet` gives CRUD for free. Bulk operations through `ListSerializer` + `bulk_create/bulk_update` — 5–10 lines. AI tooling knows DRF patterns inside out → faster vibe-coding.
- **Django ecosystem:** admin acts as a free CRUD UI from day one (manage data before the UI is written), `django-allauth` for Google OAuth, signals for activity-log auto-tracking, migrations out of the box.
- **HTML + Tailwind + vanilla JS:** already proven in `ksu24.back` (~8000 lines of working code). Zero build step. One process instead of backend + frontend dev servers.
- **PostgreSQL:** JSONB for activity log / event sourcing (see future `spec/activity-log.md`).

## Consequences

- Async background work (Celery/RQ) — not needed for MVP. ASGI handles long-lived SSE connections in-process; CPU-bound batch jobs are deferred.
- Real-time updates (SSE) are in MVP scope and require the ASGI deployment — see [0015](0015-real-time.md).
- ASGI is a deliberate departure from `ksu24.back`'s WSGI Gunicorn setup. Django 5 supports sync views unchanged under ASGI (auto-wrapped by `sync_to_async`), so application code style is unaffected.
- A build-less frontend means no React/Vue/Svelte components. Drag-and-drop kanban uses `sortable.js` (vanilla, no deps) wired through HTMX — already in MVP.
