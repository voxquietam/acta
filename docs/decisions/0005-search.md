# ADR 0005: Search and Filtering

**Status:** accepted
**Date:** 2026-05-15

## Context

Users need to find tasks: by free-text query (title/description) and by structured filters (status, assignee, labels, project, due date). Two viable approaches:

1. **ILIKE + structured filters** — Django ORM `__icontains` on title/description plus `__in`/equality filters on other fields. Zero infrastructure work.
2. **PostgreSQL full-text search (FTS)** — `SearchVector` + GIN index + a trigger or generated column for upkeep. Adds ranking and stemming, costs one migration and ongoing complexity.

Realistic data size for KSU24 in the first year: <10k tasks, most with short titles and modest descriptions.

## Decision

- **MVP search:** `ILIKE` (Django `__icontains`) across `Task.title` and `Task.description`.
- **MVP filtering:** structured filters by `status`, `assignee`, `labels`, `priority`, `size`, `due_date`, `project`. Built with `django-filter` on top of DRF.
- **No Postgres FTS in MVP.** Revisit when (a) dataset exceeds ~5k tasks with long descriptions, or (b) users complain about ranking/relevance.

## Why

- At <10k rows, `ILIKE '%term%'` over indexed columns runs in milliseconds. FTS overhead isn't justified by the data size.
- Skipping FTS keeps the schema and migrations simpler — no trigger, no generated column, no per-language config.
- Structured filters do the heavy lifting in a task tracker anyway; free-text search is a fallback, not the primary navigation.
- Migration to FTS later is mechanical: add a `SearchVector` field, populate it, swap the search query. No breaking changes for clients.

## Consequences

- No relevance ranking — results come back in whatever order the queryset specifies (default: most recently updated first).
- No stemming, no language-aware tokenization. "running" won't match "run". Acceptable trade-off for MVP.
- Trailing wildcard `ILIKE '%term%'` can't use a regular B-tree index. If performance regresses, consider a `pg_trgm` GIN index on title/description as a step before full FTS.
- Filter combinations are AND-ed by default; OR semantics across fields are not in scope.
