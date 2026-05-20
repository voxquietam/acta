# ADR 0022: Polymorphic Comment model

**Status:** accepted
**Date:** 2026-05-20

## Context

Comments started life attached to tasks only ([0006](0006-mvp-scope.md),
[0009](0009-project-updates.md)): a `Comment` had a single `task` FK, and the
whole stack — the activity log, the notification fan-out, the DRF
`CommentViewSet`, the MCP tools — assumed `comment.task` was always there and
walked `comment.task.project.workspace` freely.

Project Updates ([0009](0009-project-updates.md)) then grew a discussion need:
people want to reply under an update ("nice, what's the ETA?"), with one level
of threaded replies — the same Linear shape. The question was how to attach a
comment thread to a *second* kind of target without breaking the ~5 call sites
that hard-assume a task comment.

## Decision

**One `Comment` model targets *either* a task *or* a project update**, never
both, never neither. Two nullable FKs plus a DB check constraint:

- `task` → `tasks.Task`, nullable.
- `project_update` → `projects.ProjectUpdate`, nullable.
- `parent` → `self`, nullable — a one-level reply chain (`related_name="replies"`).
- A `CheckConstraint` named **`comment_exactly_one_target`** enforces *exactly
  one* of `task` / `project_update` is set:

  ```python
  Q(task__isnull=False, project_update__isnull=True)
  | Q(task__isnull=True, project_update__isnull=False)
  ```

**Depth-1 replies.** A reply may not have its own replies, and a reply must
share its parent's target. Enforced in two places:

- `Comment.clean()` raises `ValidationError` if `parent.parent_id is not None`
  (a reply-to-a-reply) or if the parent's target differs from the child's.
- The web view that posts update replies (`post_update_comment`) only accepts a
  `parent` that is itself a top-level comment on the same update
  (`Comment.objects.filter(project_update=update, parent__isnull=True, pk=...)`),
  so the depth limit holds at the request boundary, not just in `clean()`.

**The asymmetry — task comments log + notify, update comments don't.** This is
deliberate, not an oversight:

| | Activity log (`comment.*`) | Notifications |
|---|---|---|
| **Task comment** | yes (`comment.created/edited/deleted`, see [0011](0011-activity-log.md)) | yes — assignee + reporter + mentions (`notify_comment_created`) |
| **Update comment / reply** | **no** | **no** (the parent update already notified the whole workspace) |

Update comments stay off the activity log for the same reason
`project_update.*` events do (see [0009](0009-project-updates.md) and
[0011](0011-activity-log.md)): the activity feed is the *task* audit trail, and
update threads are their own surface. The mechanism is simply that the only
code paths that call `log_event` for a comment (`CommentViewSet`,
`post_comment`) are task-scoped and never run for an update comment.

**The DRF `CommentViewSet` stays task-only by design.** Its queryset filters on
`task__project__workspace__memberships`, its `filterset_fields` is `["task"]`,
and its `perform_*` hooks walk `comment.task.project`. Update comments are
created only through the in-app web composer, never the REST API. This keeps the
task-coupled write hooks valid without a `None`-check on every `.task` access.

## Why this over the alternatives

- **A second model (`UpdateComment`)** — rejected. Vox explicitly did not want
  a parallel model. It would duplicate the body / author / threading / markdown
  fields and force two of everything (serializers, templates, render paths) for
  what is the same "a person wrote a markdown note under X" concept.
- **Django `contenttypes` (GenericForeignKey)** — rejected. It is the textbook
  "polymorphic FK" answer, but it would have forced rewriting roughly five
  task-coupled call sites that read `comment.task` directly — the activity log
  writer, the notification fan-out (`notify_comment_created`), the DRF viewset
  (queryset + `select_related` + write hooks), and the MCP comment tools.
  `GenericForeignKey` also loses the DB-level "exactly one target" guarantee, is
  awkward to `select_related`, and complicates the admin. Two explicit nullable
  FKs + a `CheckConstraint` keep referential integrity, keep `select_related`
  trivial (`task__project`, `project_update__project`), and let the existing
  task call sites stay literally unchanged.

## Consequences

- The "exactly one target" invariant is guaranteed at the DB level
  (`comment_exactly_one_target`), not just in application code — a stray write
  that sets both or neither is rejected by Postgres.
- Code that reads `comment.task` must tolerate `None` when it can see update
  comments (e.g. inbox rendering joins both `task__project` and
  `project_update__project`). The task-only paths (DRF, activity, MCP) sidestep
  this by never handling update comments.
- Adding a *third* target later (e.g. a comment on a document) means one more
  nullable FK and widening the check constraint — a backwards-compatible
  migration, no model split.
- Threading is capped at depth 1 by both `clean()` and the view. Deeper threads
  would need an explicit decision (they were not wanted).
