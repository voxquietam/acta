# ADR 0023: Mentions (`@user` / `@task`) — markdown-token pipeline

**Status:** accepted
**Date:** 2026-05-20

## Context

[0006](0006-mvp-scope.md) originally pushed `@-mentions` out of the MVP, then
pulled them back in once the notification subsystem ([0021](0021-notification-inbox.md))
made them cheap. Mentions are a small feature that touches an unusually wide
slice of the stack at once: the TipTap rich-text editor, the markdown
round-trip, the server-side HTML render + sanitization, and the notification
fan-out. This ADR records how those pieces fit together.

Two kinds of mention:

- **`@user`** — references a workspace member. Renders as an inline chip and
  raises a `mention` notification for the referenced user.
- **`@task`** — references a task by slug (e.g. `ACTA-128`). Renders as a
  chip-link to the task. No notification (it points at a thing, not a person).

The hard requirement is that a mention must **survive the round-trip**: the
editor serializes to markdown for storage, and that markdown is re-loaded into
the editor on the next edit. A representation that the markdown serializer
mangles would corrupt mentions on every save.

## Decision

### Storage — mentions are markdown link tokens

A mention is stored in the markdown body as a **link with a custom scheme**, so
the existing markdown serializer treats it as an ordinary link and leaves it
intact across the round-trip:

- user: `[@username](mention:<user_id>)`
- task: `[ACTA-128](task:<task_id>)` (the label may carry the title after the
  slug, e.g. `[ACTA-128 Fix login](task:128)`)

The id lives in the href; the human-readable label is the link text. Nothing
about rendering or fan-out needs to re-resolve the target by name — the id is
right there.

### Editor — `@tiptap/extension-mention` + `@tiptap/suggestion`

The TipTap bundle (see [0014](0014-frontend-architecture.md)) gains
`@tiptap/extension-mention` and `@tiptap/suggestion` for the `@`-triggered
autocomplete. On serialize, a mention node is written as the markdown link
token above; on parse, the token is recognized back into a mention node. This
is the round-trip contract the storage format was chosen to satisfy.

### Server render — rewrite tokens to chips, *before* bleach

`apps/common/markdown.py` renders the stored markdown to HTML for display. The
mention tokens are rewritten into chip markup in `_render_mentions`, which runs
**after** `markdown.markdown(...)` but **before** `bleach.clean(...)`:

- `<a href="mention:<id>">@name</a>` → `<span class="acta-mention" data-user-id="<id>">@name</span>`
  (the hover-card + brand styling is wired client-side; no DB lookup at render
  time).
- `<a href="task:<id>">ACTA-128</a>` → `<a class="acta-task-mention" data-task-id="<id>" href="/projects/<prefix>/<number>/">ACTA-128</a>`,
  the href derived from the **slug label alone** (`ACTA-128` → `/projects/ACTA/128/`)
  — again no DB lookup.

### Security — bleach hardening of the chips

Because the rewrite runs before bleach, the sanitizer's per-tag attribute
filter (`_attr_filter`) is what ultimately governs what survives — so a user who
*types* a literal `<span class="acta-mention" ...>` in their markdown cannot
inject arbitrary markup. The filter locks the mention shapes down hard:

- `<span>` may carry `class` **only** if it equals `acta-mention`, and
  `data-user-id` **only** if it is all digits. Everything else on a span is
  stripped.
- `<a>` may carry `class` only if it equals `acta-task-mention`, `data-task-id`
  only if all-digits, otherwise only the standard anchor attrs.

So the chip classes/`data-*` attributes that the renderer emits are the *only*
mention attributes that can ever reach the DOM, no matter what the user typed.

### Notifications — the `mention` kind

`@user` mentions raise a notification through the per-user fan-out
([0021](0021-notification-inbox.md)):

- `parse_mentioned_user_ids(text)` reads ids straight from the `(mention:<id>)`
  tokens with a regex — no HTML parse needed.
- `notify_mentions(...)` validates the candidates are workspace members (a
  mention can only reach someone who can see the target), drops the actor via
  `notify()`'s self-rule, and emits `Notification.Kind.MENTION`.
- On a **comment**, mentions are notified *first*; the comment's
  assignee/reporter fan-out then **subtracts** the mentioned set, so a mentioned
  assignee gets the higher-signal `mention`, not a duplicate `comment`
  (`notify_comment_created`).
- On a **description edit**, only *newly added* mentions notify —
  `notify_description_mentions` diffs the token sets between old and new text, so
  re-saving never re-pings someone already mentioned.

## Why

- **Markdown link tokens** are the cheapest representation that survives the
  TipTap ⇆ markdown round-trip — the serializer already handles links, so there
  is no custom serializer/parser to keep in sync.
- **Id-in-href** means render and fan-out never re-resolve the target by name; a
  rename doesn't break a stored mention, and chip rendering needs zero DB
  queries.
- **Rewrite-before-bleach + a tight `_attr_filter`** gives a single, auditable
  XSS boundary: the renderer emits exactly the chip shapes the sanitizer allows,
  and nothing else can sneak through.
- **Mention beats comment in the fan-out** keeps inboxes signal-dense — one
  notification per event, the most relevant kind.

## Consequences

- The mention chip's *visual* form (hover card, avatar, brand tint) is wired
  client-side from the `data-user-id` / `data-task-id` attributes; the server
  emits a minimal, sanitizer-safe stub.
- Task-mention hrefs are derived from the slug label, so a malformed label
  (missing the `PREFIX-N` shape) falls back to `#` rather than a wrong link.
- Adding a new mention kind (e.g. `@label`) means a new scheme + a new
  `_render_mentions` branch + a new entry in the `_attr_filter` allowlist — the
  three places the pipeline lives. Keep them in lockstep.
- The `mention` notification kind was reserved in the `Notification.Kind` enum
  ahead of time ([0021](0021-notification-inbox.md)), so emitting mentions
  needed no migration.
