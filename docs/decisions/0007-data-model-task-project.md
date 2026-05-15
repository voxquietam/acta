# ADR 0007: Data Model — Task and Project

**Status:** accepted
**Date:** 2026-05-15

## Context

Before scaffolding Django apps, the core data shape of `Task` and `Project` has to be locked in: types of priority/size, assignment cardinality, slug rules, counter behavior, soft vs hard delete, subtask inheritance.

## Decisions

### Task fields

| Field         | Type                          | Notes                                                                 |
|---------------|-------------------------------|-----------------------------------------------------------------------|
| `title`       | `CharField`                   | Required, reasonable max_length (e.g. 200).                           |
| `description` | `TextField`, blank=True       | Markdown source. Rendering decided in a future frontend ADR.          |
| `status`      | `CharField`                   | Fixed enum (see [0004](0004-statuses.md)).                            |
| `priority`    | `SmallIntegerField`           | Linear-style 5 values: `0=no_priority, 1=urgent, 2=high, 3=medium, 4=low`. Stored as int for sortability. |
| `size`        | `SmallIntegerField`, null     | **Story points**, restricted to Fibonacci set `{1, 2, 3, 5, 8, 13}`. Validated in serializer. |
| `due_date`    | `DateField`, null             | Date only — no time, no timezone.                                     |
| `assignee`    | `FK(User)`, null              | Single assignee. No multi-assign in MVP.                              |
| `reporter`    | `FK(User)`                    | Auto-set from `request.user` on create. Immutable after creation.     |
| `labels`      | `M2M(Label)`                  | See [0008-labels.md](0008-labels.md).                                 |
| `parent`      | `FK('self')`, null            | Subtask link. Depth limited to one level (enforced in serializer).    |
| `project`     | `FK(Project)`                 | Required.                                                             |
| `number`      | `PositiveIntegerField`        | Per-project monotonic counter. See "Numbering" below.                 |
| `created_at`  | `auto_now_add`                |                                                                       |
| `updated_at`  | `auto_now`                    |                                                                       |

`(project, number)` is the natural unique identifier; user-facing ID is `{project.slug_prefix}-{number}` (e.g. `HRW-49`).

### Project fields

| Field               | Type                          | Notes                                                                |
|---------------------|-------------------------------|----------------------------------------------------------------------|
| `workspace`         | `FK(Workspace)`               | Required.                                                            |
| `name`              | `CharField`                   |                                                                      |
| `description`       | `TextField`, blank=True       | Markdown.                                                            |
| `slug_prefix`       | `CharField`                   | 2–6 uppercase Latin letters. Unique within workspace. **Immutable.** |
| `next_task_number`  | `PositiveIntegerField`        | Default 1. Incremented in a DB transaction on task creation.         |
| `archived`          | `BooleanField`                | Default False.                                                       |
| `created_at`        | `auto_now_add`                |                                                                      |

### Numbering rules

- `Project.next_task_number` increments atomically per task creation. Implementation: `SELECT ... FOR UPDATE` on the project row, then assign and increment, then commit.
- Numbers are **monotonic and never reused**, even if a task is deleted.
- Subtasks share the same numbering space as top-level tasks within a project (parent `HRW-49`, subtasks `HRW-50`, `HRW-51`, …).

### Subtask behavior

- A subtask inherits **only** `project` from its parent.
- All other fields (`assignee`, `labels`, `status`, `priority`, `size`, `due_date`) are independent and must be set explicitly.
- Depth is limited to one level: a subtask cannot have its own subtasks. Enforced in the API/serializer, not at the DB level.

### Deletion

- **Hard delete.** No `is_deleted` flag, no soft delete.
- History is preserved by `ActivityLog`: a `deleted` event with the task's title, project, and key fields in the JSONB payload.

## Why

- **Story points (Fibonacci):** the team wants estimation; fixed Fibonacci set discourages bikeshedding over "is this a 6 or a 7" and matches industry convention.
- **Single assignee:** clear ownership per task; aligns with Linear; simpler UI and bulk operations. Multi-assign tends to dilute accountability.
- **Date without time:** task deadlines are rarely time-of-day-specific; skipping `DateTimeField` avoids timezone bugs.
- **Hard delete + activity log:** simpler queries everywhere (no `is_deleted=False` filter scattered through the codebase); audit trail still exists via activity log.
- **Immutable slug prefix:** changing `HRW` → `HOMEWORK` after the fact would break every `HRW-49` reference in comments, descriptions, external links.
- **Monotonic counter:** reusing freed numbers makes references ambiguous over time ("which `HRW-49` are we talking about?").
- **Subtask inheritance off by default:** explicit > implicit. Future UI can offer a "copy from parent" affordance.

## Consequences

- Need a serializer-level validator for `size ∈ {1,2,3,5,8,13}` and for subtask depth (`parent.parent is None`).
- Race condition on `next_task_number` requires transactional locking; without it two concurrent task creations could collide. To be implemented in the Task `.save()` / `perform_create` path.
- A migration to add story points / change priority encoding is fine because of the int-based storage; cosmetic names are decoupled in code.
- ActivityLog payload format must include enough Task data on `deleted` events to keep history readable — to be specified in the upcoming activity-log ADR.
