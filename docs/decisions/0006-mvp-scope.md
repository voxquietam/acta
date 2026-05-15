# ADR 0006: MVP Scope

**Status:** accepted
**Date:** 2026-05-15

## Context

Acta is a 3–4 week vibe-coding sprint, built alongside an existing job (`ksu24.back`). Scope discipline is the difference between shipping and burning out. This ADR fixes the line between what ships in MVP and what waits.

## Decision

### In MVP

- **Tasks:** CRUD; fields = `title`, `description` (Markdown), `status` (fixed enum, see [0004](0004-statuses.md)), `priority`, `size`, `due_date`, `assignee`, `labels`, `parent` (for subtasks, see [0003](0003-hierarchy.md)).
- **Projects:** CRUD with slug-based references in the format `HRW-49`. Each project has its own slug prefix and incrementing counter.
- **Workspaces:** single-tenant entity at the top; project, members, and labels are scoped to a workspace.
- **Members + Auth:** Google OAuth via `django-allauth` (see [0002](0002-auth.md)). Workspace membership granted manually.
- **Views:** Kanban (5 columns based on fixed statuses) and table view.
- **Comments:** Markdown, attached to tasks.
- **Activity log:** auto-tracked via Django signals on Task and related models. Stored with JSONB payload for flexibility.
- **Labels:** per-workspace (or per-project — to be decided in `spec/data-model.md`).
- **Search & filters:** ILIKE + structured filters (see [0005](0005-search.md)).
- **Bulk operations:** the killer differentiator vs Kaneo. Exact endpoint list to be specified in `spec/bulk-operations.md`. At minimum: bulk update status, bulk assign, bulk add/remove label, bulk move to project, bulk delete.

### Out of MVP

- **Outgoing webhooks** — no concrete consumer in scope yet (Vox is mostly a *receiver* of webhooks from Kaneo). Deferred until a real integration appears, so the event contract can be designed for actual needs.
- **Cycles / sprints**
- **File attachments**
- **Real-time updates** (WebSocket / SSE)
- **Drag-and-drop kanban** — add post-MVP using `sortable.js` (vanilla)
- **Mobile-first UI** — desktop-first; mobile layouts later
- **PostgreSQL full-text search** — see [0005](0005-search.md)
- **Per-project custom statuses** — see [0004](0004-statuses.md)
- **Sub-projects / Initiatives** — see [0003](0003-hierarchy.md)
- **SSO with ksu24.back** — Google OAuth covers the team; ksu24 SSO can be added as another `django-allauth` provider later

## Why

- 3–4 weeks is tight. Every item above the line directly serves the daily workflow (manage tasks, see board, search, bulk-edit). Items below the line are nice-to-haves whose absence won't block real use.
- Bulk operations stay in scope because they are the *reason for building Acta in the first place* — moving 50 tasks at once is the headline pain point with Kaneo.
- Webhooks fall out because the team's pain is with *incoming* webhooks from Kaneo. Acta as a webhook *producer* has no concrete consumer planned.

## Consequences

- Anything in "Out of MVP" can be revisited after the MVP ships and is used by the team for at least two weeks.
- Adding items mid-sprint requires removing something else of comparable size, or extending the timeline.
- The "second-system effect" risk (over-engineering instead of using ksu24.back) is mitigated by this explicit line.
