# ADR 0012: Bulk Operations

**Status:** accepted
**Date:** 2026-05-15

## Context

Bulk operations are the headline differentiator of Acta vs Kaneo. Kaneo's REST API has no bulk endpoints — moving 50 tasks at once means 50 sequential calls, often paired with rate limits and inconsistent intermediate state. Acta will support multi-task updates as a first-class operation.

The design has to balance ergonomics (one round-trip for "move these 30 tasks to Done and assign them to me") with predictability (clear atomicity, clear errors, sane permission model).

## Decisions

### Endpoint shape — single universal PATCH

Two endpoints cover the entire surface:

```
PATCH  /api/tasks/bulk/      # update one or more fields on many tasks
DELETE /api/tasks/bulk/      # delete many tasks
```

Request body for `PATCH`:

```json
{
  "ids": [101, 102, 103],
  "updates": {
    "status": "in-progress",
    "assignee": 5,
    "due_date": "2026-06-01",
    "priority": 2,
    "size": 5,
    "labels_add": [10, 11],
    "labels_remove": [12],
    "project": 7,
    "parent": null
  }
}
```

Request body for `DELETE`:

```json
{ "ids": [101, 102, 103] }
```

### Semantics

- **Only keys present in `updates` are applied.** Absent key = no change. Explicit `null` = clear/unassign (where the field is nullable).
- **Labels are add/remove only**, not replace. `labels_add` and `labels_remove` operate on the existing set; both lists may be empty or omitted independently. The single-task `PATCH /api/tasks/{id}/` continues to support full `labels: [...]` replacement.
- **`parent: null`** detaches a subtask from its parent (becomes top-level in the same project).
- **`project` field** moves tasks across projects within the same workspace (see "Cross-project moves" below).

### Transactionality — all-or-nothing

The entire batch runs in a single database transaction. If any task fails permission or validation:

- HTTP 400 (validation) or 403 (permission) is returned.
- The transaction is rolled back; no task is modified.
- Validation failures return a structured `errors` body: `{ "errors": [{ "id": 101, "field": "status", "reason": "..." }, ...] }`.

No partial success mode in MVP.

### Permissions

- All `ids` must be accessible to `request.user` — i.e. each task's project must belong to a workspace where the user has a `WorkspaceMember` row.
- Implementation: `Task.objects.filter(id__in=ids, project__workspace__members__user=request.user).count() == len(set(ids))`. If the count doesn't match, return **HTTP 403 with a generic message** — no detail on which ids failed. This avoids leaking existence of tasks in workspaces the user can't see.
- Member-level role is sufficient for all bulk operations; admins are not required (consistent with per-task permissions in [0010](0010-permissions.md)).

### Cross-project moves

- A bulk `PATCH` with `updates.project` set moves all referenced tasks into the target project.
- Source tasks may come from **multiple projects** in the same workspace; target must be **one** project.
- Target project must be in the same workspace as every source task. Cross-workspace bulk moves are rejected with HTTP 400. Cross-workspace move is a deliberately separate, non-bulk operation (not in MVP scope).
- Each moved task receives a **new `number`** from the target project's `next_task_number` counter. The target counter is incremented atomically per move.
- Old slug references (`HRW-49`) embedded in comments, descriptions, and prior activity log entries are not rewritten — they remain as historical references. The `task.updated` event records `{changes: {project: {old, new}, number: {old, new}}}` so the new identity is discoverable.

### Subtask cascade on move

- Invariant: `subtask.project == subtask.parent.project`. Moving a parent must move its subtasks.
- When a parent task appears in `ids` and is moved to a new project, all its subtasks are moved with it in the same transaction.
- All cascaded subtask events share the same `bulk_id` as the parent's events.
- If both a parent and one of its subtasks appear in `ids`, the subtask is processed once (via the parent cascade); the explicit entry in `ids` is a no-op duplicate.
- If a subtask alone appears in `ids` (no parent), it is moved independently, but its `parent` link is automatically cleared because the parent is still in the old project — invariant kept. The event records `{changes: {parent: {old, new: null}, project: {old, new}}}`.

### Limits

- **Maximum 500 ids per request.** Hard limit; requests exceeding it return HTTP 400 with `{"error": "batch too large", "limit": 500}`.
- Clients are expected to chunk larger selections client-side and present a progress indicator.

### Activity log integration

Per [0011](0011-activity-log.md):

- Each bulk call generates one `bulk_id = uuid4()`.
- Each changed field on each affected task produces its own `ActivityLog` row (using the granular event_type set: `task.status_changed`, `task.assigned`, `task.due_changed`, `task.priority_changed`, `task.labels_changed`, `task.parent_changed`, `task.updated`, `task.deleted`).
- All rows from one bulk call carry the same `bulk_id`.
- UI uses `bulk_id` to collapse workspace-feed entries ("Vox moved 12 tasks to In Review") while keeping per-task timelines intact.

### Concurrency and idempotency

- No idempotency keys in MVP. Replaying the same `PATCH` is naturally idempotent for non-label, non-counter changes; replaying a `DELETE` will return 403/404 for already-deleted ids (and abort the whole batch — by design).
- Last-write-wins for concurrent edits. No `If-Match` / ETag. Acceptable for a ~10-person team; revisit if conflicts become noticeable.

## Why

- **One universal endpoint** matches how the operation is actually used: "do several things to many tasks at once." Per-operation endpoints would force callers to chain requests for combined intents (`set status + reassign`), losing atomicity and forcing the UI to invent transaction-emulation logic.
- **All-or-nothing transactionality** keeps error handling sane. Partial success requires both server and client to reason about half-applied state, and the activity log would record real changes for tasks that the user might have wanted to undo.
- **Add/remove for labels** matches the realistic bulk use case ("tag these 20 tasks as `blocked`"); replace semantics would require the UI to first read each task's current labels — defeating the bulk efficiency.
- **403 without detail** is the secure default. The downside (worse error messaging) is small for an internal tool; if it becomes a debugging headache, an admin-only diagnostic endpoint can be added.
- **500 hard limit** prevents a runaway client from locking up the DB on a 50k-row update transaction. 500 is generous for any realistic UI selection.
- **Cross-project, single-workspace** preserves the workspace as the security boundary while enabling the most common power-user move (consolidating tasks from multiple sub-projects into one project).

## Consequences

- Single endpoint has more validation logic in one place. Mitigated by clear per-field validators and an integration test for each combination matrix entry.
- Bulk move across projects generates new slugs — old `HRW-49` references in comments become "dangling" in the sense that the canonical task is now `FOO-12`. Acceptable for MVP; a future enhancement could auto-link old slugs to the renamed task via the activity log trail.
- 500 limit means the UI must surface "selection too large" UX. A toast with "split into batches" is sufficient.
- Without idempotency keys, a flaky network that double-submits a `DELETE` will fail the second attempt — predictable, but worth a UI toast pattern that suppresses double-clicks.

## Open Questions

- Whether to support bulk **archive** at the project level (`PATCH /api/projects/bulk/`). Out of MVP; revisit if needed.
- Whether `updates.labels_set` (full replace, bulk) is worth adding later for "make these tasks have exactly these labels" use case. Deferred — no concrete need yet.
- Behavior when the workspace's `next_task_number` counter is rapidly incremented by concurrent bulk moves — covered by `SELECT FOR UPDATE` on the target project row (already required for single-task creation per [0007](0007-data-model-task-project.md)).
