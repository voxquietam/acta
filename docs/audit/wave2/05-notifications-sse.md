# 05 — notifications + SSE (C7)

> Wave 2 / Chunk C7. Date: 2026-05-29. Read-only.
> Inputs: `apps/notifications/`, `apps/tasks/events.py`,
> `apps/activity/services.py`, `apps/workspaces/sse.py`, `apps/telegram/`,
> `static/js/acta.js`. No code changed.

---

## 1. Surface inventory

**Notifications fan-out:**
- `apps/notifications/services.py` (470 LOC).
- `apps/notifications/models.py` (179 LOC).

**SSE event emission:**
- `apps/tasks/events.py` — `broadcast_task_events()` lines 298–378.
- `apps/activity/services.py` — `broadcast_event()` lines 25–50,
  `log_event()` lines 53–128.

**SSE auth & channel management:**
- `apps/workspaces/sse.py` (55 LOC, `WorkspaceChannelManager`).
- `acta/urls.py` — SSE endpoint routes lines 51–63.

**JS consumers:**
- `static/js/acta.js` lines 2050–2466 (workspace SSE + task handlers),
  2468–2539 (user notification stream).

**Telegram fanout:**
- `apps/telegram/services.py` (498 LOC).
- `apps/telegram/models.py` (128 LOC).

**Test coverage:**
- Notifications: `test_sse.py` (57 LOC, 2 tests), `test_fanout.py`
  (470 LOC, 15+ tests), `test_inbox.py`, `test_models.py`.
- Telegram: `test_outbound.py` (15+ tests), `test_linking.py`,
  `test_seed_templates.py`, `test_set_webhook_command.py`.

---

## 2. Notifications fanout architecture

**Persistence vs. broadcast:**
- `notify()` (services.py:45–104) is the single writer. It creates a
  persistent `Notification` row and queues SSE broadcast + Telegram via
  `transaction.on_commit()`.
- Broadcast is deferred: `_broadcast_notification()` (services.py:155–194)
  re-fetches the notification on commit with
  `select_related("task__project", "actor", "comment",
  "project_update__project")` and renders pre-templated HTML for inbox
  row + badge.
- Telegram fanout via `_mirror_to_telegram()` (services.py:107–120) uses
  lazy import to avoid module-load cycle.

**Denormalization & re-denorm triggers:**
- `preview` field: stored as truncated markdown (280 chars max via
  `_truncate_preview()`, lines 14–30). Sources:
  - Assigned: task description (line 232).
  - Status / priority / due changes: task title (line 235).
  - Comments & mentions: comment body (line 372).
  - Project updates: update body (line 416).
  - Announcements: announcement body (line 449).
- `payload` field: event-specific JSON mirroring the underlying
  `ActivityLog.payload`. Persisted unchanged in all fan-out paths
  (lines 100, 245, 326, 396, 459). No re-denorm on edit — becomes
  stale if the source activity is altered. **Acceptable** because
  the activity log is append-only.

**Null-safety on deletion:**
- `SET_NULL`: `actor` (42), `task` (61), `comment` (69), `activity` (77),
  `project_update` (85).
- `recipient` (36): `CASCADE` (a deleted user loses their notifications).
- `workspace` (50): `CASCADE` (deleting a workspace voids its
  notifications).
- Impact: a task deletion leaves the notification row with
  `task_id=None`, but `preview` + `payload` survive for the inbox list
  and detail view.

**Query patterns (N+1 check):**
- `notify_for_task_diff()` (services.py:197–246): single event loop,
  per-event recipient loop. `notify()` issues 1 INSERT per recipient.
  No queries inside the loop after line 313. **Safe.**
- `notify_mentions()` (services.py:291–328): **single**
  `WorkspaceMember.objects.filter()` (line 314) to validate members,
  then iterates recipients. **No query inside the recipient loop
  (line 318).**
- `notify_comment_created()` (services.py:356–397): loads `comment.task`,
  then loops recipients. **No extra queries inside the loop.**
- `notify_project_update_created()` (lines 400–426): single
  `WorkspaceMember.objects.filter()` upfront (line 417), then loops.
  **Safe.**
- `notify_announcement()` (lines 429–469): single
  `WorkspaceMember.objects.filter()` (line 450), then loops.
  **Safe.**
- **Verdict: no per-recipient query loops.**

---

## 3. broadcast_task_events payload vs. JS handlers — drift table

The headline artifact for C7. Per-event-type rows comparing what Python
sends (`apps/tasks/events.py`) vs what JS reads (`static/js/acta.js`).

| Event type | Python emit (file:line) | JS handler (file:line) | Payload fields | Drift risk |
|---|---|---|---|---|
| `task.status_changed` | events.py:142–147 — `payload={"from","to"}` | acta.js:2307–2314 — reads `d.to`, `d.card_html`, `d.row_html_table` | `from` (unused), `to`, `card_html`, `row_html_table` | ✓ **Safe** — `to` key matches; `applyCardMove(d.target_id, d.to, d.card_html)` |
| `task.assigned` | events.py:150–159 — `payload={"from_user_id","to_user_id"}` | acta.js:2315 → `applyTaskUpdate(d)` (line 2295) | `from_user_id`, `to_user_id`, `card_html`, `row_html_table`, `row_html_list` | ✓ **Safe** — user IDs stored but JS never reads them; generic card replace |
| `task.priority_changed` | events.py:198–204 — `payload={"from","to"}` | acta.js:2316 → `applyTaskUpdate` | `from`, `to`, html surfaces | ✓ **Safe** — `to` integer; JS ignores, applies card HTML swap |
| `task.due_changed` | events.py:162–171 — `payload={"from","to"}` ISO | acta.js:2317 → `applyTaskUpdate` | ISO date strings, html surfaces | ✓ **Safe** — payload informational |
| `task.labels_changed` | events.py:260–270 — `payload={"added_ids":[…],"removed_ids":[…]}` | acta.js:2318 → `applyTaskUpdate` | `added_ids`, `removed_ids` (unused in JS), html surfaces | ✓ **Safe** — JS uses card HTML only. **Note:** event is excluded from `_task_activity` whitelist (Wave 1 W4); broadcast still fires. Intentional |
| `task.updated` | events.py:286–292 — `payload={"changes":{"title","description","size"}}` | acta.js:2433–2438 — reads `d.changes` | `changes` object with `old/new` or length fields | ✓ **Safe** — JS selectively refreshes (`if changes.title`, `if changes.description`) |
| `task.archived` / `task.unarchived` | events.py:252–257 — `payload={}` | acta.js:2320–2321 → `applyTaskUpdate` | empty payload + html surfaces | ✓ **Safe** |
| `task.project_changed` | events.py:219–230 — `payload={"from_project_id","to_project_id","from_slug","to_slug"}` | acta.js:2323–2335 — calls `applyCardRemove(d.target_id)` | slug + project IDs (informational), `card_html`, `row_html_table` | ✓ **Safe** — JS removes card |
| `task.deleted` | emitted via bulk.py (no diff) | acta.js:2354–2357 → `applyCardRemove(d.target_id)` | no `card_html` (task gone), `target_id`, `project_id`, `bulk_id` | ✓ **Safe** |
| `task.created` | activity/services.py:118 — broadcast_extras with `html_kanban` + `status` | acta.js:2373–2384 — reads `d.html_kanban`, `d.status` | `html_kanban` (pre-rendered card), `status` (column key) | ✓ **Safe** — uses `html_kanban` (NOT `card_html`); JS matches |
| `task.link_added` / `task.link_removed` | events.py:430–471 `broadcast_link_change()` | acta.js:2338–2352 — `source.addEventListener` (not `handle`), reads `d.target_id` | `card_html`, `row_html_table`, `row_html_list` | ✓ **Safe** — generic update |
| `comment.created` / `updated` / `deleted` | activity/services.py log_event | acta.js:2447–2458 — reads `d.task_id` to refresh timeline | `task_id`, other activity fields | ✓ **Safe** |
| `notification.created` | services.py:192 — broadcasts on `user-<id>` channel | acta.js:2478–2538 — reads `d.kind`, `d.workspace_id`, `d.unread`, `d.badge_html`, `d.row_html` | `kind`, `workspace_id`, `unread`, `badge_html`, `row_html` | ✓ **Safe** — scoped to active workspace (acta.js:2489) |

**Drift summary:** **no renames found.** All JS reads match Python
emits. The single naming-difference (`html_kanban` vs `card_html`) is
intentional and consistent: `html_kanban` for newly-created cards
(needs a column key, no row variants); `card_html` for updates (carries
all three surfaces).

---

## 4. Inbox model + pagination

**Ordering & query:**
- `Notification.Meta.ordering = ["-created_at"]` (models.py:141).
- `_inbox_base_qs()` (web/views.py:868) applies same order:
  `.order_by("-created_at")` + `select_related("task__project",
  "actor", "comment")` (lines 875–879).
- Indexes: `Meta.indexes` include `["recipient", "-created_at"]`
  (models.py:138). Efficient for the main query.

**Pagination:**
- `_inbox_base_qs()` excludes `kind=PROJECT_UPDATE` (line 874) —
  project updates surface only in the Updates tab (web/views.py:852–859).
  Exclusion is mirrored in `_unread_count()` (services.py:150).
- `_inbox_filtered_qs()` (web/views.py:884–900) applies optional kind
  filter for chips (mentions, assigned, comments).
- Badge scoping: `inbox_unread_count()` (web/views.py:925–948) scoped
  to `user.active_workspace_id`; `_broadcast_notification()` re-fetches
  unread count and includes it in SSE payload. Badge stays in sync.

**Mention escalation logic:**
- `notify_mentions()` (services.py:291–328) validates mentioned user
  IDs against `WorkspaceMember` (single query). Self-actor suppressed
  by `notify()` (line 88).
- `notify_comment_created()` (services.py:356–397): mentions get
  `MENTION` kind (higher precedence); assignee / reporter get `COMMENT`
  kind. Mentioned recipients are excluded from the duplicate COMMENT
  (line 387: `involved -= mentioned`).
- `notify_description_mentions()` (services.py:331–353): diffs
  old / new text to avoid re-notifying already-mentioned users on
  re-save.

---

## 5. Telegram fanout

**Regex placeholders for template substitution:**
- `_PLACEHOLDER_RE` (services.py:29): `r"\{(\w+)\}"` matches `{key}`.
- `_render_template()` (services.py:412–418): regex-substitutes
  placeholders; unknown placeholders left as-is (safe against crashes).
- Available placeholders in `_template_context()` (services.py:351–409):
  `actor`, `slug`, `task`, `title`, `preview`, `quote`, `priority`,
  `due`, `meta`, `status`, `status_from`, `status_to`, `status_change`,
  `priority_from`, `priority_to`, `priority_change`, `due_from`,
  `due_to`, `due_change`, `project`, `health`, `cycle`, `headline`.
- Custom templates via `TelegramMessageTemplate` (models.py:54–92) allow
  admin to override per-kind (one row per kind). Defaults fallback to
  built-in phrasing.

**Mute config:**
- `TelegramAccount.muted_kinds` (models.py:34): JSONField list of
  `Notification.Kind` values.
- `notify_via_telegram()` (services.py:476–497): skips send if kind is
  muted (line 492), **except** `ANNOUNCEMENT` which is force-delivered
  (line 480–481). Correct per spec — announcements cannot be muted.

**Quiet hours + digest gap:**
- **NOT IMPLEMENTED.** `notify_via_telegram()` sends immediately on
  every notification. No quiet hours, no digest batching.
- `TelegramAccount` has no `quiet_hours` or `digest_enabled` fields.
- **Feature gap, not a bug.** Memory `[[project-todo-telegram-quiet-hours]]`
  is open. No fix proposed per scope. **F3 below documents the surface
  area for a future PR.**

---

## 6. SSE endpoint + auth

**Endpoint routing:**
- `/events/workspace/<workspace_id>` (acta/urls.py:51–58): includes
  `django_eventstream.urls` with `format-channels=["workspace-{workspace_id}"]`.
- `/events/user/<user_id>` (acta/urls.py:59–63): private per-user channel
  for notifications.
- Both use `WorkspaceChannelManager` (workspaces/sse.py).

**Session / user auth:**
- `WorkspaceChannelManager.can_read_channel()` (sse.py:28–54):
  - `user-<id>` channel: readable only by the matching user
    (line 44: `target_user_id == user.id`).
  - `workspace-<id>` channel: readable only by members
    (lines 51–54: `WorkspaceMember.objects.filter(user=user,
    workspace_id=workspace_id).exists()`).
  - Anonymous or non-member requests: return `False` → 403.
- **Per-connection auth:** `django_eventstream` invokes
  `can_read_channel` on initial connect AND every event filter pass.
  A revoked membership terminates the stream on the next poll.

**Single-flight per user:**
- `initOneWorkspaceSse()` (acta.js:2180–2188) uses dedup:
  `SSE_BOUND_URLS` (line 2168) tracks bound channel URLs. Multiple
  `[data-workspace-sse]` markers for the same workspace reuse the same
  EventSource — only one connection per workspace per page.
- Per-user stream: `USER_SSE_BOUND` (acta.js:2475) similarly dedupes.

**Reconnect strategy:**
- `new EventSource(url)` (acta.js:2190): browser-built-in EventSource
  handles reconnect with exponential backoff.
- **Manual close on navigation:** acta.js:2196–2204 — `pagehide` and
  `beforeunload` listeners call `source.close()` to cleanly terminate
  the stream (avoids half-open connections blocking graceful uvicorn
  shutdown).

**Production note (ADR 0015):** Uvicorn/ASGI. `django_eventstream` runs
as a view returning `StreamingResponse`. No special kernel tuning
needed — ASGI handles many concurrent streams.

---

## 7. Test coverage gaps

**Notifications tests (4 files):**
- `test_sse.py` (57 LOC, 2 tests): tests `notify()` broadcasts to
  `user-<id>` channel, checks payload shape (kind, unread, row_html,
  badge_html). **Gap: does not test re-fetch on commit race.**
- `test_fanout.py` (470 LOC, 15+ tests): exercises task / comment /
  project-update fan-out, recipient resolution, self-suppression.
  Tests labels (intentionally don't notify), mention parsing.
  **Comprehensive for business logic.**
- `test_inbox.py`: inbox queries, filters, pagination, mark-read /
  unread, archive. **Good coverage.**
- `test_models.py`: basic model tests.
- **Coverage summary:** fan-out logic well-covered. SSE broadcast HTML
  rendering not tested.

**Telegram tests (4 files):**
- `test_outbound.py` (15+ tests): send for linked/enabled, mute config,
  announcements (force-deliver ignoring mute), clean preview (markdown
  stripping, truncation), placeholder rendering. **Comprehensive.**
- `test_linking.py`: tests `/start <token>` flow, link reuse, expiry.
- `test_seed_templates.py`: seed templates.
- `test_set_webhook_command.py`: webhook setup.
- **No quiet-hours test (feature not implemented).**

**Missing test scenarios:**
- T1: SSE broadcast HTML rendering (no test that `row_html` /
  `badge_html` actually renders without errors when preview is unusual).
- T2: Race condition: SSE broadcast re-fetches on commit; concurrent
  delete returns None — `_broadcast_notification()` line 182–183
  checks `if row is None: return` (silent skip). No test.
- T3: Large fan-out performance — `notify_announcement()` loops all
  workspace members. No test with 1 000+ member workspace.
- T4: Placeholder validation — admin can enter `{statua}` (typo); it's
  left as-is in the DM. No admin-form validator, no test.

---

## 8. Findings

### F1 — Preview denormalization is write-once `[Sev: low / Eff: M]`

Preview is computed at notification creation (comment body or task
title/description) and never updated. If the source is edited, the
inbox row's preview goes stale.

- **services.py:14–30, 99, 232, 235, 372.**
- **Suggested fix:** document the design choice with a one-line comment
  at `_truncate_preview()`; add `T1` test that exercises a "title
  edited after notification fired" path and asserts the preview holds
  the original snippet. **Do not** add a retro-update mechanism — it
  would require polling or a separate update event and the user-visible
  drift is acceptable.

### F2 — `broadcast_task_events()` pre-renders three HTML surfaces per task `[Sev: low / Eff: L]`

For each affected task: card HTML + table row HTML + list row HTML. In
a bulk update (e.g., 100 tasks), this is 300 renders. Trade-off per
ADR 0014: server overhead vs zero client-side HTTP fetches.

- **events.py:342–346.**
- **Suggested fix (defer):** measure with M-series before optimizing.
  Candidate optimizations:
  - lazy render — emit only the surface for the panel the recipient
    is currently viewing (requires recipient context, breaks broadcast
    semantics);
  - shared sub-template caching — `_task_card.html` ages well via
    template-fragment cache.
  - Bundle for Wave 2 / R-list, after M-measurement confirms cost.

### F3 — Telegram quiet hours not implemented `[Sev: low / Eff: L]`

`TelegramAccount` has `muted_kinds` but no quiet-hours fields. Sends
fire immediately regardless of time of day. Memory
`[[project-todo-telegram-quiet-hours]]` is open.

- **models.py:34; services.py:476–497.**
- **Suggested fix (future, not Wave 2):** add `quiet_hours_start` +
  `quiet_hours_end` + `digest_enabled` to `TelegramAccount`; defer
  `notify_via_telegram()` to a scheduled batch job during quiet
  windows; persist pending notifications to a queue (django-q already
  available).

### F4 — Unread count re-computed on every broadcast `[Sev: low / Eff: M]`

`_unread_count()` (services.py:123–152) queries the database on every
`notify()`. In a high-volume fan-out (mention to 50 members), this runs
50 times.

- **services.py:184.**
- **Suggested fix:** introduce a `cached_unread_count(user, workspace)`
  helper that memoizes within the broadcast batch (single
  `transaction.on_commit()` callback computes once and reuses).
  Defer until measurement confirms the redundant queries matter on a
  populated workspace.

### F5 — Placeholder regex does not validate token format `[Sev: low / Eff: S]`

`_render_template()` regex-substitutes any `{key}` token. Admin-entered
templates can have typos (e.g., `{statua}` instead of `{status}`) and
they're left as-is in the DM. No validation in the admin form.

- **services.py:412–418; models.py:63–68.**
- **Suggested fix:** add a `clean()` method on `TelegramMessageTemplate`
  that walks `_PLACEHOLDER_RE`, validates each token against a
  known-good set per-kind, raises `ValidationError` for typos. Add an
  admin "Preview" button that renders against a dummy context.

### F6 — List panel refetch debounced but not coalesced `[Sev: info / Eff: M]`

On any task update, `refreshListPanel()` sets a 250 ms debounce timer.
Multiple updates within 250 ms queue only one refetch. However, **each
event type that calls `refreshListPanel()` is independent** and the
timer is cleared then re-set on each call. In a rapid sequence of
different event types, the 250 ms timer keeps restarting.

- **acta.js:2257–2272.**
- **Note:** acceptable; debounce prevents thrashing. Coalescing would
  be a micro-optimization. Same observation cross-links with Wave 1
  `project_todo_list_view_promote_chip_speed`.

### F7 — Broadcast re-fetch race silently skips `[Sev: info / Eff: S]`

On `transaction.on_commit()`, `_broadcast_notification()` re-fetches.
If deleted in another transaction between creation and commit,
`row is None` and broadcast silently skips. **Correct fallback, no
error, no log.**

- **services.py:178–183.**
- **Suggested fix:** add `logger.debug(...)` so the silent skip is
  observable in operational logs (otherwise indistinguishable from a
  no-recipient case).

### F8 — `notify_for_task_diff` assignee resolution is correct but undertested `[Sev: low / Eff: S]`

When unassigning a task, the previous assignee gets a STATUS_CHANGE
notification (if status also changed) but not an ASSIGNED one (correct —
they weren't newly assigned). When only status changed, both
assignee + reporter get STATUS_CHANGE. Verified in `test_fanout.py:53`
but no unassign-without-status-change path is tested.

- **services.py:226–235.**
- **Suggested fix:** add a parametrized test covering 4 cells:
  unassign-only, status-only, both, neither.

### F9 — No factory for `Notification` / `TelegramAccount` `[Sev: low / Eff: S]`

Tests construct notifications inline. Future perf tests (T3 above)
would benefit from a factory.

- **`apps/notifications/tests/` — no `factories.py`.**
- **Suggested fix:** add `NotificationFactory` and
  `TelegramAccountFactory` modeled on `apps/tasks/tests/factories.py`.
  Same gap noted on `activity` in C6 §6 — bundle as a one-PR
  factories-everywhere sweep in Wave 3.

### F10 — SSE event filter re-runs on every poll — by design `[Sev: none]`

`django_eventstream` calls `can_read_channel()` on initial connect AND
on every outgoing event. A user who loses workspace membership
mid-stream is disconnected on the next event filter pass. This is the
correct behavior — no change needed; documenting so a future
optimization PR does not break the enforcement.

- **sse.py:1–2, 28–54.**

---

## 9. Defer-to-measurement / future

- **M (new) — Fan-out cost on a 100-recipient workspace.** Currently
  no workspace in the corpus is wide enough to stress `notify_announcement`.
  Build a 100-member factory scenario in a perf test, count queries +
  wall-time. Goal: confirm `WorkspaceMember.objects.filter()` (line 450)
  + N inserts is the only cost.
- **M (new) — Concurrent broadcast vs delete race.** Targeted test
  that creates + deletes a notification within the same
  `transaction.on_commit()` window and asserts no error.
- **Quiet hours for Telegram** — F3 above. Feature gap. Memory
  `[[project-todo-telegram-quiet-hours]]` is open.
- **Unread count caching** — F4 above. Optimization, not bug.
- **Placeholder validation in Telegram templates** — F5 above.

---

## 10. Cross-links

- **C1 (tasks):** `events.py` is the event source. Any new task field
  emission must be added to `WATCHED_EVENT_FIELDS` (tasks/events.py).
  Drift check on payload shape ✓ in §3 above.
- **C5 (comments):** comments-created broadcast reads `task.id` and
  refreshes timeline. No drift; serializer / fanout paths separate.
- **C6 (activity):** `broadcast_event()` lives in `activity/services.py`
  and is used by both `log_event` and direct broadcast paths
  (`broadcast_link_change`). The decision to bypass `log_event` for
  link mutations is intentional and documented (C6 §F5).
- **Wave 1 PR-4 (`actaForceApplySelfEvent` opt-in):** verified every
  task-event broadcast path goes through the JS handler that respects
  the self-filter. No bypass found. ✓
- **Wave 1 PR-5 (30 s TTL bump):** TTL applies to the self-event
  suppression window in `acta.js`, not the SSE persistence layer.
  Notifications persist in the inbox indefinitely (archived on user
  action). No conflict.
- **Wave 1 critical bug `project_todo_alpine_xshow_drift_kanban_body`:**
  workaround (`snapCollapsedBodies` + per-body MO at acta.js:2090–2108)
  is shipped and functioning. **Not proposed for removal.** Root cause
  remains untraced — Alpine store transient state during mutation.
- **Wave 1 PR-8 revert `project_todo_sse_kanban_substatus_wire`:**
  off-limits per scope. Re-attempt only after the drift root cause is
  understood.

---

**Summary.** C7 is well-structured with clean separation of persistence,
broadcast, and fanout. Null-safety is sound, query patterns are
efficient (no per-recipient N+1), and test coverage is strong for core
fan-out logic. SSE auth is tight and revocation-aware. Telegram has no
quiet hours (feature gap, not a bug). **No drift between Python and
JS event payloads.** The Wave 1 kanban-collapse workaround is stable
and must not be removed.

10 findings, none P0. Total Wave 2 effort attributable to C7: ~6 h
spread over 3-4 small PRs (F5 admin validator, F7 debug log, F8
parametrized test, F9 factories). F3 (quiet hours) is a feature, not
an audit fix.
