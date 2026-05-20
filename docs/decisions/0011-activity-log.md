# ADR 0011: Activity Log

**Status:** accepted
**Date:** 2026-05-15

## Context

Acta exists in part because Kaneo's activity log lies: the recorded `actor` is the task's assignee, not the user who actually performed the action. Webhooks repeat the same lie for every event type except comments. The history is therefore untrustworthy and breaks "who changed this?" investigations.

Acta needs an activity log that is:

- **Honest** about who acted.
- **Granular enough** to filter by event type (e.g. "show me only status changes for this project this week").
- **Atomic** with the change itself — if the task save succeeds, the event must be recorded; if it fails, no event.
- **Cheap to write** (no post-hoc reconciliation, no fuzzy-dedup, no lookup-to-fix-the-actor at read time).

## Decisions

### Storage model — single table + JSONB

```
ActivityLog
  workspace      FK(Workspace)
  project        FK(Project, null=True)
  target_type    CharField              # 'task' | 'comment' | 'project' | 'workspace' | 'member'
  target_id      PositiveBigIntegerField
  actor          FK(User, null=True)    # null = system-originated event
  event_type     CharField              # see naming convention
  payload        JSONField              # event-specific details (diff, denormalized snapshots)
  bulk_id        UUIDField, null=True   # shared across rows that come from one bulk operation
  created_at     DateTimeField(auto_now_add=True, db_index=True)

  Meta:
    indexes = [
      Index(fields=['workspace', '-created_at']),
      Index(fields=['target_type', 'target_id', '-created_at']),
      Index(fields=['bulk_id']) where bulk_id is not null,
    ]
```

One table for all event types. No per-event-type subclasses — the cost of a unified feed query outweighs the schema clarity of separate tables.

### Actor rule (the headline anti-Kaneo fix)

- `actor` is **always** the authenticated user from the inbound HTTP request — never the assignee, never the task reporter, never derived from the payload.
- For system-initiated events (data migrations, scheduled jobs), `actor` is `null` and the payload includes `source: "system"` plus a free-form `reason`.

### Event type list (MVP)

Convention: `{target_type}.{verb_or_field_changed}`. Verbs are past tense.

**Task events:**

| event_type              | When                                                  | payload                                                            |
|-------------------------|-------------------------------------------------------|--------------------------------------------------------------------|
| `task.created`          | Task creation                                         | `{title, project_id, parent_id}`                                   |
| `task.status_changed`   | `status` field change                                 | `{from, to}`                                                       |
| `task.assigned`         | `assignee` field change (including unassign)          | `{from_user_id, to_user_id}`                                       |
| `task.due_changed`      | `due_date` field change                               | `{from, to}` (ISO dates or null)                                   |
| `task.priority_changed` | `priority` field change                               | `{from, to}` (int values)                                          |
| `task.labels_changed`   | M2M change on `labels`                                | `{added: [{id, name, group}], removed: [...]}`                     |
| `task.parent_changed`   | `parent` field change                                 | `{from_task_id, to_task_id}`                                       |
| `task.updated`          | Catch-all for other field edits (title, description, size) | `{changes: {field: {old, new}, ...}}`                          |
| `task.deleted`          | Task hard-delete                                      | `{title, project_id, snapshot: {…minimal fields…}}`                |

**Comment events:** `comment.created`, `comment.edited`, `comment.deleted`. Payload includes `task_id` and a short `body_preview` for `created`.

> **Amendment (2026-05-20):** the `Comment` model is now polymorphic — a
> comment targets *either* a task *or* a project update, with one-level
> replies via a `parent` self-FK (see [0022](0022-polymorphic-comments.md)).
> The `comment.*` events here fire **only for task comments**.
> Comments and replies on a *project update* are deliberately **not** written
> to the activity log — the same exclusion already applied to `project_update.*`
> events (see [0009](0009-project-updates.md) and below): the update thread is
> its own audit trail and the activity feed stays task-focused. The DRF
> `CommentViewSet` that calls `log_event` is task-only by design, so the
> "log a comment event" path is never reached for an update comment.

**Workspace member events:** `member.added`, `member.removed`, `member.role_changed`. Payload includes the affected `user_id`, role(s), and the change shape.

`project_update.*` events are **not** written to the activity log — see [0009](0009-project-updates.md). The updates themselves *are* the audit trail for that surface.

### Implementation — explicit `log_event()`, no signals

```python
def log_event(*, workspace, actor, event_type, target_type, target_id,
              payload=None, project=None, bulk_id=None):
    return ActivityLog.objects.create(
        workspace=workspace, actor=actor, event_type=event_type,
        target_type=target_type, target_id=target_id,
        payload=payload or {}, project=project, bulk_id=bulk_id,
    )
```

Called from:

- DRF `ModelViewSet.perform_create / perform_update / perform_destroy` overrides.
- Bulk endpoints (one `log_event` call per affected task, all sharing a single `bulk_id = uuid4()`).
- Management commands and admin actions (passing `actor` explicitly, or `None` for system events).

**No `post_save` / `pre_save` signals are used to write events.** Signals don't carry request context, force threadlocal hacks, fire on internal saves (tests, migrations, data fixes) — and the entire reason Acta exists is that Kaneo's implicit actor inference was wrong. Explicit > implicit here.

### Bulk operations

All `ActivityLog` rows from one bulk endpoint call share a single `bulk_id` (UUID). UI consumes this:

- **Task timeline:** the row appears as a regular event ("Vox changed status to In Review").
- **Workspace feed:** rows with the same `bulk_id` are collapsed into one entry ("Vox moved 12 tasks to In Review").

### UI surfaces (MVP)

- **Task timeline** — list of events for one task, newest-first (sorted by `created_at`). Comments are interleaved.
- **Workspace feed** — `/activity/` page, newest-first. Bulk events collapsed by `bulk_id`. Filters: actor, event_type, project, date range.

### Anti-Kaneo rules

Lessons baked in from observed Kaneo quirks (see Vox's notes on the parallel `ksu24.back` project):

1. **Actor is the request user, never derived from payload state.** No post-hoc `/activity/{task_id}` lookup to "find the real actor" — that's a Kaneo workaround we don't need because we record correctly on write.
2. **Naming convention is rigid:** `{owner_target_type}.{verb_or_field_changed}`. A comment-creation event is `comment.created`, not `task.comment_created`. Owner of the event is the entity whose lifecycle the event describes.
3. **Sort by `created_at` everywhere.** Never sort by primary key (`id`) for time-ordered display, even with `BigAutoField`. Mirrors the Kaneo MongoDB ObjectID lesson: future storage changes shouldn't break ordering.
4. **Write the event in the same transaction as the change.** If the model save rolls back, the event must roll back too. No "eventual consistency" between activity log and underlying state.
5. **No fuzzy dedup at read time.** If we ever see duplicate events, fix the writer, not the reader. Activity log is the source of truth, not a noisy stream to be cleaned up.
6. **Granular events for watched fields.** `status`, `assignee`, `due_date`, `priority`, and `labels` each get their own event type — these are the fields users alert on and filter by. Lumping them into a generic `task.updated` loses signal.
7. **Webhook design (post-MVP) will mirror this event_type set directly.** No translation layer, no separate event names. What we write to the log is what we emit on the wire.

## Why

- **Activity log is the killer trust feature** — the whole point of moving off Kaneo is recording the right actor. Investing in correctness here pays back every time someone asks "wait, who actually did this?"
- **Single table + JSONB** scales to ~all event types without forcing schema changes when a new event is added. Postgres JSONB queries on `payload` are fast enough at MVP volumes.
- **Explicit `log_event()`** keeps the call site obvious: code reviewers can see "where do we log this?" by grepping. Signal-based logging tends to drift out of sync silently.
- **Granular event types** make filtering and digests (post-MVP) trivial — one `WHERE event_type IN (...)` clause.
- **`bulk_id` grouping** preserves both detail (per-task history) and ergonomics (feed not flooded).

## Consequences

- Forgetting to call `log_event()` from a new endpoint is silent. Mitigation: a base `ActivityLoggingViewSet` mixin with `perform_*` overrides that the developer can extend; unit-test coverage that asserts events are written.
- Free-text edits to title/description/size are merged into `task.updated`. If we later want per-field watched alerts on description specifically, that's a new event type — backwards-compatible addition.
- Activity log will grow large over time. Retention/archival policy is out of MVP; revisit at ~1M rows.
- Hard delete of a task removes the task row but the `task.deleted` event remains in `ActivityLog` with a denormalized `snapshot` so the history is still readable.

## Open Questions

- Should `actor` be denormalized as `actor_email` / `actor_name` in payload, so the timeline stays readable if a user is later deleted? Lean **yes** for resilience — to confirm in `spec/activity-log.md`.
- Real-time push of new events to open clients (SSE/WebSocket) — out of MVP, but the table shape is ready for it (just `WHERE workspace=? AND created_at > ?`).
- Whether `task.parent_changed` is worth its own event vs being a row in `task.updated` — leaning toward separate for clarity in subtask threads; revisit during implementation.
