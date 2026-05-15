# ADR 0013: API Conventions

**Status:** accepted
**Date:** 2026-05-15

## Context

The HTTP API is consumed primarily by Acta's own frontend, but should be reasonable to call from external scripts (e.g. ksu24.back integrations) without surprises. A few low-stakes conventions, chosen up front, prevent inconsistency later.

## Decisions

### Versioning

- All endpoints live under `/api/v1/...`.
- A future breaking change moves to `/api/v2/...`; `/api/v1/` keeps working until removed.

### URL style

- Resource collections are pluralized: `/api/v1/tasks/`, `/api/v1/projects/`, `/api/v1/workspaces/`.
- Trailing slash required (Django default; `APPEND_SLASH = True`).
- Nested routes for parent-scoped collections where it reads better:
  - `/api/v1/projects/{id}/updates/` — project updates
  - `/api/v1/tasks/{id}/comments/` — comments on a task
- Bulk endpoints: `/api/v1/tasks/bulk/` (see [0012](0012-bulk-operations.md)).

### JSON shape

- **Snake_case keys** in both request and response bodies. No camelCase translation layer.
- **ISO 8601** for all date/datetime values:
  - `DateField` → `"2026-06-01"` (no time, no timezone).
  - `DateTimeField` → `"2026-05-15T13:45:00Z"` (always UTC, `Z` suffix; never local).
- **IDs** are integers (`int`), serialized as JSON numbers.
- **Enums** are short lowercase strings (`"in-progress"`, `"on_track"`). Internal storage may be int (e.g. `priority`) but the API exposes a string identifier alongside or instead of the int — to be decided per field in `spec/api.md`.

### Pagination

- DRF `LimitOffsetPagination` on all list endpoints.
- Query params: `?limit=50&offset=0`. Default `limit=50`, max `limit=200`.
- Response envelope:
  ```json
  {
    "count": 1234,
    "next": "/api/v1/tasks/?limit=50&offset=50",
    "previous": null,
    "results": [...]
  }
  ```

### Filtering, search, ordering

- DRF + `django-filter` for structured filters: `?status=in-progress&assignee=5&project=12`.
- Free-text search via `?search=...` against `title` and `description` (see [0005](0005-search.md)).
- Ordering: `?ordering=-updated_at` (DRF `OrderingFilter`). Default ordering per resource is most-recently-updated-first.

### Error format

- DRF defaults:
  - **Field validation errors:** `{"field_name": ["error message", ...], ...}` with HTTP 400.
  - **Non-field errors:** `{"detail": "human-readable message"}` with the appropriate status (400, 403, 404, 409, etc.).
  - **Bulk endpoints:** `{"errors": [{"id": 101, "field": "status", "reason": "..."}, ...]}` for validation failures, per [0012](0012-bulk-operations.md).

### Authentication

- DRF `SessionAuthentication` is the only auth backend in MVP. Browser logs in via Google OAuth (see [0002](0002-auth.md)), session cookie is sent on subsequent API requests.
- No tokens, no JWTs, no API keys in MVP. External scripts that need to call the API run in a Django shell or via session cookie copy — acceptable for an internal tool.

### CSRF

- Standard Django CSRF protection for state-changing requests from the browser. DRF's `SessionAuthentication` enforces CSRF.
- Frontend sends `X-CSRFToken` header from cookie on POST/PATCH/PUT/DELETE.

### Rate limiting

- None in MVP. Internal tool, small team. Add DRF throttling later only if a runaway script becomes a real problem.

## Why

- **`/api/v1/` prefix** is one of the cheapest insurance policies in API design. Skipping it forces a painful path migration when v2 arrives.
- **Snake_case** removes a serialization layer and matches Python attribute names — fewer surprises in views and ORM filters.
- **ISO 8601 UTC** sidesteps timezone bugs at the wire format. The frontend can localize for display.
- **Offset pagination** is plenty for ~10k tasks; cursor pagination is overkill and worse UX for "jump to page 5" patterns.
- **DRF error format** is what every Python client library already understands; no need to invent RFC 7807 right now.
- **Session auth only** removes a class of complexity (token rotation, revocation, expiry) that isn't relevant when every consumer is a browser tab.

## Consequences

- A v2 migration eventually requires running both versions in parallel — standard cost, well understood.
- External scripts wanting programmatic access need session cookie or a Django management command path. If demand grows, add `rest_framework_simplejwt` or `Token` auth — a single-day change.
- No rate limit means a buggy client can hammer the server. Acceptable on an internal LAN-scale deployment; document the mitigation path.
