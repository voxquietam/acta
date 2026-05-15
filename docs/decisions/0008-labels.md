# ADR 0008: Labels and Label Groups

**Status:** accepted
**Date:** 2026-05-15

## Context

Tasks need labels for cross-cutting tags (bug, frontend, urgent, refactor, p0, …). Three shapes were considered: workspace-scoped flat labels, project-scoped flat labels, and Linear-style label groups (categories that contain labels, optionally mutually exclusive).

For a single-team setup, recreating common labels ("bug", "frontend") inside every project is annoying. Grouping (`Type → bug/feature/refactor`, `Priority area → P0/P1/P2`) adds structure that scales with the number of labels.

## Decision

- Labels live at **workspace** scope. One label can be attached to any task in any project of that workspace.
- Labels can optionally belong to a **LabelGroup** (also workspace-scoped). Labels without a group are "ungrouped" and behave as flat tags.
- A LabelGroup can be marked **exclusive**: at most one label from that group can be attached to a single task. Non-exclusive groups allow multiple labels from the group on one task.
- No project-local labels in MVP.

### Models

```
LabelGroup
  workspace        FK(Workspace)
  name             CharField              # e.g. "Type", "Priority"
  is_exclusive     BooleanField           # one-of vs many-of
  created_at

Label
  workspace        FK(Workspace)
  group            FK(LabelGroup, null=True)
  name             CharField
  color            CharField              # hex like #FF8800
  created_at

  Meta: unique_together = (workspace, name)
```

`Task.labels` is `M2M(Label)`. Exclusivity is enforced at serializer level: when attaching labels, validate that no two of them belong to the same exclusive group.

## Why

- **Workspace scope** prevents duplicating "bug"/"frontend"/"urgent" in every project. UI can still filter the picker to "labels actually used in this project" if the list gets long.
- **Groups (Linear-style)** scale with label count and make UI navigation faster (collapsible categories).
- **Exclusive groups** model real semantics: a task has exactly one "Type" (bug XOR feature XOR refactor), but can have many "Areas" (frontend AND backend).
- **No project-local labels in MVP:** the use case (KSU24, one team, ~10 people) doesn't need that isolation. Adding it later means one nullable `project` FK on Label and a queryset adjustment — no destructive migration.

## Consequences

- Label picker UI needs grouping support from day one (collapse/expand by group).
- Serializer validation on `Task.labels` is non-trivial: must batch-check exclusivity per group on every label change. Bulk operations need to apply the same validation.
- Activity log entries for label changes should record `label_id`, label name, and group name (denormalized in JSONB) so history reads sensibly even if a label is later renamed or deleted.
- A label deletion cascades to remove it from `Task.labels` via M2M (default Django behavior); activity log retains the historical name in its payload.

## Open Questions

- Should label colors be free-form hex or a fixed palette? Probably fixed palette in UI to avoid eyesore colors; backend stores hex string regardless. Final call deferred to frontend ADR.
- Default LabelGroups seeded per new workspace? E.g. seed "Type" with bug/feature/refactor. Decided no for MVP — keep it manual.
