# ADR 0030: Meetings / calls

**Status:** accepted
**Date:** 2026-06-10

## Context

The team wanted to log calls — record that time was spent talking, link the
call to the tasks it was about, and have that visible everywhere the linked
tasks appear. A call is explicitly **not a task**: it has no status,
assignee, or lifecycle; it's a record of time spent. Open questions:

1. **Who owns the time** — a per-task worklog (Jira/Tempo style), or a
   standalone object with its own duration?
2. **How is per-task time attributed** when one call touches several tasks?
3. **What's in scope** for the first cut — notes, participants?
4. Where does it surface, and how does it stay live?

Surveyed: Jira+Tempo (per-task worklogs with split), ClickUp (task-anchored
timers), Notion (meeting DB related to tasks, rollups), Fellow/Granola
(meeting notes → action items). Google Calendar / Zoom import was considered
and **deferred** — see Consequences.

## Decision

**A first-class `Meeting`, not a worklog on a task.** `apps/meetings/Meeting`
is workspace-level and holds the call itself: `title`, `happened_at`,
`duration_minutes`, `participants` (M2M user), `notes` (Markdown), and
`tasks` (M2M, `related_name="meetings"`). `project` is an optional, soft
(`SET_NULL`) tag — a call may link tasks across projects, so it is not owned
by one. `created_by` is `SET_NULL`. This matches the Notion/Fellow archetype
(standalone object with relations) over the Tempo worklog archetype.

**Duration on the meeting + rollup, no through-model.** The whole-call
duration lives on the meeting. A linked task surfaces its calls and sums
their minutes as a "time spent around this task" signal. This is a plain
M2M; there is deliberately **no** per-task minute allocation (the Tempo
split). The trade-off: a call linked to N tasks shows its full duration on
each, so summing the rollup *across* tasks double-counts. Therefore any
workspace/project time total is computed over `Meeting` rows, never by
summing the per-task rollup. This invariant is pinned by a model test.

**Activity log via the standard writer.** Create / update / delete call
`log_event()` with the new `ActivityLog.TARGET_MEETING`
(`meeting.created|updated|deleted`), so calls appear in the feed and over
SSE with no bespoke plumbing.

**Surfaces, kept in sync by one event.** A workspace `/calls/` page (list +
project filter + create/edit modal), and a lazy "Meetings" panel on the task
detail rail. Every mutation returns `204 + HX-Trigger: acta:meeting-changed`;
the list and each task panel listen and refetch, and the modal closes. The
task panel is a lazy fragment (`task_meetings_fragment`, fetched on `load`
and on `acta:meeting-changed`) so the meetings join never enters the
`TaskDetailView` / `task_meta_fragment` querysets — no N+1 on the hot detail
path. The modal's task picker reuses the links-panel autocomplete shape over
a workspace-scoped `meeting_task_search` endpoint.

## Consequences

- New app `apps/meetings/` (model + admin + migration `0001`) and
  `apps/web/views_meetings.py` (list / CRUD / detail page / task panel / task
  search / comments), mirroring the recurring-tasks surface.
- `ActivityLog.target_type` gains `meeting`; no schema change (it is a free
  `CharField`).
- A call links to tasks but is workspace-scoped; deleting a project clears
  the tag, deleting a task drops the M2M row — the call survives both.
- **Comments**: the dual-target `Comment` extends to a third target
  (`meeting`); the check constraint becomes exactly-one-of-three. Meeting
  comments are a plain Markdown thread (no TipTap mentions / inline images —
  a meeting may have no project to scope those to) reusing the
  owner-agnostic edit/delete endpoints. Off the activity log, like
  project-update comments.
- **Notifications**: `Notification` gains a `meeting` FK and a `MEETING`
  kind; logging a call notifies its participants in-app and over Telegram
  (`notify_meeting_created`), on edit only newly-added participants. The
  actor is self-suppressed.
- **My Work**: a personal calls strip sits above the task groups — a thin
  "recent calls" lenta plus "upcoming calls" cards, scoped to meetings the
  user takes part in or logged. Refetched on `acta:meeting-changed`.
- **Deferred:** Google Calendar / Zoom import (would add `source` /
  `external_id` / `external_url` and a per-user opt-in OAuth connection +
  django-q poller, feeding the same `Meeting`); per-task minute split;
  scheduled "upcoming call" reminder notifications (current Telegram fan-out
  fires on create/edit, not as a pre-call reminder); SSE live-insert for
  peers (mirrors recurring — peers see new calls on next navigation /
  refetch).
