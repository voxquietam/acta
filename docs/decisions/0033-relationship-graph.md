# 0033 — Relationship graph view

**Status:** accepted · 2026-10-02

## Context

Tasks carry three kinds of relationship: `blocks` (directional),
`related` (symmetrical) and `parent` / `subtask` (one level deep, see
ADR 0007). Every existing view shows them one task at a time — the links
panel on a task's detail page. Nothing answers "what does this chain of
work actually look like", which is the question asked when a release
slips or when deciding what to start next.

The data is sparse: in a representative workspace, 736 tasks carry 77
relationships between them. Most tasks are linked to nothing.

The design (Claude Design project "Graph view design for ActaSpace")
settled the shape before the implementation did: nodes are readable task
cards, not circles, and the board flows top to bottom.

## Decision

A **Graph** tab on project detail and on All Tasks — scoped to the
project in the first case and to the active workspace in the second.
Layout by dagre; everything that reaches the screen is ordinary DOM — a
card per task, one SVG layer for the edges, both inside a single
transformed stage.

**DOM, not canvas.** The first cut used cytoscape on canvas, on the
reasoning from ADR 0032 that a large render tree is what makes hovering
expensive. That reasoning was right about the risk and wrong about the
remedy: the cards carry wrapped titles, label chips, avatars and icons,
which a canvas renderer would have to re-implement as a layout engine.
DOM gives all of it for free, plus CSS theming across our three themes,
text selection and keyboard focus. Dropping cytoscape also halves the
download — dagre alone is 63 KB gzip against cytoscape's 133 KB.

**The cost canvas would have bought off is bounded directly instead:**

- only cards intersecting the viewport exist in the DOM — the same trick
  the task table plays with rows;
- the card shrinks with the zoom (full → compact → an id chip), so a
  distant board is a few elements per card rather than thirty;
- pan and zoom write one `transform` on the stage and never touch the
  cards, so neither relayouts anything;
- each card is `contain: layout paint style`, which bounds what a hover
  can invalidate. This is the piece ADR 0032 could not use: containment
  does not apply to `<tr>`, but it applies to a `<div>`.

Measured on a 241-task project with every task shown: 60 ms for layout
plus first render, 79 cards in the DOM, and a pan frame costing 1.4 ms
median / 3.3 ms worst against a 16.7 ms budget. A synthetic 1500-task
board pans just as cheaply (1.6 ms median); its one-off dagre layout
takes 1.7 s, which is the only cost that grows with size.

**Direction means one thing.** Down is "later": a parent sits above its
subtasks, and a blocker sits above the work it blocks. Reading the board
downwards is reading the order of work. The opposite convention — an
arrow pointing down at what you depend on — is equally valid in the
abstract, but mixing the two would make "down" mean two different things
on the same board.

**Every task is sent; the client decides what to draw.** The payload
carries all live tasks with a `connected` flag. Default is
connected-only — a project's unlinked tasks are the majority and drawing
them turns the view into confetti — and the "show all" switch reveals
the rest with no second request. Unlinked tasks get a grid below the
tiers; handing them to dagre stacks them into one meaningless rank.

Past `GRAPH_FULL_PAYLOAD_LIMIT` tasks in scope (a workspace reaches it;
a project does not) only the connected ones travel, the payload says so,
and the switch pays for a refetch rather than shipping a third of a
megabyte of JSON to a board nobody may scroll.

**Filters dim, they do not remove.** The sidebar is client-side, so the
board reads the form itself and greys what does not match while leaving
it in place: a blocker filtered out of view still has to explain the lock
on the task below it. A toolbar switch drops the dimmed cards for the
times that clutter is worse. One catch worth remembering — the chips that
never reach the URL stop the `change` event at their own handler, so the
board listens in the capture phase or the size and cycle filters look
dead while the rest work.

**Links that leave the project are drawn, hollow.** A blocker in a
neighbouring project is exactly what the view exists to surface. Nodes
the viewer cannot reach are dropped along with their edges.

## Consequences

- Query count is flat whatever the project's size: edges come off the
  through tables, nodes, labels and assignees are each fetched by id.
- The board is a mouse-and-trackpad surface: two fingers pan, pinch
  zooms (a pinch reaches the page as `ctrl` + wheel), matching every
  canvas app. Text selection is disabled on it, since a drag is a pan.
- Keyboard: arrows walk the graph along edges and across a tier, Enter
  opens, Esc clears. Cards are focusable, which a canvas could not offer.
- `related` is symmetrical, so Django stores both directions — the
  serialiser collapses the pair or every related line is drawn twice.
  Those edges are also excluded from the ranking, or symmetrical links
  would drag unrelated work into tiers it does not belong to.
- The workspace board is wider than a project's, and at a few thousand
  nodes the dagre pass (1.7 s for 1500) would want a worker or a
  clustering pass. The payload limit keeps the download bounded; the
  layout cost is the next ceiling to hit.

## Alternatives considered

- **cytoscape.js on canvas.** Built first, then replaced — see above.
  It remains the right answer for a graph of plain shapes at a scale
  where DOM genuinely cannot keep up; ours is neither.
- **Sigma.js (WebGL).** Handles 100k nodes. Our graphs are hundreds; the
  ceiling buys nothing and rich labels are harder.
- **elkjs for layout.** Better hierarchical layouts than dagre, at
  456 KB gzip. Not worth it at this size.
