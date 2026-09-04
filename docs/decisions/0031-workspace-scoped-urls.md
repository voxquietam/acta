# ADR 0031: Workspace-scoped URLs

**Status:** accepted
**Date:** 2026-09-04

## Context

A project's `slug_prefix` is unique **within a workspace**, not globally —
`projects_unique_workspace_slug_prefix` scopes it that way, so two workspaces
may each own a `SER` project. The URL carries no workspace:

```
/projects/SER/2/
```

For anyone who belongs to both workspaces, that URL names two different
tasks. Django's `.get()` raised `MultipleObjectsReturned` and the page
answered **500** — on an ordinary click, only ever for people in more than
one workspace. The project page (`/projects/SER/`) failed the same way.

Silently preferring one candidate is worse than the crash, not better: a
link shared in chat would open a *different* task for the recipient than for
the sender, with nothing on screen to say so.

Measured on dev, 2026-09-04: 2 colliding prefixes (`HRW`, `OPS`, plus `SER`
on prod), 93 tasks under them, 1 of 16 active users exposed. Small today,
and it grows with every workspace added.

Acta has been in daily use for roughly six months, so URLs have escaped into
places we cannot rewrite: Telegram notifications, browser bookmarks, chat
messages, external documents, and MCP tool responses. Inside Acta itself
stored links are almost absent (1 in task descriptions, 0 in comments), so
the constraint is not migrating our own text — it is that **every URL ever
issued must keep working**.

## Decision

**Every workspace-specific section moves under the workspace slug**, the
shape Linear and GitHub use:

```
/KSU24/projects/SER/2/
/KSU24/projects/SER/
/KSU24/inbox/
/KSU24/my-work/
```

Two alternatives were rejected:

- **Globally unique prefixes.** By far the cheapest — the URL would not
  change at all — but it means two organisations can never both own a `DEV`
  project. Acta is meant to host independent organisations, so this trades a
  one-off fix for permanent friction.
- **`/w/<workspace>/…` as a prefix, sections staying at the root.** Avoids
  collisions too, but leaves ten section names (`projects`, `tasks`, `inbox`,
  `calls`, `cycles`, `my-work`, `my-activity`, `recurring`, `palette`,
  `workspaces`) sharing the root namespace with workspace slugs. Moving the
  sections down removes the conflict by construction instead of by rule.

### Reserved root slugs

Two kinds of path stay at the root, and both collide with workspace slugs.

Service paths were never workspace-scoped: `admin/`, `accounts/`, `api/`,
`mcp/`, `telegram/`, `events/`, plus `static/` and `media/` served outside
Django.

Legacy section paths join them. Sections moved *under* the workspace, but
their old paths keep resolving forever (see below), so `tasks/`,
`projects/`, `inbox/`, `calls/`, `cycles/`, `my-work/`, `my-activity/`,
`recurring/`, `palette/` and `workspaces/` are still occupied. This is the
one place the "everything under the workspace" choice did *not* buy a clean
namespace, because backwards compatibility keeps the old names alive.

Django resolves patterns in order and all of these are declared first, so a
workspace slugged `tasks` would be created happily and then be half-eaten —
`/tasks/` showing All Tasks while `/tasks/inbox/` showed that workspace's
inbox. Validation turns that into "name is taken".

The list is **derived from the URLconf at runtime**, not hardcoded, so a
section added later reserves itself instead of quietly breaking whoever
already owns that name.

### Workspace slugs are immutable

A slug is assigned at creation and never changes. Renaming a workspace
changes its display name only.

The alternative — allowing renames and keeping a table of former slugs to
redirect from — was rejected: it makes every canonical URL conditionally
valid, and the alias table must then live forever anyway. An immutable slug
keeps every issued URL permanently correct, which is the whole point of
moving the workspace into the path.

### Legacy URLs live forever

The old workspace-less paths keep resolving:

- **one visible match** → `301` to the canonical URL
- **several** → the chooser (ADR-adjacent implementation: `AmbiguousSlug` +
  `_disambiguate.html`), which lists the candidates and their workspaces

Candidates come from a membership-scoped queryset, so a collision in a
workspace the viewer cannot reach never becomes a question, and a single
reachable candidate opens directly without asking.

### Access to another workspace's URL

`404`, not `403`. A `403` confirms the workspace exists, which leaks
organisation names to anyone guessing slugs.

## Consequences

**`reverse()` is the real cost, not the routing.** The canonical route takes
a workspace argument, and there are hundreds of `{% url %}` calls across
templates. Rewriting them all is not viable, so link generation goes through
a thin layer that reads the active workspace from context — the
`{% task_url %}` / `{% project_url %}` tags introduced in the first step are
the beginning of it.

**Every link generator must move**, not just templates: Telegram
notifications (`_task_url` in `apps/telegram/services.py`), invite emails,
MCP's `task_url`, exports.

**A workspace slug becomes permanent identity.** Users cannot fix a typo in
a slug after creation — only the display name. This is a deliberate trade:
stable URLs over editable ones. The creation form must therefore show the
resulting URL clearly, since it cannot be taken back.

**The chooser is permanent furniture.** It is not a migration artefact —
legacy links keep arriving indefinitely, so the page stays.
