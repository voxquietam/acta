# ADR 0006: MVP Scope

**Status:** accepted
**Date:** 2026-05-15
**Note:** Amended on 2026-05-15 to include real-time kanban, dashboards, in-app notifications, and drag-and-drop kanban — the scope grew once the frontend pivoted to HTMX + Alpine + Chart.js (see [0014](0014-frontend-architecture.md)).

## Context

Acta is a 3–4 week vibe-coding sprint, built alongside an existing job (`ksu24.back`). Scope discipline is the difference between shipping and burning out. This ADR fixes the line between what ships in MVP and what waits.

## Decision

### In MVP

- **Tasks:** CRUD; fields = `title`, `description` (Markdown), `status` (fixed enum, see [0004](0004-statuses.md)), `priority`, `size`, `due_date`, `assignee`, `labels`, `parent` (for subtasks, see [0003](0003-hierarchy.md)).
- **Projects:** CRUD with slug-based references in the format `HRW-49`. Each project has its own slug prefix and incrementing counter.
- **Workspaces:** single-tenant entity at the top; project, members, and labels are scoped to a workspace.
- **Members + Auth:** Google OAuth via `django-allauth` (see [0002](0002-auth.md)). Workspace membership granted manually.
- **Views:** Kanban (5 columns based on fixed statuses) and table view.
- **Drag-and-drop kanban** — via `sortable.js` wired through HTMX. See [0014](0014-frontend-architecture.md).
- **Comments:** Markdown, attached to tasks.
- **Activity log:** auto-tracked via explicit `log_event()` calls. JSONB payload. See [0011](0011-activity-log.md).
- **Labels:** workspace-scoped with optional label groups (Linear-style). See [0008](0008-labels.md).
- **Project Updates:** Linear-style manual status posts per project, with health indicator. See [0009](0009-project-updates.md).
- **Search & filters:** ILIKE + structured filters (see [0005](0005-search.md)).
- **Bulk operations:** the killer differentiator vs Kaneo. See [0012](0012-bulk-operations.md).
- **Real-time updates** — SSE-based, every connected client sees kanban moves, status changes, comments live. See [0015](0015-real-time.md).
- **Dashboards** — workspace overview, project overview, and personal "my work" pages with charts (tasks by status, throughput, workload). See [0016](0016-dashboards.md).
- **In-app notifications** — toasts for events relevant to the current user (assigned to you, comment on your task, your task moved by someone else). See [0017](0017-notifications.md).

### Out of MVP

- **Outgoing webhooks** — no concrete consumer in scope yet (Vox is mostly a *receiver* of webhooks from Kaneo). Deferred until a real integration appears, so the event contract can be designed for actual needs.
- **Cycles / sprints**
- **File attachments**
- **WebSocket bi-directional protocols** — SSE is one-way (server → client). Sufficient for MVP; WebSocket reserved for cases that need client → server pushes outside of normal HTTP requests (none in MVP).
- **Browser desktop notifications** (Notification API). In-app toasts only.
- **Email notifications.**
- **@-mentions in comments / descriptions.**
- **Mobile-first UI** — desktop-first; mobile layouts later.
- **PostgreSQL full-text search** — see [0005](0005-search.md).
- **Per-project custom statuses** — see [0004](0004-statuses.md).
- **Sub-projects / Initiatives** — see [0003](0003-hierarchy.md).
- **SSO with ksu24.back** — Google OAuth covers the team; ksu24 SSO can be added as another `django-allauth` provider later.

## Why

- 3–4 weeks is tight. Every item above the line directly serves the daily workflow (manage tasks, see board live, search, bulk-edit, see what's going on, get pinged when something concerns you). Items below the line are nice-to-haves whose absence won't block real use.
- Bulk operations stay in scope because they are the *reason for building Acta in the first place* — moving 50 tasks at once is the headline pain point with Kaneo.
- Real-time, dashboards, and notifications got added because the frontend pivot to HTMX + Alpine made them cheap, and they were the user's stated must-haves. Without those, Acta would be a slightly nicer Kaneo, not a meaningfully better tool.
- Webhooks fall out because the team's pain is with *incoming* webhooks from Kaneo. Acta as a webhook *producer* has no concrete consumer planned.
- @-mentions fell out to keep the markdown pipeline and notifications simple. Can be added later as a serializer extension + new notification trigger.

## Consequences

- The expanded MVP is closer to 4–5 weeks than 3, realistically. Dashboards and real-time add real implementation surface, even with cheap libraries.
- Anything in "Out of MVP" can be revisited after the MVP ships and is used by the team for at least two weeks.
- Adding items mid-sprint requires removing something else of comparable size, or extending the timeline.
- The "second-system effect" risk (over-engineering instead of using ksu24.back) is mitigated by this explicit line. Re-audit it weekly.
