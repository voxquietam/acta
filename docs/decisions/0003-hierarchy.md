# ADR 0003: Hierarchy — Workspace, Project, Task, Subtask

**Status:** accepted
**Date:** 2026-05-15

## Context

Acta needs a multi-level structure to organize work. Kaneo uses Workspace → Project → Task. Linear adds an Initiative layer above Project for grouping and high-level descriptions. The team also asked for "sub-projects or epics" — a way to break large items into smaller pieces.

There are two distinct concepts that get conflated:

- **Sub-project:** a separate project nested inside a project, with its own slug, kanban, and members. Adds a hierarchy level.
- **Epic / parent task:** a *task* that owns child tasks (subtasks). One kanban, parent-child relationship inside the task model.

For a single-team tracker (KSU24, ~10 people), a sub-project layer is almost always overkill — the real need ("split a big thing into pieces") is solved by subtasks.

## Decision

MVP hierarchy:

```
Workspace ──▶ Project ──▶ Task ──▶ Subtask
```

- **Workspace:** top-level tenant. Members belong to a workspace. One Acta instance can host multiple workspaces.
- **Project:** lives inside a workspace. Has its own slug prefix (e.g. `HRW`) used for task references like `HRW-49`.
- **Task:** lives inside a project. Carries status, priority, size, assignee, labels, due date, description.
- **Subtask:** modeled as `Task.parent = ForeignKey('self', null=True)`. Shares the project's slug prefix; numbering is continuous (parent `HRW-49`, subtasks `HRW-50`, `HRW-51`, …).
- **Initiative:** *not* in MVP. Deferred — see Consequences.

## Why

- **Subtasks via self-FK** is the cheapest way to satisfy the "break work into pieces" need: one nullable field on `Task`, no new entity, one kanban per project, easy filters ("top-level only" or "expand subtasks under parent").
- **No sub-projects:** a sub-project layer would mean its own slug, kanban, members, and permission edges. None of that is justified by the current use case.
- **Workspaces from day one:** they're cheap to add now (one FK on Project, one M2M on User) and painful to retrofit later. Even with one workspace today, the model is ready for more.
- **Initiative deferred:** Linear-style initiatives are valuable for cross-project narratives, but they introduce a new entity with its own description, status, owner, and aggregation logic. Not worth the scope in a 3–4 week MVP.

## Consequences

- A single workspace will host all KSU24 projects on day one; the workspace selector UI can be minimal until a second workspace exists.
- Subtask depth: kept to a single level (`parent` → `child`); a child cannot have its own children. Enforced in the API layer, not at the DB level.
- Slug counter must increment across both tasks and subtasks within the same project, since they share the same numbering space.
- Adding Initiative later means a new model + nullable FK on Project (or M2M if a project may belong to several initiatives). No data migration needed for existing tasks.

## Open Questions

- Workspace permissions model (roles: owner/admin/member?) — see future `spec/permissions.md`.
- Whether subtasks inherit fields (assignee, labels) from parent by default — to be decided during data-model spec.
