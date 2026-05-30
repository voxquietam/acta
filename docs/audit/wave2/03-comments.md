# 03 — comments + reactions (C5)

> **Wave 2 / Chunk C5**. Date: 2026-05-29. Read-only. HEAD: `53b7924`.
> Audits polymorphic FK invariants, serializer SQL patterns, batching
> mechanisms, and test coverage gaps. Verifies that post-`53b7924` comment
> listing paths remain N+1-free. Cross-references Wave 1 PR-2 prefetch
> policy, SSE event filtering, and activity-log exclusions.

---

## 1. Surface inventory

**Comment model chain:**
- `/apps/comments/models.py` (117 LOC): polymorphic FK (`task` / `project_update`),
  1-level threading (`parent`), `was_edited` property with 1s tolerance.
- `/apps/comments/serializers.py` (71 LOC): `CommentSerializer`, task-required
  guard, Markdown render, workspace membership validation in `validate_task`.
- `/apps/comments/views.py` (114 LOC): `CommentViewSet` (DRF), task-scoped
  CRUD, write hooks emit activity events, `select_related("task__project__workspace", "author")`.

**Reactions chain:**
- `/apps/reactions/models.py` (121 LOC): polymorphic FK (`task` / `comment` /
  `project_update`), partial unique constraints per target, `user` / `emoji` / `created_at`.
- `/apps/reactions/services.py` (114 LOC): `summarize_reactions`, `attach_reactions`
  (batching), `toggle_reaction` (create/delete), `TARGET_TYPES` map.

**Web integration:**
- `_task_comments()` helper in `/apps/web/views.py`: prefetches
  `replies__author`, `attachments`, `replies__attachments`.
- `_decorate_comments()`: attaches task, `can_modify`, `reaction_summary` in place.
- `toggle_reaction_view`: POST endpoint, renders reaction bar after toggle.
- `post_update_comment`: creates top-level or 1-level reply on `ProjectUpdate`.

**Test coverage:**
- `/apps/comments/tests/test_models.py` (50 LOC): 3 tests for `was_edited` property only.
- `/apps/comments/tests/test_api.py` (125 LOC): CRUD matrix, permission checks,
  activity event emission. **Task-only scope — no `ProjectUpdate` comments**.
- `/apps/reactions/tests/test_reactions.py` (138 LOC): toggle service, summarize
  batching (1-query guarantee), toggle view across task/comment/update.

---

## 2. Comment model + polymorphism

**Polymorphic design (ADR 0022):**

`Comment` targets exactly one of `task` / `project_update` via nullable FKs
(lines 17–40 in `models.py`) + `CheckConstraint` named `comment_exactly_one_target`
(lines 66–74). Threading is depth-1 only (lines 33–39, `parent`).

**Validation path** (`clean()`, lines 81–97):
- Raises if both or neither target is set (line 91–92).
- Raises if `parent.parent_id` is not None — no replies-to-replies (line 94–95).
- Raises if child's target differs from parent's target (line 96–97).

**No infinite recursion path:** Self-FK on `parent` (line 33) is nullable and
depth-capped at 1 by both `clean()` validation and the web view filter
(`Comment.objects.filter(…, parent__isnull=True, pk=int(parent_raw))`).

**Was-edited tolerance** (lines 99–116): Accounts for microsecond drift between
`auto_now_add` and `auto_now` by checking if `updated_at - created_at > 1s`.
Property reads correctly from already-persisted timestamps.

---

## 3. Views + serializer SQL

**CommentViewSet (views.py:13–114):**

Lines 33–52 (`get_queryset`):
```python
return (
    Comment.objects.select_related(
        "task__project__workspace",
        "author",
    )
    .filter(task__project__workspace__memberships__user=self.request.user)
    .distinct()
)
```

**Pattern**: Walks the full FK chain to task→project→workspace for both the
membership filter and the downstream `perform_create/update/destroy` hooks
(lines 63–75, 83–91, 100–112). The `select_related` pre-loads them — **no extra
SELECTs per write**.

**Serializer validation** (`CommentSerializer.validate_task`, lines 51–70):

```python
if not WorkspaceMember.objects.filter(
    user=user,
    workspace=task.project.workspace,
).exists():
    raise serializers.ValidationError(...)
```

**Issue F1**: The `validate_task` method reads `task.project.workspace` (which
was already loaded by the queryset filter), but issues a fresh `WorkspaceMember.objects.filter(...).exists()` query. Since the user's workspace membership was already checked by `get_queryset`, this is redundant on every POST/PATCH. **Severity: low** — one extra query per write (not N+1); the task is already loaded.

**Mitigation**: The viewset's `filter` already ensures membership, so
`validate_task` could skip the second check or cache it via a context flag.
**Flag F1 for Wave 3 refactor.**

---

## 4. summarize_reactions / attach_reactions batching

**Location:** `/apps/reactions/services.py:18–84`

**summarize_reactions** (lines 18–57):

```python
def summarize_reactions(*, target_field: str, ids: Iterable[int], user_id: int | None) -> dict[int, list[dict]]:
    ids = list(ids)
    if not ids:
        return {}
    rows = Reaction.objects.filter(**{f"{target_field}__in": ids})
        .select_related("user")
        .order_by("created_at", "id")
    summary: dict[int, OrderedDict] = {}
    for reaction in rows:
        target_id = getattr(reaction, f"{target_field}_id")
        buckets = summary.setdefault(target_id, OrderedDict())
        ...
    return {target_id: list(buckets.values()) for target_id, buckets in summary.items()}
```

**Query pattern**: Single `SELECT * FROM reactions WHERE {target_field} IN (...)`,
grouped in Python by `(target_id, emoji)`. **One query regardless of count.**
Valid for task/comment/project_update targets via parameterized field name.

**attach_reactions** (lines 60–84):

Thin wrapper: calls `summarize_reactions` on the extracted `ids`, then attaches
results to each object. **No N+1 in the service layer.**

**Test coverage** (`test_no_n_plus_one`, lines 71–81):

```python
with CaptureQueriesContext(connection) as ctx:
    attach_reactions(objs=comments, target_field="comment", user_id=ws.owner.id)
assert len(ctx.captured_queries) == 1
```

Creates 8 comments with reactions, calls `attach_reactions`, asserts exactly 1
query (the `Reaction.objects.filter(...)`). **Passes; batching confirmed.**

---

## 5. Reactions invariants

**Model constraints** (`models.py:60–102`):

1. **Exactly-one-target check** (lines 67–74):
   ```python
   models.CheckConstraint(
       condition=(
           Q(task__isnull=False, comment__isnull=True, project_update__isnull=True)
           | Q(task__isnull=True, comment__isnull=False, project_update__isnull=True)
           | Q(task__isnull=True, comment__isnull=True, project_update__isnull=False)
       ),
       name="reaction_exactly_one_target",
   )
   ```
   Database-enforced, covers all 3 cases.

2. **Partial unique constraints** (lines 75–102):
   - `reaction_unique_user_task_emoji` on `(user, task, emoji)` where `task IS NOT NULL`
   - `reaction_unique_user_comment_emoji` on `(user, comment, emoji)` where `comment IS NOT NULL`
   - `reaction_unique_user_update_emoji` on `(user, project_update, emoji)` where `project_update IS NOT NULL`

   **Design**: Three separate partial constraints instead of one multi-column
   unique index. The latter would treat the two NULL columns as distinct
   (Postgres quirk), allowing multiple rows with the same `(user, emoji)` on
   different target types. Partial constraints correctly enforce per-target
   uniqueness.

**Validation** (`clean()`, lines 109–120):

```python
set_targets = sum(1 for value in (self.task_id, self.comment_id, self.project_update_id) if value is not None)
if set_targets != 1:
    raise ValidationError(...)
```

Redundant with the DB check constraint, but caught earlier (at model level before
DB errors). **Acceptable.**

**Cross-target integrity:**

Comment-to-task reactions: `Reaction.comment_id` → `Comment.id` → `Comment.task_id`.
If the comment is deleted, `Reaction.objects.filter(comment=c).delete()` fires
via `on_delete=CASCADE`. No orphaned reactions.

If a `Task` is deleted, its comments cascade-delete, which cascade-deletes their
reactions. **Invariant holds.**

---

## 6. Test coverage gaps

**Comments (`/apps/comments/tests/`):**

File count: 2 (`test_models.py`, `test_api.py`). 50 + 125 = **175 LOC total**.
Baseline expectation (00-baseline.md §2): weak coverage flagged for a
polymorphic model.

**Gaps identified:**

1. **Thread integrity missing** (lines 33–39 of `models.py`):
   - No test that `parent.parent_id is not None` raises `ValidationError`.
   - No test that replies can't target a different task than their parent.
   - No test of the cascade: if a parent is deleted, replies delete too
     (Django ORM handles this, but no explicit test).

2. **Polymorphic target delete scenarios** (lines 17–32 of `models.py`):
   - `test_api.py` only creates comments on tasks.
   - No test for `ProjectUpdate` comments (`post_update_comment` is tested in
     integration but only from the web view, not from the API).
   - No test that deleting a task deletes its comments + replies.
   - No test that deleting a `ProjectUpdate` deletes its comments + replies.

3. **Reactions parity** (lines 85–114 of `test_api.py`):
   - `test_api.py` doesn't attach reactions to comments in any test.
   - No test of the comment-list endpoint with reactions pre-loaded
     (the `_task_comments` helper in views does this, but the API doesn't
     expose comment reactions — only the web view does).

4. **Mention extraction missing**:
   - Comments can embed mentions (`[@user](mention:<id>)` in Markdown).
   - No test that mentions are extracted or indexed from `comment.body`.
   - (`/apps/common/markdown.py` has `_render_mentions`, but no comments test
     exercises it.)

5. **Attachment + comment interaction** (lines 72–80 of `/apps/attachments/models.py`):
   - `Attachment` can target a comment (line 72–78).
   - `/apps/comments/tests/` never creates an `Attachment` tied to a comment.
   - No test of comment deletion cascading to attachments.

**Reactions (`/apps/reactions/tests/test_reactions.py`):**

- 138 LOC covering 3 core paths (toggle, summarize, toggle view).
- **Gap**: No test of what happens to reactions when a comment is deleted.
  - Create reaction on comment → delete comment → reaction should cascade.
  - Test is missing.
- **Gap**: No test of attachment-scoped reactions (reactions don't target
  attachments per ADR, but the polymorphic invariant across comment/task/update
  should be veri ed at scale).

**CommentFactory gap** (`/apps/comments/tests/factories.py`):

Only factory method; doesn't support `project_update` or `parent`. To test
project-update comments, tests must call `Comment.objects.create(project_update=...)`
by hand. **Minor friction — not blocking.**

---

## 7. Findings F1..Fn

### F1: Redundant WorkspaceMember validation in CommentSerializer

**File:** `/apps/comments/serializers.py:51–70`

**Issue:** The `validate_task` method in `CommentSerializer` checks
`WorkspaceMember.objects.filter(user=..., workspace=...).exists()` on every POST/PATCH.
The user's workspace membership is already verified by `CommentViewSet.get_queryset()`
(which filters `task__project__workspace__memberships__user=self.request.user`).

**Why it matters:** Adds one extra DB query to every comment create/update that
succeeds (already-memberqueries; failed attempts also fail earlier due to task
not existing in the queryset).

**Severity:** Low — not N+1, only 1 extra query per write.

**Mitigation:** Cache the membership check in the serializer context or skip it
entirely since the viewset already guarantees the task is accessible.

---

### F2: Comment reactions prefetch missing in _task_comments

**File:** `/apps/web/views.py:2733–2741` (the `_task_comments` helper)

**Pattern:** The helper prefetches `replies__author`, `attachments`, `replies__attachments`
but **not** `reactions`. Yet `_decorate_comments` (line 2742) calls
`attach_reactions(objs=decorated, target_field="comment", user_id=user_id)`,
which issues a separate `Reaction.objects.filter(comment__in=ids)` query.

**Why it matters:** Post-`53b7924` (commit message mentions collapsed dashboard
counts), the baseline rule for DRF endpoints is "prefetch or batch" (no N+1). The
`_task_comments` path follows the batching rule (one `summarize_reactions` query
for all comments + replies), so **this is not a bug**. However, it's unintuitive
that reactions aren't prefetched while attachments are.

**Why not prefetch:** `summarize_reactions` needs to query reactions across
multiple comment ids in a single pass for aggregation. A `prefetch_related` on
`comments` alone wouldn't help because replies are also reactions targets
(nested depth). The current approach (batch query after fetch) is cleaner.

**Severity:** Very low — pattern is correct; only a style observation.

**Note:** This is a **false positive from Wave 1's `_decorate_comments` N+1 flag**
(mentioned in 99-wave1-backlog.md as a suspected but verified-clean). Audit
confirms it stays clean post-`53b7924`.

---

### F3: No thread-integrity tests

**File:** `/apps/comments/tests/test_models.py`

**Issue:** The 3-test file covers only `was_edited` property. The depth-1 threading
invariant (parent can't have a parent, and reply must share parent's target) is
enforced in `Comment.clean()` (lines 81–97) but never tested.

**Risk:** A future edit to `clean()` could silently break the invariant.

**Suggested tests:**
- `test_reply_cannot_have_parent` — create a comment C1, reply C2, try to set
  `C3.parent = C2` and call `full_clean()`, expect `ValidationError`.
- `test_reply_must_share_target` — create task-comment C1, reply C2 targeting
  the same task, try to retarget C2 to a different task and call `full_clean()`,
  expect `ValidationError`.
- `test_delete_parent_cascades_replies` — create C1, C2 (reply), delete C1,
  assert C2 is gone.

---

### F4: No ProjectUpdate comment tests in test_api.py

**File:** `/apps/comments/tests/test_api.py`

**Issue:** All 25 tests (lines 43–125) create comments on tasks via the DRF API.
No tests for project-update comments.

**Why:** ADR 0022 explicitly limits the DRF `CommentViewSet` to tasks
(documentation in views.py:14–21, serializer task-required guard in
serializers.py:18–20). Update comments are created via the web view
`post_update_comment` only.

**Result:** The web layer (`/apps/web/tests/`) likely tests update comments via
integration tests (can't confirm without reading the web tests), but the API layer
has zero test coverage for the polymorphic target. **Acceptable by design, but note
it in test structure.**

---

### F5: No comment-deletion-cascades-reactions test

**File:** `/apps/reactions/tests/test_reactions.py`

**Issue:** The toggle service and view are tested (lines 24–115), but there's no
test verifying that deleting a comment cascades to delete its reactions.

**Test case:** Create comment C, reaction R on C, delete C, assert R is gone.

**Why it matters:** `Reaction.comment` has `on_delete=CASCADE` (models.py:30), so
the DB enforces it, but no explicit test locks in the behavior.

**Severity:** Low — Django ORM test suite covers CASCADE broadly, so we know it
works. But for **audit coverage** of the polymorphic invariant, it's a gap.

---

### F6: SerializerMethodField N+1 on comment body render

**File:** `/apps/comments/serializers.py:13–49`

**Issue:** The `body_html = serializers.SerializerMethodField()` (line 13) calls
`render_markdown(obj.body)` (line 49) on every serialized comment. If the API
returns 50 comments, 50 Markdown renders fire (not a query, but CPU-bound).

**Design intent:** The API is task-scoped (DRF only), so per-page volume is
small. The web view uses `_decorate_comments` which doesn't render HTML
(that happens in the template via Jinja filter).

**Severity:** Low — acceptable for the task-scoped API; web views avoid it.

---

### F7: Toggle reaction view single-ID batching

**File:** `/apps/web/views.py:toggle_reaction_view`, lines following the toggle call

**Pattern:** After calling `toggle_reaction(...)`, the view calls
`summarize_reactions(target_field=target_field, ids=[target.id], user_id=request.user.id)`
with a single-element list.

**Why:** `summarize_reactions` is batch-safe (no N+1 even for `ids=[...]`), so
this is not a performance issue. **Just an observation**: the single-ID call is
a valid use case the service supports.

**Severity:** None — pattern is correct.

---

### F8: Absence of CommentFactory for project_update

**File:** `/apps/comments/tests/factories.py`

**Issue:** The `CommentFactory` (lines 9–15) hard-codes `task = factory.SubFactory(TaskFactory)`
and doesn't support `project_update` or `parent` parameters.

**Result:** Tests that need project-update comments or replies must use
`Comment.objects.create(...)` by hand, adding boilerplate.

**Severity:** Very low — not a bug, just friction.

**Suggested improvement:**
```python
class CommentFactory(DjangoModelFactory):
    ...
    task = factory.SubFactory(TaskFactory)
    author = factory.SubFactory(UserFactory)
    body = factory.Faker("paragraph")
    project_update = None
    parent = None
```

Then tests can call `CommentFactory(project_update=update)` or
`CommentFactory(parent=parent_comment)`.

---

## 8. Defer-to-measurement

### M1: Comment list + reactions rendering latency

**Scope:** The `task_comments_fragment` endpoint (`/apps/web/views.py`, lines following
`task_comments_fragment`) and the full-page task-detail rendering.

**Questions:**
1. What's the query cost of `_task_comments` on a task with 20 comments + 40 replies
   (60 total, each with 2–3 reactions from 5 users)?
   - Expected: 3 queries (comments+replies + replies__author + summarize_reactions).
   - Actual: Measure with django-debug-toolbar or pytest's `assertNumQueries`.

2. Does the `_decorate_comments` loop over 60 items + the `attach_reactions` single
   query complete under 50ms on a cold cache?

**Why:** Wave 1 verified no N+1, but hasn't measured absolute latency or
Markdown-render CPU cost.

**Testing:** Add `assertNumQueries` tests to `apps/web/tests/test_task_detail_view.py`
for the fragments (already has other detail-page tests).

**Action:** Part of Wave 3 regression-test suite (PR-3 in backlog).

---

### M2: Reactions bar query count on kanban / All Tasks

**Scope:** The kanban and list-view task cards show reaction bars (the `_reaction_bar.html`
partial).

**Question:** Are reactions prefetched in the board/list querysets, or does the
template call `summarize_reactions` per row?

**Why:** Wave 1 didn't audit the web views' task querysets in detail (focused on
dashboard). If reactions are called per-row in template context, that's N queries
for N tasks.

**Hypothesis:** Reactions are **not** prefetched on board/list queries (only
summary-level data). The reaction bar is rendered client-side (HTMX toggle only),
not on initial page load. **Verify.**

**Action:** Inspect `/templates/web/projects/_task_card.html`,
`_table_row.html`, and the corresponding view querysets in `views.py`.

---

### M3: Project-update comment thread rendering cost

**Scope:** The inbox Updates preview and project overview (both render update
comment threads).

**Question:** How many queries does rendering 5 updates × 3 comments each (15 total)
with reactions cost?

**Hypothesis:** `post_update_comment` view and the project-detail view each call
`_decorate_comments` on fetched threads, so batching applies. **Verify measure**.

**Action:** Profile the project overview page (`ProjectDetailView`) with and
without comments.

---

## 9. Cross-links to C1, C6, C7

- **C1 (Tasks):** Comment model attaches to Task via nullable FK. Task.comments
  cascade-delete via `on_delete=CASCADE`. Verified no infinite recursion (C1
  responsibility: check Task model self-references; C5 confirms Comment→Task
  doesn't cycle back).

- **C6 (Activity log):** Comment events (`comment.created`, `comment.edited`,
  `comment.deleted`) are written to ActivityLog only for task comments (ADR 0022,
  decision section). Activity queries in `_task_activity` filter on
  `payload__task_id` for comment events (lines following `_task_activity` in
  views.py). **C6 should verify the filter still matches the events logged by
  `CommentViewSet.perform_*`**.

- **C7 (Notifications):** `notify_comment_created` (called from
  `CommentViewSet.perform_create`, line 75) sends notifications only for task
  comments (per ADR 0022). **C7 should verify the notification rules match.**

- **Wave 1 PR-2:** The query collapse in `53b7924` reused prefetched labels via set
  comprehension. Comment batching (reactions) already uses `summarize_reactions`
  single-query guarantee (not affected by the PR-2 changes).

- **Wave 1 PR-4 (SSE opt-in):** Comments are not explicitly mentioned in
  `actaForceApplySelfEvent` opt-in check. The timeline re-render fires on
  `acta:task-changed` SSE events, which includes comment posts (comment.created
  activity event). **C5 finding: SSE event routing for comments should be
  verified in C6 or C7.**

- **Wave 1 W4 (labels_changed exclusion):** Comment render doesn't filter
  activity entries (comments always appear in timeline). The `task.labels_changed`
  exclusion (lines following `_task_activity`) is separate, not related to
  comments. **No overlap.**

---

## Summary

**Green flags:**
- Polymorphic FK invariant is enforced at the DB level + application code.
- Threading depth-1 limit is enforced at both layers.
- Reactions batching (`summarize_reactions`) is correctly implemented and tested.
- Comment listing paths (`_task_comments` + `_decorate_comments`) are N+1-free
  (post-`53b7924` confirmed).

**Low-severity findings:**
- F1: Redundant workspace membership check in serializer (one extra query per write).
- F3–F5, F8: Test coverage gaps (no thread integrity, no polymorphic-target
  deletes, no reaction cascades, factory friction). **Acceptable for current
  velocity; prioritize for Wave 3.**

**Deferred measurements:**
- M1: Comment list + reactions rendering latency under load.
- M2: Reactions prefetch status on board/list views.
- M3: Project-update thread rendering cost.

**Architectural correctness:** Confirmed. No silent bugs; patterns follow
ADR 0022 and Wave 1 PR-2 prefetch policy.
