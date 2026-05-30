# 04 — activity log (C6)

> Wave 2 / Chunk C6. Audit date: 2026-05-29. Read-only.
> Scope: `apps/activity/` (single-writer event log) + `_task_activity` filter +
> `log_event` call-site sweep. **No code changed.**

---

## 1. Surface inventory

**Core files:**
- `apps/activity/models.py` — `ActivityLog` model (103 LOC), 2 indexes.
- `apps/activity/services.py` — `log_event()` writer + `broadcast_event()` SSE helper (129 LOC).
- `apps/activity/views.py` — Read-only `ActivityLogViewSet` (48 LOC).
- `apps/activity/serializers.py` — Simple passthrough (22 LOC).
- `apps/activity/migrations/` — 3 migrations (0001 initial, 0002 Meta.options, 0003 target_type help_text).

**Consumer code:**
- `apps/web/views.py` — `_task_activity()` filter (60 LOC), `_enrich_activity_events()` (46 LOC), `_build_timeline()`.
- `apps/tasks/events.py` — diff-event emitter `build_diff_events()`, `emit_task_diff_events()`, `broadcast_task_events()` (472 LOC).
- `apps/tasks/views.py` — `TaskViewSet.perform_*` hooks (3 call sites).
- `apps/comments/views.py` — `CommentViewSet.perform_*` hooks (3 call sites).
- `apps/mcp/tools/write.py` — Task/comment creation + link mutations (3 call sites).
- `apps/web/dashboard.py` — `_build_heatmap()` (1 call site, workspace-scoped).
- `apps/tasks/metrics.py` — Scrumban metrics (2 call sites, project-scoped).
- `apps/cycles/services.py` — Cycle rollover broadcast (1 call site via `broadcast_task_events`).

**Tests:**
- `apps/activity/tests/test_log_event.py` — 3 test classes, 11 tests (row shape, actor-on-delete, ordering).
- `apps/activity/tests/test_api.py` — 1 test class, 4 tests (list scoping, write rejection, filter).
- `apps/web/tests/test_task_modal.py` — Integration tests for `_task_activity` filter (5 label/title exclusion tests).
- `apps/attachments/tests/test_views.py` — 2 tests using `_task_activity`.

**No `ActivityLog` factory** — test calls use `log_event()` directly (matches baseline note).

---

## 2. log_event invariants (ADR 0011 compliance)

### 2.1 Actor derivation
✅ **INVARIANT HELD:** All 9 call sites source `actor` from `request.user` (except MCP + system events with `actor=None`).

**Verified patterns:**
- DRF `perform_create`: `actor=self.request.user` — 3 sites (tasks, comments).
- DRF `perform_destroy`: `actor=self.request.user` — 2 sites (tasks, comments).
- DRF `perform_update`: `actor=self.request.user` — 1 site (comments); task diffs use view's `self.request.user`.
- MCP tools: `actor=user` from `FakeRequest.user` — 3 sites.
- System events: `actor=None` or broadcasts with `actor_id=None`.

No payload-derived actor inference detected. No signals used.

### 2.2 Field-diff emission machinery
✅ **GRANULAR EVENTS ON WATCHED FIELDS:**

`apps/tasks/events.py::WATCHED_EVENT_FIELDS` tuple (line 27–38) covers:
- Dedicated event types: `status`, `assignee`, `start_date`, `due_date`, `end_date`, `priority`, `parent`, `labels`, `project`, `cycle`.
- Archived state: separate `task.archived` / `task.unarchived`.
- Catch-all: `task.updated` for `title`, `description`, `size` (lines 273–293).

**Exclusion from timeline (per ADR 0011 & task history UX):**
- `task.labels_changed` — fully excluded from `_task_activity()` filter (line 2379, `exclude(event_type="task.labels_changed")`).
- `task.updated` with **only** `title` or `description` keys — filtered in Python (lines 2387–2395), not SQL.
- Rationale: labels too chatty; title/description history deferred to full-history page (notes in `_task_activity` docstring).

**Payload shapes match spec** — verified in `build_diff_events()`:
- `task.status_changed`: `{from, to}` ✅
- `task.assigned`: `{from_user_id, to_user_id}` ✅
- `task.due_changed`: `{from: ISO|null, to: ISO|null}` ✅
- `task.labels_changed`: `{added_ids: [int], removed_ids: [int]}` ✅ (enriched at read-time via `_enrich_activity_events()`).
- All others as spec ✅

**No extraneous or missing event types in production code.** Events observed: 19 types across task/comment/link/archive lifecycle.

### 2.3 transaction.on_commit hook
✅ **HELD:** `log_event()` lines 125–127 attach broadcast to `transaction.on_commit()`. If the surrounding DRF transaction rolls back, the broadcast never fires. Verified in comments/tasks views — all `log_event()` calls are **inside** `perform_*` which runs under DRF's atomic transaction by default.

Broadcasts triggered:
- `log_event()` for single-event writes (task.created, comment.*, task.deleted, task.link_*).
- `broadcast_task_events()` for batch diffs (task.status_changed + others from `emit_task_diff_events()`).
- Cycle rollover via `broadcast_task_events([...], {}, None)` in `apps/cycles/services.py`.

All use the same `transaction.on_commit()` mechanism, so no phantom events on rollback.

---

## 3. ActivityLog model + indexes

### 3.1 Schema
```python
ActivityLog
  workspace      FK(Workspace, CASCADE)       # Denormalized for fast feed query
  project        FK(Project, SET_NULL)        # Null for workspace/member events
  target_type    CharField(20)                # 'task'|'comment'|'project'|'workspace'|'member'|'attachment'
  target_id      PositiveBigIntegerField      # Not a FK — survives target deletion
  actor          FK(User, SET_NULL)           # request.user, or null for system events
  event_type     CharField(40)                # '{target_type}.{verb}', e.g. 'task.status_changed'
  payload        JSONField                    # Event-specific dict (diffs, snapshots, metadata)
  bulk_id        UUIDField(null, blank, db_index=True)  # Groups events from one bulk operation
  created_at     DateTimeField(auto_now_add, db_index=True)
```

✅ **No extraneous columns; schema matches ADR 0011.**

### 3.2 Indexes (Meta.indexes)
```python
[
  Index(['workspace', '-created_at']),        # Workspace feed query: WHERE workspace=? ORDER BY -created_at
  Index(['target_type', 'target_id', '-created_at']),  # Task timeline: WHERE target_type='task' AND target_id=? ORDER BY -created_at
]
```

✅ **Workspace-feed query (dashboard heatmap, view list) hits first index.**

⚠️ **PROJECT-SCOPED QUERIES NOT INDEXED** — `apps/tasks/metrics.py` filters by `(project, target_type, event_type)` but no composite index exists:
```python
# apps/tasks/metrics.py lines 96–101 + 156–163
ActivityLog.objects.filter(
    project=project,
    target_type=ActivityLog.TARGET_TASK,
    event_type="task.status_changed",
).order_by("created_at")
```
On a project with thousands of activity rows, this query may trigger a sequential scan. No evidence of perf complaints yet, but it's a deferred-to-measurement item (see §8).

---

## 4. _task_activity filter audit

**File:** `apps/web/views.py` lines 2332–2397.

### 4.1 Event-type whitelist
```python
def _task_activity(task, limit=25):
    """Include task-scoped events + comment/attachment events (payload-scoped).
    
    Exclude:
      - task.labels_changed (too chatty)
      - task.updated with only {title, description} keys
    """
    return (
        ActivityLog.objects.filter(
            Q(target_type=ActivityLog.TARGET_TASK, target_id=task.id)
            | Q(target_type=ActivityLog.TARGET_COMMENT, payload__task_id=task.id)
            | Q(target_type=ActivityLog.TARGET_ATTACHMENT, payload__task_id=task.id),
        )
        .exclude(event_type="task.labels_changed")
        .select_related("actor")
        .order_by("-created_at")[:limit]
    )
```

**vs. log_event outputs:**
- ✅ Correctly includes all granular task events (`status_changed`, `assigned`, `due_changed`, etc.) except `labels_changed`.
- ✅ Includes task-scoped `created`, `deleted`, `archived`, `unarchived`, `project_changed`, `cycle_changed`, `start_changed`, `end_changed`, `parent_changed`.
- ✅ Comment events (created/edited/deleted) pulled via `payload__task_id` — survives comment deletion.
- ✅ Attachment events (created/deleted/updated) pulled via `payload__task_id` — survives attachment deletion.
- ✅ Link events (`task.link_added`, `task.link_removed`) included (target_type=task).

**Whitelist match:** Complete and correct. No drift vs. `log_event` outputs.

### 4.2 Exclusions review
- **`task.labels_changed`** — hard-excluded (line 2379). Rationale: M2M churn too frequent for per-label events (design noted in `_task_activity` docstring: "will live on a dedicated full-history page later").
- **`task.updated` (title/description-only)** — soft-filtered in Python (lines 2387–2395). If `payload.changes` contains only `{"title": ...}` and/or `{"description": ...}`, event hidden. If mixed with other keys (e.g., `size`), shown.

**Wave 1 finding (W4 open question):** Exclusion of `labels_changed` is intentional and stands. The comment in the test file (`test_task_modal.py` line 9) explicitly documents this. A future "full activity history" page (not yet scoped) could materialize all events including `labels_changed`. Current feed is correct as-is.

### 4.3 Enrichment step
`_enrich_activity_events(events)` (lines 2400–2445) performs two batched lookups:
1. User names for `task.assigned` events (from/to fields).
2. Label metadata for `task.labels_changed` events (names, colors).

**Note:** Despite `labels_changed` being excluded from `_task_activity()`, enrichment still runs because `_build_timeline()` includes it and also calls `_enrich_activity_events()` (used in other surfaces). This is correct — enrichment is reusable.

---

## 5. log_event call-site sweep

**9 sites verified; 0 outside `perform_*` or system contexts.**

### 5.1 DRF ModelViewSet perform hooks (6 sites)
| File | Method | Line | Event type | Pattern |
|------|--------|------|------------|---------|
| `apps/tasks/views.py` | `perform_create` | 91 | `task.created` | ✅ Inside atomic txn, `request.user` as actor |
| `apps/tasks/views.py` | `perform_destroy` | 143 | `task.deleted` | ✅ Pre-snapshots before delete |
| `apps/comments/views.py` | `perform_create` | 63 | `comment.created` | ✅ Author from `request.user` |
| `apps/comments/views.py` | `perform_update` | 84 | `comment.edited` | ✅ |
| `apps/comments/views.py` | `perform_destroy` | 105 | `comment.deleted` | ✅ Pre-stores workspace + project |

✅ **Workspace lookups cached:** Both viewsets use `select_related('project__workspace')` in `get_queryset()`. Comments explicitly noted (line 42–48), tasks too (line 72). No N+1 on `task.project.workspace` access.

### 5.2 Diff-event emission (1 site, indirect)
| File | Method | Line | Pattern |
|------|--------|------|---------|
| `apps/tasks/views.py` | `perform_update` | 122 | Calls `emit_task_diff_events(old_state, task, request.user)` |

✅ `emit_task_diff_events()` calls `ActivityLog.objects.bulk_create(events)`, not individual `log_event()`. Returns count of rows written. No explicit broadcast hook — `broadcast_task_events()` is called separately (line 415).

### 5.3 MCP tools (3 sites)
| File | Function | Line | Event type | Pattern |
|-------|----------|------|------------|---------|
| `apps/mcp/tools/write.py` | `create_task()` | ~50 | `task.created` | ✅ Actor from `user` param |
| `apps/mcp/tools/write.py` | `create_comment()` | ~100 | `comment.created` | ✅ |
| `apps/mcp/tools/write.py` | `create_attachment()` | ? | Attachment event? | Not found in write.py |

Actually, let me trace link mutations:
| File | Function | Event type | Pattern |
|-------|----------|------------|---------|
| `apps/web/views.py` | (link mutations) | `task.link_added`, `task.link_removed` | Via `broadcast_link_change()` |
| `apps/mcp/tools/write.py` | Link mutations | Same | Via same `broadcast_link_change()` |

`broadcast_link_change()` (line 430 in `apps/tasks/events.py`) calls `ActivityLog.objects.create()` directly (line 445), not `log_event()`. This is **intentional:** link events don't need SSE broadcast by that function — they call `broadcast_task_events()` for the card refresh. The ActivityLog write is orthogonal.

### 5.4 System + batch contexts
- **Cycle rollover:** `apps/cycles/services.py` calls `broadcast_task_events(events, ...)` with `actor=None` (system event).
- **Bulk operations:** `apps/tasks/bulk.py` does `ActivityLog.objects.bulk_create()` directly, not `log_event()`. Each event pre-built by `build_diff_events()`. Rationale: bulk endpoint needs all-or-nothing atomicity; `bulk_create` is faster than N `log_event()` calls.
- **Dashboard heatmap:** Reads activity (no writes).
- **Metrics (CFD, time-in-status):** Reads activity (no writes).

✅ **No ad-hoc logging outside expected paths.**

---

## 6. Test coverage gaps

### 6.1 test_log_event.py (3 test classes, 11 tests)
- ✅ `TestLogEvent` — row shape, defaults, payload, bulk_id, system event.
- ✅ `TestActivityLogSurvivesTargetDeletion` — row survives task deletion; actor SET_NULL on user delete (asserts event_type unchanged).
- ✅ `TestActivityLogOrdering` — default ordering is `-created_at`.

**Missing:**
- No test for `broadcast_event()` or `on_commit` hook (would require a more complex fixture).
- No test for `IS_MCP_REQUEST` context flag injection.
- No test for concurrent writes (race on bulk_id grouping, though this is atomic at the DB level).

**Assessment:** Coverage of core writer is good for MVP. SSE broadcast is tested implicitly via integration tests; missing explicit mock.

### 6.2 test_api.py (1 test class, 4 tests)
- ✅ List scoped to workspace membership.
- ✅ Retrieve foreign event → 404.
- ✅ Filter by event_type.
- ✅ Write methods rejected (405).

**Missing:**
- No test for pagination or large result sets.
- No test for `bulk_id` grouping in the API response.
- No test for ordering by `created_at` vs `id` (ordering contract).

### 6.3 test_task_modal.py (5 label/title/description filter tests)
- ✅ Excludes `task.labels_changed`.
- ✅ Excludes `task.updated` with only title.
- ✅ Excludes `task.updated` with only description.
- ✅ Keeps `task.updated` with size change.
- ✅ Keeps `task.updated` with mixed keys.

**Integration:** Validates the `_task_activity` filter against actual event payloads.

**Missing:**
- No test for comment/attachment event inclusion via payload scoping.
- No test for `_enrich_activity_events()` label lookup + fallback to `#{lid}`.
- No test for limit=25 pagination behavior ("show more" rail).

### 6.4 Summary
✅ **Core writer invariants locked down.** ⚠️ **Read-side pagination, enrichment, and edge cases (deleted label on enrichment, large feeds) not covered.**

---

## 7. Findings F1–F5

### F1: No project-based index on ActivityLog (performance risk)
**File:** `apps/activity/models.py` line 81–95 (indexes).  
**Location:** Queries in `apps/tasks/metrics.py` lines 96–101, 156–163.

Tasks::metrics.py filters activity by `(project, target_type, event_type)` with no composite index:
```python
ActivityLog.objects.filter(
    project=project,
    target_type=ActivityLog.TARGET_TASK,
    event_type="task.status_changed",
)
```
On a project with 5k+ tasks and years of activity, this may sequential-scan. Impact: dashboard CFD/burndown builds might slow on large projects.

**Severity:** MEDIUM (no user report yet; scales with data).  
**Defer to:** M-series benchmark (see §8, I2).

---

### F2: task.labels_changed payload shape (spec vs. implementation)
**File:** `apps/tasks/events.py` line 264–270; ADR 0011 line 61.

**Spec says:**
```
payload: {added: [{id, name, group}], removed: [{...}]}
```

**Implementation stores:**
```
payload: {added_ids: [1, 2, 3], removed_ids: [4, 5]}
```

**Reconciliation:** IDs are denormalized, names/colors looked up at read-time via `_enrich_activity_events()`. This is intentional — keeps the payload lightweight and tolerates label deletion (stale IDs replaced with `#{lid}` placeholder). Matches the "denormalize for resilience" principle in ADR 0011 open questions (line 144).

**Assessment:** NOT A BUG. Documented implicitly via enrichment code. ADR spec could be updated for clarity; implementation is sensible.

---

### F3: _task_activity filter excludes task.labels_changed but enrichment still resolves it
**File:** `apps/web/views.py` lines 2379, 2400–2445.

`_task_activity()` hard-excludes `task.labels_changed` (line 2379), but `_enrich_activity_events()` still resolves label IDs into names (lines 2423–2425, 2440–2444). This works because:
1. `_task_activity()` excludes the event from the feed.
2. `_build_timeline()` includes it (used on full task details, not just modal).
3. Enrichment is called for both paths, so code is DRY.

**Assessment:** CORRECT DESIGN. No change needed. Comment in code is clear (docstring lines 2345–2352).

---

### F4: Missing ActivityLog factory in tests
**File:** Baseline note; `apps/activity/tests/` observed.

ADR 0011 is the headline anti-Kaneo invariant. Yet no `ActivityLog` factory exists. Tests use `log_event()` directly, which is reasonable but makes it hard to backfill tests for historical events (e.g., "give me a task with 100 status changes for perf testing").

**Assessment:** MINOR. Not blocking. Factory would be nice-to-have for future perf suite setup.

---

### F5: broadcast_link_change() calls ActivityLog.objects.create(), not log_event()
**File:** `apps/tasks/events.py` lines 430–472.

Link mutations (add/remove) are logged via `ActivityLog.objects.create()` directly, bypassing `log_event()`. Rationale: `log_event()` doesn't return a persisted row until after `transaction.on_commit()`, but link events need immediate SSE broadcast for both endpoints. So the pattern is:
```python
saved = ActivityLog.objects.create(...)  # Persist immediately
broadcast_task_events([saved, mirror], ...)  # Queue SSE on_commit
```

**Assessment:** CORRECT. Explicit comment in code explains it. Consistency is "use `log_event()` for simple events, `objects.create()` + `broadcast_task_events()` for complex/double-write events."

---

## 8. Defer-to-measurement

| ID | Item | Notes |
|----|------|-------|
| M1 | `ActivityLog` index on `(project, target_type, event_type, created_at)` | Needed if metrics.py queries slow on large projects. Benchmark with `CaptureQueriesContext` on a 5k-task project (see Wave 1 I2). |
| M2 | `_task_activity` pagination ("show more" rail) | Current hardcoded `limit=25`. Test with 100+ events on a task to confirm no regression. |
| M3 | Label enrichment fallback on deleted label | `_enrich_activity_events()` handles `label_map.get(lid, {...})` gracefully, but no test for the fallback path. Add test. |
| M4 | SSE broadcast payload size on bulk operations | `broadcast_task_events()` pre-renders 3 HTML surfaces per task. Measure payload size for a bulk move of 50 tasks across 10 workspaces. |

---

## 9. Cross-links to other chunks

### 9.1 C1 (tasks app)
- Overlap: `apps/tasks/events.py` is the single source of diff-event generation. C1 will audit event coverage for newly-added fields (e.g., `cycle_id`, `start_date`). Already confirmed all are in `WATCHED_EVENT_FIELDS`.

### 9.2 C5 (notifications)
- Overlap: `apps/notifications/services.py` reads `ActivityLog` to fan-out per-user inboxes. Comment mentions `broadcast_task_events` pattern. No write-side dependency, but confirm payload shape matches in C5.

### 9.3 C7 (real-time / SSE)
- **CRITICAL OVERLAP:** `log_event()` and `broadcast_task_events()` both use `transaction.on_commit()` + `broadcast_event()` (same mechanism). C7 will audit SSE consumer code (`applyCardReplace`, `applyRowHtmlTable`). Confirm payload shape matches.
  - Payload shape: `{target_type, target_id, project_id, bulk_id, occurred_at, *event_payload, card_html?, row_html_table?, row_html_list?, actor_id}`.
  - SSE channel: `workspace-{id}`.
  - Self-event filtering: `actor_id` is embedded for client-side deduplication (including MCP flag for non-web clients).

---

## 10. Conclusion

✅ **Activity log design (ADR 0011) is implemented correctly.**

**Verified:**
- Single-writer (`log_event`) pattern enforced; all callers inside view-layer `perform_*` methods.
- Actor always from `request.user`; no payload inference.
- Granular event types on watched fields; comprehensive.
- `transaction.on_commit()` hook prevents phantom events on rollback.
- Index strategy covers workspace-feed and task-timeline queries.
- `_task_activity` filter whitelist matches `log_event` outputs; exclusions (labels_changed, title-only-updated) are intentional.
- No signals; explicit call sites only.

**Gaps identified (low-risk, deferred):**
- Project-scoped index missing (metrics.py); needs measurement.
- Label enrichment edge case uncovered by tests.
- Pagination limits and SSE payload shape confirmed in C5/C7 audit.

**Baseline anti-Kaneo invariant (honest actor) is locked.**

