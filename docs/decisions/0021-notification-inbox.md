# ADR 0021: Notification inbox (persistent, per-user)

**Status:** accepted
**Date:** 2026-05-20
**Supersedes:** the "Persistence — no notification inbox in MVP" section
of [0017](0017-notifications.md).

## Context

ADR 0017 scoped notifications for the MVP as **ephemeral in-app toasts
only** — explicitly *no persistent list, no unread badge, no read/unread
state*. The reasoning was sound at the time: the activity feed already
records everything, and an inbox is extra UX surface.

Acta has since moved past the MVP line. A persistent inbox is now wanted
as a first-class surface (`comp-page-inbox` from the design system): a
list a user can come back to, mark read, filter, and archive — the thing
0017 deferred. This ADR records that decision and the Phase 0 shape.

## Decisions

- **A persistent `Notification` row per recipient.** New app
  `apps/notifications`. A notification is the *personal fan-out* of a
  workspace event; the global `ActivityLog` stays the single append-only
  event stream and is untouched. Notifications carry denormalized
  ``preview`` / ``payload`` so the inbox list renders without re-walking
  the target graph, and survive deletion of the task/comment they point
  at (FKs are ``SET_NULL``).
- **`notify()` is the single writer**, mirroring `log_event`'s role for
  activity. It enforces the self-suppression rule from 0017 — never
  notify the actor about their own action.
- **Triggers (Phase 0):** `task.assigned` (→ both the new and the
  previous assignee, so a person learns a task left their plate),
  `task.status_changed`, `task.priority_changed`, `task.due_changed`
  (→ assignee + reporter), `comment.created` (→ assignee + reporter).
  Fan-out hangs off the one path every single-task edit funnels through
  (`apps.tasks.events.emit_task_diff_events`) plus the three
  `comment.created` call sites (web, DRF, MCP). **Labels do not notify**
  (too noisy — confirms 0017's lean-no). Note `task.due_changed` is the
  *due date was edited* event; **"due soon"** (deadline approaching) is a
  separate time-driven alert that needs a scheduler and is deferred.
- **Read / unread / archive** state is persisted. Inbox endpoints:
  open (mark read + preview), toggle read, archive, bulk action, mark
  all read.
- **Inbox page** at `/inbox/`: Notifications tab with filter chips
  (All / Unread / @Mentions / Assigned / Due / Comments), split list /
  preview, sidebar entry with an unread badge.

## Deferred (tracked, not in Phase 0)

- **Live SSE arrival** (new-notification push + sidebar pulse). The badge
  updates on navigation (context processor) and out-of-band on inbox
  actions; real-time push over a per-user channel is a fast-follow.
- **Mentions** — `@user` parsing in comment + description editors, the
  `mention` notification kind, and the highlighted comment-thread preview.
  This is the next phase and the headline ask.
- **Updates tab** over `ProjectUpdate` (compose UI + subscriptions).
- **Due-soon + Snooze** — both need a scheduler, which the project does
  not have yet (no Celery/cron). Out of scope until that lands. See the
  ``project-todo-due-soon-notifications`` roadmap note.

## Why

- **Reusing `emit_task_diff_events`** as the fan-out point means web, DRF,
  and MCP edits all generate notifications from one place — no per-surface
  duplication, consistent with how the activity log already centralizes.
- **Per-user rows (not a filtered activity view)** make read/unread,
  archive, and per-recipient counts trivial and indexed, instead of
  recomputing "is this event for me" on every read.
- **Keeping `ActivityLog` untouched** preserves ADR 0011's "single writer,
  append-only" guarantee; notifications are a derived, mutable layer on
  top.

## Consequences

- The sidebar unread badge is computed in a context processor, adding one
  indexed `COUNT` per authenticated page render. Acceptable; the
  `(recipient, archived_at, is_read, -created_at)` index covers it.
- A user can mark-read / archive; there is no "unarchive" UI in Phase 0
  (archive is terminal from the inbox — the activity feed still has the
  underlying event).
- When mentions land, `notify()` and the `Notification.Kind` enum already
  carry the `mention` kind — no migration needed to start emitting them.
