# 02 — workspaces app + helpers (C3)

> **Wave 2 / Chunk C3**. Date: 2026-05-29. Read-only audit.
> 
> Covers `apps/workspaces/` model layer, invite flow, DRF viewsets,
> permissions, and the five workspace-aware helpers Wave 1 B3 deferred.

---

## 1. Surface inventory

### Model layer (`apps/workspaces/models.py`)
- **Workspace** — tenant root; slug-scoped; config fields (auto_archive_done_after_days, wip_limits, cycle_settings, allow_member_announcements)
- **WorkspaceMember** — role-bearing through-table (OWNER / ADMIN / MEMBER); unique_together (user, workspace); joined_at tracking
- **WorkspaceInvite** — one-use token capability; email-bound; role-prebaked; 7-day TTL; single-use by `accepted_at` flip

### DRF viewsets (`apps/workspaces/views.py`)
- **WorkspaceViewSet** — CRUD; reads filtered by user membership; create seeding owner
- **WorkspaceMemberViewSet** — CRUD; list/read to members, write to admins/owners

### Permissions (`apps/workspaces/permissions.py`)
- **IsWorkspaceMember** — has_object_permission checks membership via `membership(user, workspace)`
- **IsWorkspaceAdmin** — owner/admin only; special `has_permission` hook for POST (workspace_id resolved from request.data)
- **IsWorkspaceOwner** — owner only
- **IsAuthorOrWorkspaceAdmin** — dual-layer (author wins, admin override)
- Helper: **workspace_of(obj)** — workspace FK resolver (direct, project.workspace, task.project.workspace, task via comment)
- Helper: **membership(user, workspace)** — single `WorkspaceMember.objects.filter(...).first()` call

### Services (`apps/workspaces/services.py`)
- **send_invite_email** — render + dispatch via Django mail backend; accepts optional request for absolute URL

### Serializers (`apps/workspaces/serializers.py`)
- **WorkspaceSerializer** — basic CRUD; `create` seeds owner-member in atomic txn
- **WorkspaceMemberSerializer** — role validator preventing non-owner from assigning owner role

### Tests (`apps/workspaces/tests/`)
- **test_invites.py** — 450 LOC; comprehensive invite lifecycle, email dispatch, signup flow, email-match claim, idempotency

### Migrations
- **0001_initial.py** — Workspace, WorkspaceMember with unique constraint, M2M through
- **0002_0005.py** — config fields (auto_archive, wip_limits, cycle_settings)
- **0006.py** — allow_member_announcements + cycle_settings help_text tweak

---

## 2. Models + invariants

### Workspace
```
name: CharField(120)                                    # Display name
slug: SlugField(60, unique=True)                        # URL anchor
owner: FK(User, PROTECT, → "owned_workspaces")          # Immutable; transfer requires explicit op
created_at: DateTimeField(auto_now_add)
members: M2M(User, through="WorkspaceMember")           # Declared for reverse access

# JSON config — all defaults to {} / None; normalization in getters
auto_archive_done_after_days: PositiveInt (null, default=30)
  Null = disabled. Setter: ~30 days old + status==done → auto-archive via daily task
  
wip_limits: JSONField (default=dict)
  {"mode": "off"|"personal"|"column", "limits": {status_key: int}}
  Getter: .wip_config() → (mode, limits_dict)
  
cycle_settings: JSONField (default=dict)
  {"enabled": bool, "length_weeks": int, "start_date": "YYYY-MM-DD", "auto_rollover": bool}
  Getter: .cycle_config() → normalized dict with length clamped 1..8 weeks
  
allow_member_announcements: BooleanField (default=False)
  When True, any member can broadcast; False = owner/admin only
```

**Invariants:**
- Owner is immutable by design (PROTECT on delete). Transfer requires a manual op (not in scope).
- `slug` is globally unique (site-wide scope, not per-user). Enforced by DB constraint.
- JSONField values are read-only after normalization — getters return computed values, no setters.
- `cycle_config()` returns `{"enabled": bool}` even when missing/disabled; consumers check enabled flag before using.

### WorkspaceMember
```
user: FK(User, CASCADE, → "workspace_memberships")
workspace: FK(Workspace, CASCADE, → "memberships")
role: CharField(max_length=10, choices=[OWNER, ADMIN, MEMBER], default=MEMBER)
joined_at: DateTimeField(auto_now_add)

Constraint: UNIQUE(user, workspace) — one row per membership per workspace
```

**Invariants:**
- Role is the sole permission vector; no other state (active/inactive/suspended). Revoke by row deletion.
- Once created, `joined_at` never changes (immutable audit field).
- A user can have exactly zero or one row per workspace; duplicate membership is impossible.
- Owner can never be demoted to admin/member (enforced by WorkspaceMemberSerializer.validate).
- **No soft-delete.** Row deletion = revocation. Audit trail lives in Activity rows.

### WorkspaceInvite
```
email: EmailField                                        # Invitee address (normalized lower-case)
workspace: FK(Workspace, CASCADE, → "invites")
role: CharField(choices=[ADMIN, MEMBER], default=MEMBER) # Never grants OWNER
token: CharField(64, unique=True)                        # secrets.token_urlsafe(32)
created_by: FK(User, SET_NULL)                           # Auditable; nil if sender deleted
created_at: DateTimeField(auto_now_add)
expires_at: DateTimeField(default=_default_invite_expiry) # now() + 7 days
accepted_at: DateTimeField(null=True)                    # Non-null = consumed
```

**Invariants:**
- Token is a one-use capability: `is_active ⟺ not is_expired ∧ not is_consumed`.
- Email is pre-normalized to lower-case on generate so case differences don't double-mint tokens.
- `accepted_at` flip is atomic with `WorkspaceMember` creation (`claim_invite_for_user` txn).
- No user FK; invite is email-addressed. Signup path matches by email (case-insensitive).
- **No soft-delete.** Revoke by deletion; resend by minting a fresh row.
- Expired or consumed tokens still exist in DB (audit trail); they simply fail the `is_active` check.

---

## 3. _workspace_* helpers map

All five helpers live in `/Users/voxquietam/Documents/REPOS/acta/apps/web/views.py:2473–2559`.

### `_workspace_members(task)` — lines 2473–2489

```python
def _workspace_members(task):
    """Workspace members ordered by username."""
    return (
        WorkspaceMember.objects.filter(workspace=task.project.workspace)
        .select_related("user")
        .order_by("user__username")
    )
```

**Queryset shape:**
- Base: `WorkspaceMember` filtered by `workspace_id`
- Join: `select_related("user")` (1 query + N user rows)
- **Total: 2 queries** (members + eager-loaded users)

**Callers:** 20 locations across inline edits, task detail, fragment endpoints.

**Issue F1 (per-request rebuild):** Each caller re-queries. Example:
- Line 2105 (TaskDetailView.get_context_data) — calls once
- Lines 2204, 2265 (task_meta_fragment, task_meta_compact_fragment) — each re-calls on SSE
- Line 3782 (set_task_assignee) — re-calls in HTMX response context
- Line 4842 (toggle_task_archived) — re-calls

**Scenario:** User edits task detail modal → 5 async fragment swaps (title, meta, timeline, comments, activity) → `_workspace_members` called 3+ times per request for same workspace.

### `_workspace_labels(task)` — lines 2509–2521

```python
def _workspace_labels(task):
    """Workspace labels in picker order (position, name)."""
    return Label.objects.filter(workspace=task.project.workspace).order_by("position", "name")
```

**Queryset shape:**
- **1 query** — no joins needed; position + name already on Label model
- Returns live queryset (not `list()`); will re-execute if template accesses it twice

**Callers:** 16 locations (same pattern as _workspace_members)

**Issue F2 (queryset vs list):** Returns queryset, not list. Template re-evaluation (e.g., {% for %}) may not re-query (Django's queryset caching), but HTMX swaps render independently so each fragment endpoint re-fetches fresh.

### `_workspace_label_groups(task)` — lines 2524–2531

```python
def _workspace_label_groups(task):
    """Labels organised by LabelGroup."""
    return grouped_labels(task.project.workspace)
```

**Delegates to:** `apps/labels/services.grouped_labels` (lines 41–79 of services.py)

```python
def grouped_labels(workspace):
    """One query; builds in-memory groups; returns list of dicts."""
    labels = list(
        Label.objects.filter(workspace=workspace).select_related("group").order_by("position", "name"),
    )
    # In-memory grouping logic
    by_group_id = {}
    for label in labels:
        gid = label.group_id
        if gid not in by_group_id:
            by_group_id[gid] = {"group": label.group, "labels": []}
        by_group_id[gid]["labels"].append(label)
    # Order: alphabetical named groups, then Ungrouped
    ...
```

**Queryset shape:**
- **1 query** — `Label` with `select_related("group")`
- In-memory sort + grouping (no additional DB calls)
- Returns list of dicts (memoizable)

**Callers:** 14 locations (same pattern)

### `_workspace_projects(task)` — lines 2492–2506

```python
def _workspace_projects(task):
    """Workspace projects ordered by name."""
    return Project.objects.filter(workspace=task.project.workspace).order_by("name")
```

**Queryset shape:**
- **1 query** (no joins; returns queryset)

**Callers:** 14 locations (same pattern)

### `_workspace_cycles(workspace)` — lines 2534–2558

```python
def _workspace_cycles(workspace):
    """Active + upcoming cycles; empty list if disabled."""
    if workspace is None or not workspace.cycle_config()["enabled"]:
        return []
    ensure_cycles(workspace)  # ← Side-effect: materializes rolling windows
    return list(
        workspace.cycles.exclude(status=Cycle.COMPLETED).order_by("status", "start_date"),
    )
```

**Queryset shape:**
- **0 queries** if disabled (returns `[]`)
- **1 query** if enabled (after ensure_cycles materializes)
- Side-effect: `ensure_cycles` may create new Cycle rows (mutates DB)

**Callers:** 14 locations (same pattern)

**Issue F3 (side-effect):** `ensure_cycles` can mutate the workspace. Called on every task detail / meta fragment render. Idempotent (only creates missing cycles) but adds latency on cold start.

---

## 4. Tenant scoping audit

### resolve_active_workspace (`apps/web/nav.py:23–65`)

Scopes all task/project/dashboard views to a single workspace.

```python
def resolve_active_workspace(request, members=None):
    """Resolve user's active workspace with per-request caching."""
    cached = getattr(request, _ACTIVE_WS_CACHE, "unset")
    if cached != "unset":
        return cached
    
    user = request.user
    if members is None:
        members = list(
            Workspace.objects.filter(memberships__user=user).order_by("name").distinct(),
        )
    
    active = None
    if user.active_workspace_id is not None:
        active = next((w for w in members if w.pk == user.active_workspace_id), None)
    
    if active is None:
        active = members[0] if members else None
        if active is not None and active.pk != user.active_workspace_id:
            user.active_workspace = active
            user.save(update_fields=["active_workspace"])
    
    setattr(request, _ACTIVE_WS_CACHE, active)
    return active
```

**Scoping contract:**
- Every paginated/keyed view (All Tasks, Kanban, Dashboard) calls `resolve_active_workspace(request)` to lock onto one workspace
- Follows `User.active_workspace_id` with fallback to first-by-name
- **Lazily updates User** if stored choice is stale (member was removed)
- **Per-request cache** on `request._acta_active_workspace` (memo key)

**Verification (F4):** ✓ Called on line 1263 (AllTasksView), plus dashboard init. Ensures `user.active_workspace_id` is consistent.

### WorkspaceMember membership check (F5)

Wave 1 PR-2 (`53b7924`) merged `WorkspaceMember.exists()` with `resolve_active_workspace`. 

**Status:** Not duplicated elsewhere. Two locations:
1. **`apps/workspaces/permissions.py:38–51`** — `membership(user, workspace)` helper
   ```python
   def membership(user, workspace):
       if not (user and user.is_authenticated and workspace):
           return None
       return WorkspaceMember.objects.filter(user=user, workspace=workspace).first()
   ```
   Called by permission classes; adds 1 query per permission check.

2. **`apps/accounts/views.py:453–456`** — `claim_invite_for_user` invokes `get_or_create` (atomic txn)
   ```python
   WorkspaceMember.objects.get_or_create(
       workspace=invite.workspace,
       user=user,
       defaults={"role": invite.role},
   )
   ```
   Called during signup; idempotent.

**Finding F6 (deferred to C5):** `IsWorkspaceAdmin.has_permission` checks membership at line 110 for create-new-member POST. The post-signin invite-accept flow (`accounts/views.py:453`) re-checks membership with `get_or_create`. No duplicate measurement needed here since both are necessary (POST auth, signup completion).

### Filter scoping in helpers

All five `_workspace_*` helpers **filter by `workspace=task.project.workspace`** or pass workspace directly. No cross-workspace data leakage path found.

**Verify per helper:**
- F7a: `_workspace_members` — filters `WorkspaceMember.objects.filter(workspace=task.project.workspace)` ✓
- F7b: `_workspace_labels` — filters `Label.objects.filter(workspace=task.project.workspace)` ✓
- F7c: `_workspace_label_groups` — calls `grouped_labels(task.project.workspace)` ✓
- F7d: `_workspace_projects` — filters `Project.objects.filter(workspace=task.project.workspace)` ✓
- F7e: `_workspace_cycles` — receives `workspace` arg, calls `workspace.cycles.exclude(…)` ✓

---

## 5. Views + permissions

### WorkspaceViewSet (lines 9–38, workspaces/views.py)

**Get queryset:**
```python
def get_queryset(self):
    return Workspace.objects.filter(memberships__user=self.request.user).distinct()
```
- **1 query + DISTINCT** — hits membership join; prevents duplicate rows if user has multiple roles (e.g., owner + invited as admin concurrently)
- Scoped: only user's own workspaces

**Permissions:**
- List/retrieve: `IsAuthenticated` + `IsWorkspaceMember` — any member can list
- Create: `IsAuthenticated` + `IsWorkspaceMember` — any user can create (seeded as owner)
- Update/delete: `IsAuthenticated` + `IsWorkspaceMember` — owner / members can access; serializer controls write

**Invariant:** User creating a workspace becomes its owner and gets a `WorkspaceMember(role=OWNER)` row in `perform_create` (serializer.create delegates this).

### WorkspaceMemberViewSet (lines 41–75, workspaces/views.py)

**Get queryset:**
```python
def get_queryset(self):
    return WorkspaceMember.objects.filter(
        workspace__memberships__user=self.request.user,
    ).distinct()
```
- **Scoped:** only memberships of workspaces the request user is a member of
- Returns all roles in user's workspaces (not just the user's own membership)

**Permissions:**
- List/retrieve: `IsAuthenticated` + `IsWorkspaceMember` — members can read all memberships in their workspace
- Create/update/delete: `IsAuthenticated` + `IsWorkspaceAdmin` — admins / owners only
  - `create` has special `has_permission` hook to resolve workspace_id from request.data before checking role

**Serializer validation:** `WorkspaceMemberSerializer.validate` prevents non-owners from assigning owner role (line 80–86).

### Page views (web/views.py)

**TaskDetailView (lines 2077–2114):**
```python
def get_context_data(self, **kwargs):
    ctx = super().get_context_data(**kwargs)
    task = self.object
    ctx["workspace_members"] = _workspace_members(task)
    ctx["workspace_labels"] = _workspace_labels(task)
    ctx["workspace_label_groups"] = _workspace_label_groups(task)
    ctx["workspace_projects"] = _workspace_projects(task)
    ctx["workspace_cycles"] = _workspace_cycles(task.project.workspace)
    return ctx
```
- Called once per page load
- Helpers re-fetch on each call; no caching

**Fragment endpoints (task_meta_fragment, task_meta_compact_fragment, etc.):**
- Lines 2199–2274
- Each re-calls the five helpers independently
- No request-level memo

**Finding F8 (reconstruction per-HTMX-swap):** The five helpers are rebuilt on every SSE fragment swap (meta, compact, comments). With 3–5 simultaneous edits on a workspace with 20+ members, 50+ labels, 10+ projects:
- **3 queries × 5 helpers × 5 swaps** = 75 queries per "simultaneous multi-cell edit" scenario
- Actually **much lower** in practice (workspace_members + labels + cycles cached by Postgres / Django queryset), but the **code pathway re-fetches on each request**.

---

## 6. Test coverage gaps

### Coverage present (test_invites.py, 450 LOC)

✓ Token generation + default TTL
✓ Active / expired / consumed state checks
✓ Anonymous visitor → signup flow
✓ Authenticated user with matching email → immediate membership (idempotent)
✓ Authenticated user with mismatched email → redirect (secure)
✓ Email normalization (case + whitespace stripping)
✓ Token single-use enforcement
✓ Serializer role-tier validation (admin can't assign owner)
✓ Web UI: create / revoke / resend invites
✓ Email dispatch + ACTA_PUBLIC_BASE_URL fallback

### Gaps (F9–F11)

**F9 — WorkspaceMember role coverage (low severity):** Tests do not verify:
- Promotion from member → admin (serializer allows it)
- Demotion from admin → member (serializer allows it)
- Owner cannot be demoted (serializer validates, but no test)
- DELETE on a WorkspaceMember row via API

**Suggested fix:** Add `test_promote_member_to_admin`, `test_cannot_demote_owner`, `test_delete_membership` in new `test_members.py`.

**F10 — Workspace config field round-trip (low severity):** Tests do not verify:
- JSON field values survive model save/load (wip_limits, cycle_settings)
- cycle_config() getter normalizes missing / invalid values (tested at model level, but no fixture verification)

**Suggested fix:** Add `test_cycle_config_normalization`, `test_wip_config_with_invalid_limits` in new `test_models.py`.

**F11 — Permission matrix coverage (medium severity):** No test explicitly verifies the role matrix from `docs/decisions/0010-permissions.md`:
- Owner can read/write all; admin can read/write except owner escalation; member can read only
- Current tests rely on serializer + permission class impl; no declarative matrix test

**Suggested fix:** Add `PermissionMatrixTests` with parameterized (user_role, action, resource) → expected_outcome table.

---

## 7. Findings F1..F11 (severity / effort / suggested fix)

| # | Finding | Severity | Effort | Fix | File:line |
|---|---------|----------|--------|-----|-----------|
| F1 | _workspace_members re-queried on every HTMX swap (3–5 times per multi-cell edit) | Medium | 2 h | Cache helpers on request object (`@cached_property` style) or bundle into TaskDetailView.get_context_data once | apps/web/views.py:2105–2109, 2204, 2265, 3782, 4842 |
| F2 | _workspace_labels returns queryset, not list; two re-renders on same page may re-query (benign in practice due to Django caching) | Low | 0.5 h | Wrap in `list()` for consistency; document no-op | apps/web/views.py:2509–2521 |
| F3 | _workspace_cycles calls ensure_cycles (side-effect: may create Cycle rows) on every render; idempotent but adds latency | Low | 1 h | Document side-effect in docstring; consider pre-materialization in background task | apps/web/views.py:2534–2558, apps/cycles/services.py |
| F4 | resolve_active_workspace fallback saves User if stale; no test of the write path | Low | 0.5 h | Add test: `test_resolve_active_workspace_updates_stale_choice` | apps/web/nav.py:57–63 |
| F5 | membership() called twice on signup: once in IsWorkspaceAdmin.has_permission (POST invite accept), once in claim_invite_for_user (get_or_create inside txn) | Low | Measurement | M-series: measure POST invite acceptance query count (Wave 1 PR-2 measurement deferred) | apps/accounts/adapters.py:28–52, apps/workspaces/permissions.py:38–51 |
| F6 | IsWorkspaceAdmin.has_permission resolves workspace_id from request.data; no validation that workspace actually exists before checking membership | Low | 0.5 h | Add null guard: `workspace = Workspace.objects.filter(pk=workspace_id).first()` is correct, but comment the risk | apps/workspaces/permissions.py:109–110 |
| F7a–F7e | All five helpers correctly scoped by workspace FK; no cross-workspace leakage | ✓ None | — | — | apps/web/views.py:2473–2558 |
| F8 | Five helpers re-fetched on every SSE fragment swap (task_meta, compact, comments); no per-request memoization | Medium | 2 h | Implement request-level cache (wrapper function or functools.lru_cache with workspace_id key) | apps/web/views.py:2199–2274 |
| F9 | WorkspaceMember role change tests missing (promote, demote, delete via API) | Low | 1 h | Add test_members.py with promotion/demotion/deletion scenarios | apps/workspaces/tests/ |
| F10 | JSON config field round-trip tests missing (wip_limits, cycle_settings) | Low | 1 h | Add test_models.py with config serialization fixtures | apps/workspaces/tests/ |
| F11 | No declarative permission matrix test; coverage relies on impl test (serializer + class) | Medium | 1.5 h | Add PermissionMatrixTests with parameterized (role, action, resource) → outcome table | apps/workspaces/tests/ |

---

## 8. Defer-to-measurement

Per Wave 1 backlog §4 (M-series), the following need dev-stack measurements:

| # | Item | Source | When ready |
|---|------|--------|-----------|
| M-F5 | Query count on POST `/api/accounts/invite/<token>/accept/` (before + after Wave 1 PR-2 merge) | F5 § Membership check re-query | After Wave 1 ships |
| M-F1 | Request query count on TaskDetailView (full page) vs sum of fragment endpoints (3 swaps) | F1 § Per-swap rebuild | Before Wave 3 optimization |
| M-F8 | Queryset cache hit rate: helpers called N times per request (current vs memoized) | F8 § Re-fetch cost | Before cache impl |

**Expected outcome:** F1 + F8 will likely show 30–50% query reduction with simple request-level caching.

---

## 9. Cross-links to C5, C9

### C5 (comments / reactions)
- **Overlap:** `_workspace_members` is used in `task_detail.html` comment threads for avatar lookup; comments/reactions could batch their member lookup with the page-level call.
- **Deferred:** Wave 1 B3 deferred `summarize_reactions` + `attach_reactions` batching. When audited, verify memo against `_workspace_members` cache key (both use workspace scope).

### C9 (web / sidebar)
- **Overlap:** `filter_sidebar_context` (234 LOC) renders labels sidebar; currently inlines `grouped_labels` call on every render.
- **Deferred:** C9 audit will recommend memoise or refactor. Could reuse the F1/F8 per-request cache pattern if implemented here first.

### Delegation from Wave 1 backlog §6

**W2 (this chunk):** "Audit `_workspace_members`, `_workspace_labels`, `_workspace_label_groups`, `_workspace_projects`, `_workspace_cycles` — input from B3 (deferred F5 evaluation)."

This chunk completes that backlog item: F1, F2, F3, F8 identify optimization opportunities; F4–F11 are test coverage / validation gaps.

---

## 10. Summary of findings

**8 findings, 11 sub-items:**

1. **High-value optimization (F1 + F8):** Per-request caching of five workspace helpers would eliminate redundant queries on multi-cell HTMX edits. **Estimated win:** 30–40 queries / page in heavy-edit scenarios. **Effort:** 2–3 hours including tests.

2. **Low-friction test coverage (F9–F11):** Three test files adding ~50 LOC total. Covers role matrix, config round-trip, and DELETE path. **Effort:** 2.5 hours.

3. **Documentation + safe paths (F4–F6):** Small comment / test additions clarifying resolve_active_workspace fallback write, ensure_cycles side-effect, and permission-check workspace validation. **Effort:** 1 hour.

**Total suggested effort for C3:** ~8.5 hours (optimization + tests + docs). **Priority:** F1/F8 are medium-severity (observable in high-concurrency edits); F9–F11 are low-severity coverage gaps.

