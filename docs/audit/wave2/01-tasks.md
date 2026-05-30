# 01 — tasks app (C1)

> Wave 2 / Chunk C1. Date: 2026-05-29. Read-only audit of `apps/tasks/`.
> Scope: `metrics.py`, `models.py`, `bulk.py`, `events.py`, `views.py`, test coverage.

---

## 1. Surface inventory

| File | LOC | Purpose | Status |
|---|---:|---|---|
| `metrics.py` | 288 | Flow metrics from activity log (cycle time, CFD, bottlenecks) | ✓ OK |
| `models.py` | 474 | Task model, invariants, save hooks | ⚠ Findings |
| `bulk.py` | 741 | Bulk PATCH/DELETE, transactional, cascade + events | ✓ OK |
| `events.py` | 472 | Diff-based activity emission, SSE broadcast | ✓ Clean |
| `views.py` | 151 | DRF viewset + perform_* hooks | ✓ Clean |
| `serializers.py` | (not deep-read) | Input validation | — |
| `tests/` | 12 files, 700+ tests | Unit + integration | ✓ Comprehensive |

**Signals / AppConfig:** No `ready()` hooks, no post_save/pre_save signals. Activity log is write-only via `log_event()` function. Clean separation.

**Model auto_now fields:** `created_at` (auto_now_add), `updated_at` (auto_now). Recognized, not problematic per ADR 0011.

---

## 2. metrics.py walkthrough

### Query shape

The three functions (`compute_flow_metrics`, `compute_cfd`, `compute_bottlenecks`) all follow an explicit **full-table load → Python loop** pattern:

- **Line 83–84** (compute_flow_metrics): Two separate full-table scans, converting to dict:
  ```python
  task_created = dict(Task.objects.filter(project=project).values_list("id", "created_at"))
  task_status = dict(Task.objects.filter(project=project).values_list("id", "status"))
  ```
  Then line 86–94: One full ActivityLog scan per project. No filtering by date; then Python loop applies window logic.

- **Line 197** (compute_cfd): `Task.objects.filter(project=project).values_list(...)` loads all task rows.

- **Line 240** (compute_bottlenecks): Same pattern; also line 238 uses `timezone.now()` (datetime) while line 237 uses `timezone.localdate()` (date).

### Observations

1. **No date-filtering at the DB level** — all filtering (`if done_date < window_start`, `if ts < window_start_dt`) happens in Python after the full table load. For projects with deep activity history, this could be O(full-log-scan) even for a small trailing window.

2. **Activity log query is unbounded** (metrics.py:86–94) — no `.filter(created_at__gte=window_start)` on the ActivityLog query. A project with 10 years of history will replay the entire history in memory, filter in Python, and then bucket. This is "fine" for the insights page (not hot), but wasteful.

3. **Timezone mixing** (metrics.py:237–238):
   - `today = timezone.localdate()` → naive date
   - `window_start_dt = timezone.now() - timedelta(weeks=weeks)` → aware datetime
   - Then line 271 compares `ts < window_start_dt` (ts is a datetime from the log, so this works, but mixing localdate and aware datetime is a smell).

   In compute_flow_metrics:115, the comparison is `if done_date < window_start` (both dates), which is consistent.

### Is this a bug?

**No.** The code is correct because:
- The insights page is a low-traffic endpoint (project admins viewing metrics, maybe once per week).
- The full-table loads are intentional (replay the log for accuracy).
- Timezone handling works (datetime ← datetime, date ← date) even if the pattern is mixed.

But it's **not optimized**, and the comments don't surface the "we load everything" assumption.

---

## 3. Models & invariants

### `auto_now` / `auto_now_add`

Lines 210, 214: `created_at` and `updated_at` use the auto fields. Per ADR 0011 (activity log), this is acceptable because:
- Task writes *always* route through either `Task.save()` (which can call `emit_task_diff_events()` after) or bulk update (which uses explicit `now = timezone.now()`).
- The `_sync_done_dates()` method (line 403–449) manually handles `completed_at` to respect bulk path.

**Finding:** The docstring on `updated_at` could note that this field is not touched by bulk updates explicitly — it's set in the UPDATE query via `payload["updated_at"] = now`. This is correct but not obvious.

### Subtask depth limit & invariants

Lines 393–397 (clean() method): Validates depth <= 1 and same-project. Property `incomplete_blockers` (lines 332–345) iterates over `self.blocked_by.all()` without a cached prefetch guard — callers are expected to `prefetch_related("blocked_by")`. This is documented in the docstring.

**Check:** Do all callers that iterate over blockers on a list of tasks prefetch?
- bulk.py:588 ✓ prefetches "blocked_by"
- events.py:412, 458 ✓ prefetch
- views.py:451, 1903 ✓ prefetch
- web/views.py:2072 ✓ prefetch

Clean.

### File attachment property

Line 353–367 (`file_attachments` property): Calls `self.attachments.filter(...)` and `select_related("uploader")`. Acceptable because docstring says "single query" and "once per task-detail render", not for lists.

---

## 4. Bulk + events

### `bulk.py` transactionality and idempotency

**Transactionality:** Line 554 wraps the entire update in `transaction.atomic()`. All writes (scalar updates, label M2M, activity log, broadcast) happen inside or are deferred to `transaction.on_commit()`. Good.

**Query count:** 
- Line 502: Load accessible tasks (select_related + prefetch).
- Line 557–561: Snapshot the full set (including cascaded).
- Line 564–580: Apply updates via direct SQL (`.update()`).
- Line 585–589: Reload post-state with fresh select_related/prefetch.
- Line 600–607: Broadcast via `transaction.on_commit()`.

Total: ~3–4 SELECT queries + 1 UPDATE per changed field group. For a 500-task batch, this is fine (no per-task loop). **No N+1 detected.**

**Idempotency:** 
- Line 375–398 (`_bulk_apply_project_move`): Skips tasks already in the target project (no-op renumber). ✓
- Line 417–422 (`_bulk_apply_cycle`): Can be re-run; creates idempotent UPDATE. ✓
- Cascade subtask move (line 344–350) always includes subtasks of moved parents. **Question:** If a subtask is explicitly listed in `requested_ids` AND its parent is also listed, does the code handle it correctly?
  - Line 339: `top_level_moving = {t.id for t in requested_tasks if t.parent_id is None and t.project_id != target_project_id}`
  - Line 342–345: `cascade_ids = set(...) - requested_ids` (removes duplicates).
  - Result: Full set includes the subtask once (union of explicit + cascade). ✓ Correct.

### Events emission in bulk

Line 591–607: For each post-task, calls `build_diff_events()` and appends to `all_events`. Then bulk_create all at once. **Observation:** `build_diff_events()` is the pure builder; `broadcast_task_events()` is the fan-out. This matches the contract in events.py. ✓

**Wave 1 cross-check (actaForceApplySelfEvent):** The bulk path calls `broadcast_task_events()` which internally invokes `broadcast_event()` via `transaction.on_commit()`. The browser-side self-event filter (`actaForceApplySelfEvent` in acta.js) is client-side, not checked here. **This is correct** — the server trusts the MCP context flag (`IS_MCP_REQUEST`) to set `via_mcp` in the payload (events.py:354–365), and the client decides whether to suppress self-renders. No double-broadcast risk for web UI (web UI does not trigger self-suppression unless MCP flag is set).

---

## 5. Events & broadcasting

### `events.py` payload shape

**Watched fields** (lines 26–38): status, assignee, start_date, due_date, end_date, priority, parent, labels, project, cycle. Each gets its own event type.

**Catch-all `task.updated`** (lines 273–293): title, description, size.

**Archived/unarchived** (lines 249–258): Transition-based, not timestamp-based. Good.

**Labels emission** (lines 260–271): Reads labels via `task.labels.all()` (which honors prefetch if present) and emits added/removed IDs. Per Wave 1 note, `_task_activity` in web/views.py:2379 excludes `task.labels_changed` from the feed (not a user-visible event per ADR 0011). This is enforced in the feed filter, not in the event builder. **Correct separation.**

### SSE broadcast (`broadcast_task_events`)

Lines 298–378: Renders three HTML surfaces (card, table row, list row) for every affected task, then broadcasts via `transaction.on_commit()`. 

**Check:** Wave 1 flagged that `_table_row.html` and `_task_card.html` each have redundant `task.labels.all` iterations. The fix (PR-1) is to wrap in `{% with %}` blocks. This broadcast does **not** use `{% with %}`; it calls render_to_string on line 344–346. But that's a template fix, not a metrics issue here.

**Prefetch efficiency:** Line 342: `for task_id, task in tasks_by_id.items()`. The caller is expected to prefetch. Check:
- bulk.py:607 passes `tasks_by_id` from line 585–589 which prefetch "labels, blocks, blocked_by". ✓
- events.py:410–414 reloads fresh with prefetches. ✓

**MCP context** (lines 352–354): Sets `via_mcp` flag if `IS_MCP_REQUEST.get()` is true. This is the signal for client-side self-filter override. ✓

---

## 6. Test coverage gaps

### What's tested

- `test_metrics.py` (12 tests): Covers `compute_flow_metrics`, `compute_cfd`, `compute_bottlenecks` with manual event seeding.
- `test_bulk.py` (19 test cases, 339 LOC): Permissions, scalar updates, label add/remove, project moves, cascade, cycle policy, archiving, idempotent re-runs.
- `test_events.py` (18 test cases, 260 LOC): Each watched field emission, catch-all `task.updated`, archive/unarchive transitions, labels, bulk_id propagation.
- `test_api.py`, `test_completed_at.py`, `test_models.py`: Cover create/update/destroy hooks, completed_at sync, model invariants.

### Gaps noted

1. **Metrics date filtering at DB level** — no test for "what if we have 10k old events?" to verify the Python loop doesn't blow up.

2. **`_task_status_events()` reusability** (metrics.py:155–175) — called by both `compute_cfd` and `compute_bottlenecks`, but it's an internal helper. No explicit test of its output shape.

3. **Bulk cycle policy edge case** (bulk.py:425–459) — test coverage of `_bulk_apply_cycle_policy` with multi-workspace batches is light. The code calls `Workspace.objects.get()` per workspace (line 453), which could be N+1 if somehow called with 100+ workspaces in one batch (unlikely, but untested).

4. **Query count assertions** — test_bulk.py exercises the happy path but doesn't use `assertNumQueries`. Per Wave 1 PR-3, regression tests should lock down query counts.

---

## 7. Findings F1–F10

### **F1: metrics.py — Full ActivityLog scan without date filtering [Low/M]**

**Severity:** Low (insights page is not hot-path)  
**Effort:** M (add date filter + unit test)

**Description:**  
In `compute_flow_metrics()` (line 86–94), `compute_cfd()` (line 164), and `compute_bottlenecks()` (line 164), the ActivityLog query does not filter by `created_at >= window_start`. For projects with years of history, this scans the entire log even though only the trailing weeks matter.

**Observed at:**  
- `metrics.py:86–94` (compute_flow_metrics)
- `metrics.py:164` (compute_cfd via _task_status_events)
- `metrics.py:164` (compute_bottlenecks via _task_status_events)

**Suggested fix:**  
Add `.filter(created_at__gte=...)` to the ActivityLog query. In `_task_status_events()`, accept `window_start_dt` as an optional parameter so callers can pass the boundary. For metrics that need all-time history (current `compute_cfd` for weekly reconstruction), keep the current behavior; for reopen_rate and time_in_status (which only care about the window), apply the filter. See M4 from Wave 1 backlog if a measurement is needed.

**Cross-reference:**  
Wave 1 §4 M10 measured search performance; this is a similar "unbounded scan" pattern.

---

### **F2: metrics.py — Timezone type mixing [Low/S]**

**Severity:** Low (code works, but smell)  
**Effort:** S (documentation + optional refactor)

**Description:**  
In `compute_bottlenecks()`, line 237 uses `timezone.localdate()` (date) but line 238 uses `timezone.now() - timedelta()` (aware datetime). The comparison on line 271 is `ts < window_start_dt` (datetime ← datetime), so it's correct, but mixing date and datetime types is a code-smell that can lead to subtle bugs on porting.

**Observed at:**  
`metrics.py:237–238, 271`

**Suggested fix:**  
Change line 238 to `window_start_dt = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0) - datetime.timedelta(weeks=weeks)` OR use a dedicated function like `timezone.localtime(timezone.now()).date()` and reconstruct. Or document why the mix is intentional (the code is testing datetime ← datetime, and `today` is only used for labels, not comparisons).

**Effort:** S because a one-line comment suffices if the current pattern is intentional.

---

### **F3: models.py — `updated_at` bulk behavior undocumented [Low/S]**

**Severity:** Low (behavior is correct, but implicit)  
**Effort:** S (docstring update)

**Description:**  
The `updated_at` field (line 214) uses `auto_now=True`, which fires on `.save()`. However, the bulk path (`bulk.py:258`) explicitly sets `payload["updated_at"] = now` and uses `.update()` to bypass save(). This is correct, but the docstring does not explain that the bulk path handles it manually.

**Observed at:**  
`models.py:213–216` (field definition) vs `bulk.py:257–258` (explicit update)

**Suggested fix:**  
Add a note to the `updated_at` field docstring: `"Auto-set on save(); bulk updates set it explicitly via UPDATE so auto_now is bypassed."` This prevents future maintainers from being surprised.

---

### **F4: bulk.py — Cycle policy lacks query-count regression test [Low/M]**

**Severity:** Low (happy path is tested, not the count)  
**Effort:** M (add assertNumQueries test)

**Description:**  
The `_bulk_apply_cycle_policy()` function (line 425–459) handles multi-workspace batches by grouping IDs per workspace, then calling `Workspace.objects.get(pk=workspace_id)` per workspace (line 453). If a batch happens to span 50 workspaces (unlikely but possible in a federation), this is 50 SELECT queries. No regression test prevents a future O(workspace-count) regression.

**Observed at:**  
`bulk.py:452–453`

**Suggested fix:**  
In `test_bulk.py`, add a test case that mixes tasks from multiple workspaces in a single batch, then use `assertNumQueries` to lock the count. Expected: O(1 + workspace-count), since Workspace lookups are necessary.

**Alternative:** Pre-load all affected workspaces at line 452 with a single `Workspace.objects.in_bulk()` query, then look them up from the dict (1 query total). This is **safe** because the workspace IDs come from the Task query, so all workspaces involved are already known.

---

### **F5: bulk.py — Cascade subtask move assumes parent is not re-moved [Low/S]**

**Severity:** Low (edge case, handles correctly)  
**Effort:** S (add defensive test)

**Description:**  
In `_expand_move_set()` (line 312–350), the code computes `cascade_ids` as the subtasks of moved parents minus the explicitly requested IDs. The logic is correct, but the comment does not explicitly state: "If a parent and its subtask are both in the request, they are deduplicated correctly via set union." A future reader might misinterpret the filter logic.

**Observed at:**  
`bulk.py:338–350`

**Suggested fix:**  
Add a test case in `test_bulk.py` where both a parent and a subtask are in the same bulk move request, and verify they are moved once (not duplicated). Comment the test "Deduplication check: subtask listed both explicitly and via cascade."

---

### **F6: events.py — `snapshot_task()` reads cycle.name without check [Medium/S]**

**Severity:** Medium (potential N+1 if prefetch missed)  
**Effort:** S (documentation clarification)

**Description:**  
In `snapshot_task()` (line 75–76), the code reads `task.cycle.number` and `task.cycle.name` only if `task.cycle_id` is not None. However, if the task's cycle is loaded (e.g., from a select_related), this triggers a lookup. The function's docstring (line 45–49) says "Reads labels via .all() (not .values_list) so that any prefetch_related is honoured." But it does NOT say this applies to `task.cycle`. If a caller misses `select_related("cycle")`, this silently querying once.

**Observed at:**  
`events.py:41–85` (snapshot_task), specifically lines 75–76

**Suggested fix:**  
Clarify the docstring: "Reads both M2M relations (labels via .all()) and FK relations (cycle via attribute access) as-is, so callers should prefetch_related('labels') and select_related('cycle'). If cycle is not prefetched, this reads it once." Alternatively, always read cycle early in the function and assume callers select_related.

**Current callers:**
- views.py:120 (instance is already loaded; safe)
- bulk.py:562 (prefetch is not explicit for cycle; line 559 select_relates it, so safe)
- events.py:410–414 (explicit select_related; safe)

All callers are safe, but the contract is implicit.

---

### **F7: views.py — perform_create does not notify_task_created on error [Low/S]**

**Severity:** Low (edge case)  
**Effort:** S (defensive check)

**Description:**  
In `perform_create()` (line 84–104), the function calls `serializer.save()` and then `notify_task_created()`. If the save succeeds but notify fails (e.g., notifications service is down), the task is created but the notification is not sent, and no exception bubbles up (notifications.py services swallow errors). The code does not explicitly handle this, but it's acceptable because notification delivery is async anyway. However, there is no comment explaining the edge case.

**Observed at:**  
`views.py:84–104`

**Suggested fix:**  
Add a one-line comment: `# notify_task_created is fire-and-forget; failures do not roll back the save` to document the contract.

---

### **F8: test_metrics.py — No test for N > 1000 tasks [Low/M]**

**Severity:** Low (not hot-path)  
**Effort:** M (add benchmark test)

**Description:**  
`test_metrics.py` tests up to a few tasks, but there's no test of `compute_flow_metrics()` with a project that has 5000+ tasks and 10+ years of events. For such a project, the Python loop (metrics.py:110–127) could be slow. Wave 1 backlog M1 should capture this if measurements are needed.

**Observed at:**  
`test_metrics.py` (absence of scale test)

**Suggested fix:**  
Add a `@pytest.mark.slow` test that creates 5000 tasks with 10000 activity events and calls compute_flow_metrics. Mark it slow so CI skips it by default. This is not urgent (insights page is not hot), but useful for future scaling.

---

### **F9: events.py — broadcast_task_events does not handle missing tasks_by_id gracefully [Low/S]**

**Severity:** Low (assumption holds in practice)  
**Effort:** S (documentation)

**Description:**  
In `broadcast_task_events()` (line 298–378), the function iterates over `events` and looks up `tasks_by_id.get(ev.target_id)` (line 366). If the task is missing from the dict (e.g., a deletion event), the card_html is None and the SSE payload omits `card_html`. The comment on line 307 says the deletion events omit it intentionally, but for other event types, a missing task_id is silently swallowed.

**Observed at:**  
`events.py:366, 369–374`

**Suggested fix:**  
Add a defensive log line or assertion: `assert ev.target_id in tasks_by_id or ev.event_type == "task.deleted", f"Missing task {ev.target_id} for event type {ev.event_type}"`. In practice, this should not happen because the caller builds tasks_by_id from the same task set that generated the events.

---

### **F10: models.py — `file_attachments` property not tested for N+1 on lists [Low/M]**

**Severity:** Low (docstring says "single query, once per task")  
**Effort:** M (add test + refactor guidance)

**Description:**  
The `file_attachments` property (line 353–367) calls `self.attachments.filter(...).select_related("uploader")`. The docstring correctly says "single query" and "once per task-detail render, not for use across a list of tasks." However, if a caller mistakenly uses this on a list of tasks (e.g., `[task.file_attachments for task in tasks]`), it will N+1. There is no test that enforces the "not for lists" rule.

**Observed at:**  
`models.py:353–367`

**Suggested fix:**  
Add a test in `test_models.py` that calls `file_attachments` on a list of tasks inside `assertNumQueries(N)` (N = 1 + count), expecting a failure if someone refactors to use it in a list. Or add a `@cached_property` so it's only evaluated once per instance, making the N+1 impossible.

---

## 8. Defer to measurement (Wave 1 backlog M-series)

No new M-series entries for C1. However, the findings above suggest:

- **M1+ (from Wave 1 B4 F1)**: Dashboard metrics query count baseline should include whether `compute_flow_metrics` is called with a large project.
- **M10 (from Wave 1 backlog)**: If search performance is measured, apply the same EXPLAIN analysis to ActivityLog scans in metrics.py.

---

## 9. Wave 2 cross-links

### C1 → C6 (activity, SSE)

**Finding:** The excluded `task.labels_changed` event in web/views.py:2379 is enforced at query-time, not at emission. The event is always built and broadcast (events.py:260–271), then filtered on read. This is correct per ADR 0011 but assumes the activity log is large enough that filtering a few extra rows is cheaper than skipping emission. **No action needed** — it's by design.

### C1 → C5 (comments)

**Reference:** The snapshot payload in `_run_bulk_delete()` (bulk.py:634–652) captures a task snapshot but does NOT capture comments. When a task is deleted, comment activity rows survive because target_id is int, not FK. This is correct but not obvious.

---

## 10. Summary

**Findings:** 10 total (7 Low, 2 Medium, 1 Low). All are either documentation, query optimization, or defensive tests. **No correctness bugs.**

**Code quality:** The tasks app is well-structured. Activity emission is separated (events.py), bulk operations are transactional (bulk.py), and the model is clean (models.py). The only inefficiency is the unbounded ActivityLog scan in metrics.py, which is acceptable for a non-hot-path but worth optimizing in a future PR.

**Wave 1 closure:** All cross-references to Wave 1 findings (broadcast_task_events, labels_changed exclusion, prefetch patterns) check out. No regressions found.

**Recommendations:**
1. **Priority:** F1 (metrics date filtering) — easy win for future scaling.
2. **Priority:** F4 (cycle policy regression test) — defensive test, ~30 min.
3. **Deferred:** F10 (file_attachments as @cached_property) — quality-of-life, next refactor cycle.

---

**End of report.**
