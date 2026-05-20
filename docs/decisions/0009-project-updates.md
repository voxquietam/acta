# ADR 0009: Project Updates

**Status:** accepted
**Date:** 2026-05-15

## Context

Activity log (written via explicit `log_event()` calls, **not** Django signals — see [0011](0011-activity-log.md)) captures *what happened* at the task level — status changes, comments, assignments. It does not surface *how the project is going overall*. For that, Linear has "Project Updates": short, manually-written status posts with a health indicator.

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

> **Amendment (2026-05-20):** the REST shape sketched below was the original
> plan. Updates are now **composed in-app** from the project overview via the
> web view `post_project_update` (`apps/web/views.py`), which replaced the
> earlier "go to the admin to post an update" link. The DRF endpoints below
> are not the live surface; treat this list as historical intent, not the
> current API.

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

## Amendment (2026-05-20): notifications, Inbox tab, and comment threads

Project updates have grown past the "post + read on the project page" shape:

- **They notify the whole workspace.** Creating an update calls
  `notify_project_update_created` (`apps/notifications/services.py`), which
  fans a `PROJECT_UPDATE` notification out to *every* member of the update's
  workspace (author dropped by `notify()`'s self-suppression), delivered over
  the per-user SSE channel (see [0015](0015-real-time.md) Stream-topology
  amendment and [0021](0021-notification-inbox.md)).
- **They feed the Inbox Updates tab.** The persistent inbox ([0021](0021-notification-inbox.md))
  surfaces updates in a dedicated Updates tab — the same audience that gets
  the notification.
- **They carry comments + one-level replies.** A `ProjectUpdate` is now one of
  the two targets of the polymorphic `Comment` model (see
  [0022](0022-polymorphic-comments.md)). Update comments and their single level
  of replies are posted from the web view (`post_update_comment`). Consistent
  with the "activity stream stays task-focused" rule above, **update comments
  do not log activity events** (unlike task comments — see [0011](0011-activity-log.md)).
