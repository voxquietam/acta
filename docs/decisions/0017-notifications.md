# ADR 0017: Notifications

**Status:** accepted
**Date:** 2026-05-15

## Context

The MVP includes in-app notifications: a toast appears in the corner when something relevant happens to the user. Linear, Asana, and Jira all have this; it's the lightest possible way to pull a user's attention to an event they care about, without leaving the app.

The hard parts of a notification system are usually:

1. Deciding **which events** trigger a notification for **which users**.
2. Delivering them **without lag** (real-time, already solved in [0015](0015-real-time.md)).
3. **Persistence** — should the user see a "5 unread" badge, an inbox of past notifications, etc.?
4. **Channels** — in-app, email, browser desktop, mobile push.

This ADR scopes all of the above for MVP.

## Decisions

### Channels — in-app only

- **In-app toast** is the only notification channel in MVP.
- **No email.** Email notifications need transactional email infra (SMTP/SES/etc.), opt-out preferences, digest scheduling. Out of scope.
- **No browser desktop notifications** (Notification API). They require an explicit permission prompt, complicate local testing, and are easy to add later as a per-user setting.
- **No mobile push.** No mobile app exists.

### Triggers (which events surface as notifications)

A notification is shown for a given user when an event matches one of these patterns:

- **`task.assigned` to me.** Someone (anyone, including me-by-myself) sets `assignee=me`. Self-assignments are suppressed via the actor filter below.
- **`task.status_changed` on a task I'm assigned to or I reported.** Someone else moved my task.
- **`task.due_changed` on a task I'm assigned to.** Due date moved.
- **`task.deleted` on a task I'm assigned to or I reported.** My task disappeared.
- **`comment.created` on a task I'm assigned to, I reported, or I previously commented on.** Discussion progressed.
- **`project_update.created` on a project where I'm a member (i.e. always, since all members see all projects in MVP).** New status post — visible to the whole workspace.

### Suppression rules

- **Never notify the actor about their own action.** If I move my own task to Done, I don't see a toast. (Already covered by the SSE actor-exclusion filter in [0015](0015-real-time.md), reinforced here.)
- **Bulk operations collapse.** If a bulk PATCH affects N tasks where I'm assigned, I get **one** toast: "Vox moved 12 of your tasks to In Review." The `bulk_id` from [0011](0011-activity-log.md) groups them. Implementation: client-side debounce keyed by `bulk_id` over a 500 ms window.
- **Rate limit on the client.** No more than 5 toasts visible at once; older toasts auto-dismiss after 6 seconds.

### Delivery — over the existing SSE stream

- Notifications are not a separate transport. The SSE workspace stream (see [0015](0015-real-time.md)) delivers every event; the client decides which ones turn into a toast.
- A small vanilla JS module subscribes to the SSE EventSource, evaluates each event against the trigger rules above using the current user's `id` and a precomputed list of "task ids I'm watching" (assignee + reporter + commenter), and dispatches a custom `acta:notify` DOM event with the rendered message.
- An Alpine.js component listens for `acta:notify` and manages the toast stack: animation, dismissal, click-to-go-to-task.

### Watch list — what tasks am I "involved with"

- Computed server-side on page load and re-fetched on tab focus.
- Endpoint: `GET /api/v1/me/watching/` → `{task_ids: [...]}`. Returns all task IDs where I'm assignee, reporter, or have ever commented.
- Cached client-side for the duration of the page session. Updated by the SSE event handler when:
  - I'm assigned a task → add to watch list.
  - I'm unassigned and not the reporter and have no comments → remove from watch list.
  - I create a comment → add to watch list.

### Persistence — no notification inbox in MVP

- Toasts are ephemeral. Once dismissed or auto-cleared, they're gone.
- **No persistent notification list, no unread badge, no "mark as read" state in MVP.**
- Rationale: the activity feed (`/activity/`) already shows everything that happened. A notification inbox would be a filtered view of the activity feed; building it now is duplicating UX surface for marginal benefit.
- Will revisit post-MVP if users say "I missed a toast and now I can't find what happened."

### Click behavior

- Click on a toast → navigate to the relevant URL:
  - Task events → `/projects/{slug_prefix}/{number}/`
  - Comment events → same task page, scrolled to the comment
  - Project update → `/projects/{slug_prefix}/` with the update expanded

### Tab-aware behavior

- If the tab is hidden (`document.hidden === true`), toasts queue but don't render. When the tab becomes visible again, a single "summary toast" appears: "3 updates while you were away" linking to `/activity/`. Avoids spamming a stack of stale toasts.

## Why

- **Reusing the SSE stream** removes a whole category of "the notification said X but the activity log says Y" bugs. One source of truth (activity events), one delivery channel.
- **Client-side trigger evaluation** keeps the server logic simple — broadcast everything to all workspace members; let each browser decide what's a toast.
- **No persistence in MVP** sidesteps the inbox UX, the unread-count badge, the read/unread state machine, and the inevitable "mark all read" bug. Activity feed covers the audit need.
- **Bulk collapsing** preserves the value of bulk operations (one Vox action) without firing N toasts.
- **Tab-aware queueing** matches user expectation: nobody wants a stack of toasts from the past 20 minutes.

## Consequences

- The "watch list" endpoint needs a reasonable index — `(assignee_id)`, `(reporter_id)`, and a comments-by-author join. Verify query plans during implementation.
- The vanilla JS module that evaluates triggers has to stay simple and well-tested; it's the only piece of UI logic that isn't HTMX-driven.
- A user can't go back and see "what notifications fired earlier" — only the activity feed. Make sure the activity feed's filters are good enough to make this a non-issue (filter by "events that mention me" would be a nice UX).
- If we add email or desktop notifications later, the trigger rules and watch-list computation move to the server side; in-app stays as-is. Migration is mechanical.

## Open Questions

- Should `task.priority_changed` on my task notify? Lean **yes**, but lower priority than status/due changes. To be decided at implementation if it feels too noisy.
- Should `task.labels_changed` on my task notify? Lean **no** — labels change too often, low signal per event.
- Should I get a notification when **I'm removed as assignee**? Yes, that's a `task.assigned` event with `to_user_id = null` (or someone else) and the previous `from_user_id = me`. Captured by the trigger as written.
- Should `member.added` notify the added member? Yes eventually, but the user lands on a "no workspaces" screen before being added — and after being added they see the new workspace on next reload. Toast is overkill. Skip in MVP.
