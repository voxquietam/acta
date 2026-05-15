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

- **Backend:** Django 5 + Django REST Framework
- **Database:** PostgreSQL (containerized)
- **Frontend:** Django templates + Tailwind CSS + vanilla JS (no React, no build step)
- **Deployment:** Docker Compose (separate stack on the server, sitting next to Kaneo)
- **Auth:** see [0002-auth.md](0002-auth.md)

## Why

- **DRF:** `ModelViewSet` gives CRUD for free. Bulk operations through `ListSerializer` + `bulk_create/bulk_update` — 5–10 lines. AI tooling knows DRF patterns inside out → faster vibe-coding.
- **Django ecosystem:** admin acts as a free CRUD UI from day one (manage data before the UI is written), `django-allauth` for Google OAuth, signals for activity-log auto-tracking, migrations out of the box.
- **HTML + Tailwind + vanilla JS:** already proven in `ksu24.back` (~8000 lines of working code). Zero build step. One process instead of backend + frontend dev servers.
- **PostgreSQL:** JSONB for activity log / event sourcing (see future `spec/activity-log.md`).

## Consequences

- Async work (background jobs, heavy reports) — handled later via Celery/RQ; DRF is not async-first. Not needed for MVP.
- Real-time updates (WebSocket/SSE) — out of MVP scope (see [0006-mvp-scope.md](0006-mvp-scope.md)).
- Rewriting to an async-first stack later would be non-trivial, but is not on the roadmap.
- A build-less frontend means no React/Vue/Svelte components. Drag-and-drop kanban will use `sortable.js` (vanilla) post-MVP.
