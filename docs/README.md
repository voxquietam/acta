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
| 0010 | accepted | Permissions: owner/admin/member + onboarding flow      | [0010-permissions.md](decisions/0010-permissions.md) |
| 0011 | accepted | Activity Log: explicit logging, honest actor, anti-Kaneo rules | [0011-activity-log.md](decisions/0011-activity-log.md) |
| 0012 | accepted | Bulk operations: single PATCH endpoint, all-or-nothing       | [0012-bulk-operations.md](decisions/0012-bulk-operations.md) |
| 0013 | accepted | API conventions: v1 prefix, snake_case, ISO 8601, session auth | [0013-api-conventions.md](decisions/0013-api-conventions.md) |
| 0014 | accepted | Frontend: server-rendered + HTMX + Alpine + Chart.js, no build step | [0014-frontend-architecture.md](decisions/0014-frontend-architecture.md) |
| 0015 | accepted | Real-time updates via SSE (`django-eventstream`), one stream per workspace | [0015-real-time.md](decisions/0015-real-time.md) |
| 0016 | accepted | Dashboards: live ORM queries, Chart.js, fixed time windows         | [0016-dashboards.md](decisions/0016-dashboards.md) |
| 0017 | accepted | Notifications: in-app toasts over SSE, no inbox in MVP             | [0017-notifications.md](decisions/0017-notifications.md) |
| 0018 | accepted | i18n: en + uk, User.language preference, LocaleMiddleware          | [0018-i18n.md](decisions/0018-i18n.md) |

## Spec (filled in as decisions land)

- `spec/project-layout.md` — directory tree, Django apps, settings/requirements split ✅
- `spec/data-model.md` — models, fields, foreign keys (TBD)
- `spec/api.md` — endpoints and payload formats (TBD)
- `spec/activity-log.md` — event schema details (TBD)
- `spec/bulk-operations.md` — bulk endpoints, edge cases (TBD)

## Open Questions

Things still to discuss live in [open-questions.md](open-questions.md).
