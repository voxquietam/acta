# Acta — Documentation

Map of decisions and specs. Each decision lives in its own ADR under `decisions/`.
ADR format: **Context → Options → Decision → Why → Consequences**.

## Decisions (ADRs)

| #    | Status   | Topic                                                  | File                                              |
|------|----------|--------------------------------------------------------|---------------------------------------------------|
| 0001 | accepted | Stack: Django + DRF, Postgres, HTML/Tailwind/vanilla   | [0001-stack.md](decisions/0001-stack.md)          |
| 0002 | accepted | Auth: Google OAuth via django-allauth                  | [0002-auth.md](decisions/0002-auth.md)            |
| 0003 | accepted | Hierarchy: Workspace → Project → Task → Subtask        | [0003-hierarchy.md](decisions/0003-hierarchy.md)  |
| 0004 | accepted | Task statuses: fixed set of 5, CharField in DB         | [0004-statuses.md](decisions/0004-statuses.md)    |
| 0005 | accepted | Search: ILIKE + field filters; FTS later               | [0005-search.md](decisions/0005-search.md)        |
| 0006 | accepted | MVP scope: what's in, what's out                       | [0006-mvp-scope.md](decisions/0006-mvp-scope.md)  |
| 0007 | accepted | Data model: Task and Project fields, numbering, delete | [0007-data-model-task-project.md](decisions/0007-data-model-task-project.md) |
| 0008 | accepted | Labels with optional groups (Linear-style)             | [0008-labels.md](decisions/0008-labels.md)        |
| 0009 | accepted | Project Updates (Linear-style status posts)            | [0009-project-updates.md](decisions/0009-project-updates.md) |

## Spec (filled in as decisions land)

- `spec/data-model.md` — models, fields, foreign keys
- `spec/api.md` — endpoints and payload formats
- `spec/activity-log.md` — event schema
- `spec/bulk-operations.md` — bulk endpoints, contracts

## Open Questions

Things still to discuss live in [open-questions.md](open-questions.md).
