# ADR 0009: Project Updates

**Status:** accepted
**Date:** 2026-05-15

## Context

Activity log (auto-tracked via Django signals) captures *what happened* at the task level — status changes, comments, assignments. It does not surface *how the project is going overall*. For that, Linear has "Project Updates": short, manually-written status posts with a health indicator.

Without project updates, the team has no canonical place to post "we're on track for the deadline" or "blocked on design feedback, slipping by a week". Activity log doesn't replace that — it's noise filtered for one task at a time.

## Decision

Introduce a `ProjectUpdate` model — a thread of manual, periodic status posts on a project.

### Model

```
ProjectUpdate
  project          FK(Project)
  author           FK(User)
  health           CharField              # enum: on_track | at_risk | off_track | completed
  body             TextField              # markdown
  created_at       auto_now_add
  updated_at       auto_now
```

### Behavior

- Any workspace member can create an update on a project they can access.
- Author can edit/delete their own updates; admins can delete any. Exact permissions: see future `spec/permissions.md`.
- The latest update's `health` is shown as the project's "current health" badge in the project list view.
- Updates are listed newest-first on the project page.
- Markdown rendering (server- or client-side) follows the same approach as task descriptions and comments — decided in the future frontend ADR.

### API surface

- `GET /api/projects/{id}/updates/` — list
- `POST /api/projects/{id}/updates/` — create
- `PATCH /api/project-updates/{id}/` — edit (author only)
- `DELETE /api/project-updates/{id}/` — delete (author or admin)

## Why

- **Cheap:** one new model, four endpoints, ~1 day of work. Doesn't bloat the MVP.
- **Fills a real gap:** activity log answers "what happened to task X", project updates answer "is this project on track". Different question, different audience.
- **Familiar pattern:** Linear's implementation is well-known and validated; no need to reinvent the shape.
- **Composable with future digest emails / Slack notifications** — out of MVP but the data model already supports it.

## Consequences

- Project list view needs a health badge — small frontend addition.
- Old updates accumulate forever; no archival policy in MVP. Acceptable at the team's volume.
- Activity log does *not* auto-track ProjectUpdate creation/edits in MVP — keeps the activity stream focused on task events. May revisit if it becomes confusing.
