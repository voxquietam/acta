/**
 * Relationship graph renderer — the project Graph tab.
 *
 * Loaded on demand (see the ``[data-task-graph]`` loader in base.html)
 * together with dagre, which is used for layout only. Everything that
 * reaches the screen is ordinary DOM: a card per task, one SVG layer for
 * the edges, both inside a single transformed stage.
 *
 * Why not canvas. The cards carry wrapped titles, label chips, avatars and
 * icons — markup a canvas renderer would have to re-implement as a layout
 * engine. DOM gives that for free, plus CSS theming, text selection and
 * keyboard focus. What canvas would have bought us is bounded instead:
 *
 *   - only cards intersecting the viewport exist in the DOM (the same
 *     trick the task table plays with rows, ADR 0032);
 *   - the card shrinks with the zoom — full, then compact, then an id
 *     chip — so a distant board is a handful of elements per card, not
 *     thirty;
 *   - pan and zoom write ONE transform on the stage and never touch the
 *     cards, so neither relayouts anything;
 *   - every card is ``contain: layout paint style``, which bounds what a
 *     hover can invalidate. (This is why the table could not be fixed the
 *     same way: containment does not apply to table rows.)
 *
 * Interaction follows the design: a click explores (select + highlight the
 * chain above and below), opening the task is a deliberate second step.
 */
(function () {
  "use strict";

  const STATUS_COLOR = {
    planned: "#71717a",
    "to-do": "#3b82f6",
    "in-progress": "#8b5cf6",
    "in-review": "#f59e0b",
    done: "#10b981",
    cancelled: "#52525b",
  };
  const EDGE_COLOR = { blocks: "#f43f5e", related: "#0ea5e9", parent: "#71717a", resolved: "#71717a" };
  const PRIORITY = {
    1: { name: "Urgent", color: "#f43f5e", icon: "chevrons-up" },
    2: { name: "High", color: "#f97316", icon: "chevron-up" },
    3: { name: "Medium", color: "#f59e0b", icon: "minus" },
    4: { name: "Low", color: "#0ea5e9", icon: "chevron-down" },
    0: { name: "None", color: "#71717a", icon: "circle-dashed" },
  };

  // Card footprint per detail level. Layout is recomputed when the level
  // changes, never on every zoom tick.
  const SIZES = { full: { w: 280, h: 152 }, compact: { w: 180, h: 32 }, mini: { w: 104, h: 26 } };
  // A stack stands in for a whole neighbouring project, so it is shorter
  // than a task card — there is no title to wrap — and keeps the same
  // width, which holds the tiers on one grid.
  const STACK_SIZES = { full: { w: 280, h: 104 }, compact: { w: 180, h: 32 }, mini: { w: 104, h: 26 } };
  // Below this a foreign project is drawn as its own cards: a stack
  // standing in for a single task hides the task and says nothing new.
  const STACK_MIN = 2;
  // Status dots a full-size stack shows before it starts counting.
  const STACK_DOTS = 10;
  const MIN_ZOOM = 0.12;
  const MAX_ZOOM = 1.6;
  // How far outside the viewport a card is still worth keeping in the DOM,
  // in screen pixels — a pan of less than this reveals no empty space.
  const OVERSCAN = 240;
  // Biggest single wheel event the zoom will act on, in pixels. One mouse
  // notch reports far more than a pinch step does, and without a ceiling
  // the two gestures zoom at wildly different speeds.
  const WHEEL_CLAMP = 50;
  // Rough pixel equivalents of the other two ``deltaMode`` units, so a
  // browser reporting lines or pages scrolls like one reporting pixels.
  const LINE_HEIGHT = 16;
  const PAGE_HEIGHT = 400;

  let G = null;
  // Whether the Unlinked list is open survives a re-render — the panel is
  // replaced on every filter change and the list should not blink shut.
  let DOCK_OPEN = false;
  // Zoom and pan survive a re-render. A filter chip replaces the whole panel
  // (the sidebar refetches it), and snapping back to "fit" every time threw
  // away wherever the user had navigated to.
  const CAMERA = new Map();
  // Foreign projects whose stack the user has opened. Also survives a
  // re-render, for the same reason the camera does.
  const EXPANDED = new Set();
  // Whether the board drops the cards a filter does not match or keeps them
  // dimmed in place. Dropping is the default: a filtered board should show
  // the work asked for, and the dimmed context is one button away.
  let ONLY_MATCHING = true;
  // Links made by dragging a row onto a card. The payload in the DOM is the
  // one the server rendered, so a link created since then is replayed on
  // top of it rather than costing a refetch of the whole panel.
  const ADDED_EDGES = [];

  // Wheel deltas come in three units depending on the browser. Firefox
  // reports lines, so an unconverted delta of 3 panned the board by three
  // pixels per notch.
  function wheelDelta(value, mode) {
    if (mode === 1) return value * LINE_HEIGHT;
    if (mode === 2) return value * PAGE_HEIGHT;
    return value;
  }

  function levelFor(zoom) {
    if (zoom >= 0.75) return "full";
    if (zoom >= 0.3) return "compact";
    return "mini";
  }

  function esc(value) {
    return String(value == null ? "" : value).replace(
      /[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
    );
  }

  // The icon sprite is an external file, so a ``<use>`` needs its full
  // URL — the panel hands it over on the host element.
  let SPRITE = "";

  function icon(name, cls) {
    return `<svg class="${cls}" viewBox="0 0 24 24" aria-hidden="true"><use href="${SPRITE}#lu-${name}"></use></svg>`;
  }

  // ---- model ---------------------------------------------------------------

  // Adjacency, both ways. Kept as one helper because the board is indexed
  // twice: once for the tasks the server sent, once again after the foreign
  // projects fold into stacks.
  function indexEdges(edges) {
    const out = new Map();
    const into = new Map();
    edges.forEach((e) => {
      if (!out.has(e.source)) out.set(e.source, []);
      if (!into.has(e.target)) into.set(e.target, []);
      out.get(e.source).push(e);
      into.get(e.target).push(e);
    });
    return { out, into };
  }

  function buildModel(data) {
    const byId = new Map();
    data.nodes.forEach((n) => byId.set(n.id, Object.assign({}, n)));
    const edges = data.edges.filter((e) => byId.has(e.source) && byId.has(e.target));
    const { out, into } = indexEdges(edges);
    // A task is blocked when something that blocks it is still open. The
    // server could compute it, but it is one pass over edges we already
    // hold, and it has to be redone whenever the board is refiltered.
    byId.forEach((node) => {
      node.blocked = (into.get(node.id) || []).some((e) => {
        if (e.kind !== "blocks") return false;
        const source = byId.get(e.source);
        return source && source.status !== "done" && source.status !== "cancelled";
      });
    });
    edges.forEach((e) => {
      const source = byId.get(e.source);
      e.resolved = e.kind === "blocks" && (source.status === "done" || source.status === "cancelled");
    });
    return { byId, edges, out, into };
  }

  // Neighbouring projects fold into one card each (design 1h). A board that
  // reaches into four other projects is mostly other people's work: the
  // stack keeps the link visible and the tiers readable, and opens in place
  // when that work is what you came for. Only foreign tasks fold — the
  // project the board is about is never a stack.
  function foldStacks(model) {
    const groups = new Map();
    model.byId.forEach((node) => {
      if (!node.external || node.projectId == null) return;
      if (!groups.has(node.projectId)) groups.set(node.projectId, []);
      groups.get(node.projectId).push(node);
    });

    const folded = new Map();
    const stacks = [];
    groups.forEach((members, projectId) => {
      // Marked even when the group is open, because that is what tells an
      // expanded card it can fold itself back up.
      members.forEach((node) => {
        node.foldable = members.length >= STACK_MIN;
      });
      if (members.length < STACK_MIN || EXPANDED.has(projectId)) return;
      const head = members[0];
      const blocking = members.filter((node) =>
        (model.out.get(node.id) || []).some((e) => {
          const target = model.byId.get(e.target);
          return e.kind === "blocks" && !e.resolved && target && !target.external;
        }),
      ).length;
      stacks.push({
        // Negative on purpose: the whole renderer reads a card's identity
        // through ``Number(dataset.graphNode)``, so a stack has to be a
        // number too — and no task id can collide with it.
        id: -projectId,
        stack: true,
        projectId: projectId,
        project: head.project,
        projectKey: head.projectKey,
        projectIcon: head.projectIcon,
        projectIconClass: head.projectIconClass,
        slug: head.projectKey,
        title: head.project,
        external: true,
        connected: true,
        blocked: false,
        count: members.length,
        blocking: blocking,
        members: members.map((node) => node.id),
        statuses: members.map((node) => node.status),
      });
      members.forEach((node) => folded.set(node.id, -projectId));
    });
    if (!stacks.length) return model;

    const byId = new Map();
    model.byId.forEach((node, id) => {
      if (!folded.has(id)) byId.set(id, node);
    });
    stacks.forEach((stack) => byId.set(stack.id, stack));

    // Several links into the same project arrive as one line on the stack.
    // A merged block is only spent when every link behind it is.
    const merged = new Map();
    model.edges.forEach((edge) => {
      const source = folded.has(edge.source) ? folded.get(edge.source) : edge.source;
      const target = folded.has(edge.target) ? folded.get(edge.target) : edge.target;
      // A link between two tasks of the same folded project is the stack's
      // own business and has nowhere to go on this board.
      if (source === target) return;
      const key =
        edge.kind === "related"
          ? `related|${Math.min(source, target)}|${Math.max(source, target)}`
          : `${edge.kind}|${source}|${target}`;
      const seen = merged.get(key);
      if (seen) {
        seen.resolved = seen.resolved && !!edge.resolved;
        return;
      }
      merged.set(key, { source: source, target: target, kind: edge.kind, resolved: !!edge.resolved });
    });

    const edges = [...merged.values()];
    const { out, into } = indexEdges(edges);
    return { byId: byId, edges: edges, out: out, into: into };
  }

  function cardSize(node, level) {
    return (node && node.stack ? STACK_SIZES : SIZES)[level];
  }

  // The filter sidebar is client-side (it never round-trips), so the board
  // reads the form directly rather than waiting for the server. Values are
  // ids, which is why the payload carries them alongside the display data.
  function readFilters() {
    const form = document.getElementById("filter-form");
    if (!form) return null;
    const fd = new FormData(form);
    const many = (name) => new Set(fd.getAll(name).map(String));
    const state = {
      status: many("status"),
      xstatus: many("xstatus"),
      priority: many("priority"),
      xpriority: many("xpriority"),
      assignee: many("assignee"),
      xassignee: many("xassignee"),
      label: many("label"),
      xlabel: many("xlabel"),
      project: many("project"),
      xproject: many("xproject"),
      size: many("size"),
      cycle: many("cycle"),
      q: (fd.get("q") || "").toString().trim().toLowerCase(),
      dateField: (fd.get("date_field") || "").toString(),
      dateAfter: (fd.get("date_after") || "").toString(),
      dateBefore: (fd.get("date_before") || "").toString(),
    };
    state.active =
      !!state.q ||
      !!(state.dateField && (state.dateAfter || state.dateBefore)) ||
      [
        "status",
        "xstatus",
        "priority",
        "xpriority",
        "assignee",
        "xassignee",
        "label",
        "xlabel",
        "project",
        "xproject",
        "size",
        "cycle",
      ].some((k) => state[k].size > 0);
    return state;
  }

  function meId() {
    const el = document.querySelector("[data-current-user-id]");
    return el ? el.getAttribute("data-current-user-id") : "";
  }

  function matchesFilters(node, state) {
    if (!state || !state.active) return true;
    const assignee = node.assigneeId == null ? "" : String(node.assigneeId);
    const assigneeTokens = new Set([assignee || "unassigned"]);
    if (assignee && assignee === meId()) assigneeTokens.add("me");
    const labels = (node.labelIds || []).map(String);
    const has = (set, value) => set.has(String(value));
    const anyLabel = (set) => labels.some((id) => set.has(id));
    const anyAssignee = (set) => [...assigneeTokens].some((t) => set.has(t));

    if (state.status.size && !has(state.status, node.status)) return false;
    if (has(state.xstatus, node.status)) return false;
    if (state.priority.size && !has(state.priority, node.priority)) return false;
    if (has(state.xpriority, node.priority)) return false;
    if (state.assignee.size && !anyAssignee(state.assignee)) return false;
    if (anyAssignee(state.xassignee)) return false;
    if (state.project.size && !has(state.project, node.projectId)) return false;
    if (has(state.xproject, node.projectId)) return false;
    if (state.label.size && !anyLabel(state.label)) return false;
    if (anyLabel(state.xlabel)) return false;
    if (state.size.size && !has(state.size, node.size == null ? "" : node.size)) return false;
    if (state.cycle.size) {
      // ``active`` and ``backlog`` are chip words, not ids — resolve them
      // against the cycle the payload says is running.
      const own = node.cycleId == null ? null : String(node.cycleId);
      const tokens = new Set([own || "backlog"]);
      if (own && own === String(state.activeCycleId)) tokens.add("active");
      if (![...tokens].some((t) => state.cycle.has(t))) return false;
    }
    if (state.dateField && (state.dateAfter || state.dateBefore)) {
      const day = (node.dates || {})[state.dateField];
      if (!day) return false;
      if (state.dateAfter && day < state.dateAfter) return false;
      if (state.dateBefore && day > state.dateBefore) return false;
    }
    if (state.q) {
      const hay = `${node.slug} ${node.title}`.toLowerCase();
      if (hay.indexOf(state.q) === -1) return false;
    }
    return true;
  }

  function connectedIds(model) {
    const ids = new Set();
    model.edges.forEach((e) => {
      ids.add(e.source);
      ids.add(e.target);
    });
    return ids;
  }

  // Walk the dependency chain in one direction from a node. Related links
  // are symmetrical context, not a chain, so they stop the walk.
  function chain(model, startId, direction) {
    const seen = new Set();
    const queue = [startId];
    const map = direction === "down" ? model.out : model.into;
    while (queue.length) {
      const id = queue.pop();
      (map.get(id) || []).forEach((e) => {
        if (e.kind === "related") return;
        const next = direction === "down" ? e.target : e.source;
        if (seen.has(next)) return;
        seen.add(next);
        queue.push(next);
      });
    }
    seen.delete(startId);
    return seen;
  }

  // ---- layout --------------------------------------------------------------

  // Tiers for the connected work, a shelf for the rest. Handing the
  // unlinked tasks to dagre puts them all in rank 0 — one endless row
  // across the top of the board, which is how this looked before the
  // design called for a shelf.
  const DOCK_ROWS_PER_GROUP = 8;

  function layout(model, level) {
    const linked = new Set();
    model.edges.forEach((e) => {
      linked.add(e.source);
      linked.add(e.target);
    });
    return packComponents(
      components(model, linked).map((ids) => layoutComponent(model, ids, level)),
      level,
    );
  }

  // Weakly connected groups of the linked work. Each one is a separate
  // dagre run: handing dagre the whole board instead lets it interleave
  // nodes from unrelated chains inside one rank, which is both unreadable
  // and impossible to re-pack afterwards (a chain's bounding box ends up
  // spanning the entire board).
  function components(model, linked) {
    const parent = new Map();
    const find = (id) => {
      while (parent.get(id) !== id) {
        parent.set(id, parent.get(parent.get(id)));
        id = parent.get(id);
      }
      return id;
    };
    linked.forEach((id) => parent.set(id, id));
    model.edges.forEach((edge) => {
      if (!parent.has(edge.source) || !parent.has(edge.target)) return;
      const a = find(edge.source);
      const b = find(edge.target);
      if (a !== b) parent.set(a, b);
    });
    const groups = new Map();
    linked.forEach((id) => {
      const key = find(id);
      if (!groups.has(key)) groups.set(key, new Set());
      groups.get(key).add(id);
    });
    return [...groups.values()];
  }

  function layoutComponent(model, ids, level) {
    const g = new window.dagre.graphlib.Graph({ multigraph: true });
    g.setGraph({
      rankdir: "TB",
      nodesep: level === "full" ? 36 : 20,
      ranksep: level === "full" ? 76 : 44,
      marginx: 0,
      marginy: 0,
    });
    g.setDefaultEdgeLabel(() => ({}));
    ids.forEach((id) => {
      const box = cardSize(model.byId.get(id), level);
      g.setNode(String(id), { width: box.w, height: box.h });
    });
    model.edges.forEach((e, i) => {
      // Related edges are symmetrical — letting them influence the ranking
      // drags unrelated work into tiers it does not belong to.
      if (e.kind === "related" || !ids.has(e.source) || !ids.has(e.target)) return;
      g.setEdge(String(e.source), String(e.target), { weight: e.kind === "parent" ? 2 : 1 }, "e" + i);
    });
    window.dagre.layout(g);

    const pos = new Map();
    let w = 0;
    let h = 0;
    g.nodes().forEach((id) => {
      const n = g.node(id);
      if (!n) return;
      // Per-node, not per-level: a stack is a different shape to the task
      // cards it stands in for.
      const box = { x: n.x - n.width / 2, y: n.y - n.height / 2, w: n.width, h: n.height };
      pos.set(Number(id), box);
      w = Math.max(w, box.x + box.w);
      h = Math.max(h, box.y + box.h);
    });
    return { pos, w, h };
  }

  // Shelf packing, tallest first. Dagre would otherwise line every chain up
  // left to right: a project with forty of them comes back as a ribbon
  // thousands of pixels wide and five tiers tall, where "fit to screen"
  // means 12% and nothing is readable. The mosaic aims at the shape of a
  // screen instead.
  function packComponents(parts, level) {
    const gap = level === "full" ? 72 : 40;
    const margin = 40;
    const pos = new Map();
    if (!parts.length) return { pos, width: SIZES[level].w + margin * 2, height: 0 };

    parts.sort((a, b) => b.h - a.h || b.w - a.w);
    const area = parts.reduce((sum, part) => sum + (part.w + gap) * (part.h + gap), 0);
    const widest = parts.reduce((max, part) => Math.max(max, part.w), 0);
    // 16:9 — the shape of the thing the board is looked at on.
    const target = Math.max(widest, Math.sqrt((area * 16) / 9));

    let x = 0;
    let y = 0;
    let rowHeight = 0;
    let width = 0;
    parts.forEach((part) => {
      if (x > 0 && x + part.w > target) {
        x = 0;
        y += rowHeight + gap;
        rowHeight = 0;
      }
      part.pos.forEach((box, id) => {
        box.x += x + margin;
        box.y += y + margin;
        pos.set(id, box);
      });
      x += part.w + gap;
      width = Math.max(width, x - gap);
      rowHeight = Math.max(rowHeight, part.h);
    });

    return { pos, width: width + margin * 2, height: y + rowHeight + margin * 2 };
  }

  // ---- edges ---------------------------------------------------------------

  // Rounded orthogonal path through a list of points.
  function roundedPath(points, radius) {
    let d = `M${points[0][0]} ${points[0][1]}`;
    for (let i = 1; i < points.length - 1; i += 1) {
      const [x0, y0] = points[i - 1];
      const [x1, y1] = points[i];
      const [x2, y2] = points[i + 1];
      const l1 = Math.hypot(x1 - x0, y1 - y0);
      const l2 = Math.hypot(x2 - x1, y2 - y1);
      if (!l1 || !l2) continue;
      const r = Math.min(radius, l1 / 2, l2 / 2);
      d += ` L${x1 - ((x1 - x0) / l1) * r} ${y1 - ((y1 - y0) / l1) * r}`;
      d += ` Q${x1} ${y1} ${x1 + ((x2 - x1) / l2) * r} ${y1 + ((y2 - y1) / l2) * r}`;
    }
    const last = points[points.length - 1];
    return `${d} L${last[0]} ${last[1]}`;
  }

  // Leave the source's bottom edge, run along the gutter between the two
  // tiers, enter the target's top edge. Same-tier links (``related``) hop
  // sideways instead.
  function edgePoints(edge, pos) {
    const a = pos.get(edge.source);
    const b = pos.get(edge.target);
    if (!a || !b) return null;
    const ax = a.x + a.w / 2;
    const bx = b.x + b.w / 2;
    if (b.y < a.y + a.h) {
      // Same tier. A straight line here would run behind every card between
      // the two, reading as a stray stroke that leaves the screen — so the
      // edge dips under the row and comes back up.
      const sy = a.y + a.h;
      const ty = b.y + b.h;
      const dip = Math.max(sy, ty) + 22;
      return [
        [ax, sy],
        [ax, dip],
        [bx, dip],
        [bx, ty],
      ];
    }
    const sy = a.y + a.h;
    const ty = b.y;
    if (Math.abs(ax - bx) < 2) return [[ax, sy], [bx, ty]];
    const gutter = sy + Math.max(14, (ty - sy) / 2);
    return [[ax, sy], [ax, gutter], [bx, gutter], [bx, ty]];
  }

  function renderEdges(state) {
    const { model, pos, level } = state;
    const parts = [];
    const arrows = [];
    state.edgeIndex = new Map();
    model.edges.forEach((edge, i) => {
      const points = edgePoints(edge, pos);
      if (!points) return;
      const kind = edge.resolved ? "resolved" : edge.kind;
      const color = EDGE_COLOR[kind] || EDGE_COLOR.related;
      const width = kind === "blocks" ? 2 : kind === "related" ? 1.25 : 1.5;
      const pts = points.map((p) => p.slice());
      const n = pts.length;
      const [x2, y2] = pts[n - 1];
      const [x1, y1] = pts[n - 2];
      const len = Math.hypot(x2 - x1, y2 - y1) || 1;
      const dx = (x2 - x1) / len;
      const dy = (y2 - y1) / len;
      if (kind === "blocks") {
        pts[n - 1] = [x2 - dx * 6, y2 - dy * 6];
        const head = 8;
        const half = 4.5;
        const bx = x2 - dx * head;
        const by = y2 - dy * head;
        arrows.push(
          `<path d="M${x2} ${y2} L${bx - dy * half} ${by + dx * half} L${bx + dy * half} ${by - dx * half} Z" fill="${color}" data-edge="${i}"/>`,
        );
      }
      const dash = kind === "related" ? ' stroke-dasharray="4 4"' : "";
      parts.push(
        `<path d="${roundedPath(pts, level === "full" ? 8 : 6)}" fill="none" stroke="${color}" stroke-width="${width}"${dash} stroke-linecap="round" vector-effect="non-scaling-stroke" data-edge="${i}"/>`,
      );
      state.edgeIndex.set(i, edge);
    });
    state.svg.setAttribute("width", state.width);
    state.svg.setAttribute("height", state.height);
    state.svg.innerHTML = parts.join("") + arrows.join("");
  }

  // ---- cards ---------------------------------------------------------------

  // One folded project. The counts answer the only two questions a stack
  // has to answer before it is worth opening: how much of this project the
  // board touches, and how much of it is holding work here up.
  function stackHtml(node, level) {
    const badge = `<span class="acta-gstack-pic ${esc(node.projectIconClass || "")}">${icon(node.projectIcon || "folder", "acta-gcard-ic")}</span>`;
    const expand = `<button type="button" class="acta-gstack-exp" data-graph-expand="${node.projectId}" title="Draw this project's tasks">${icon("maximize-2", "acta-gcard-ic")}Expand</button>`;

    if (level === "mini") {
      return `${badge}<span class="acta-gcard-id">${esc(node.projectKey)}</span><span class="acta-gstack-n">${node.count}</span>`;
    }
    if (level === "compact") {
      return (
        `${badge}<span class="acta-gcard-id">${esc(node.projectKey)}</span>` +
        `<span class="acta-gcard-ttl">${esc(node.project)}</span>` +
        `<span class="acta-gstack-n">${node.count}</span>`
      );
    }

    const shown = node.statuses.slice(0, STACK_DOTS);
    const rest = node.statuses.length - shown.length;
    const dots = shown
      .map((s) => `<span class="acta-gcard-dot" style="background:${STATUS_COLOR[s] || STATUS_COLOR.planned}"></span>`)
      .join("");
    const blocking = node.blocking
      ? ` · <span class="acta-gstack-block">${node.blocking} blocking here</span>`
      : "";
    return (
      `<div class="acta-gcard-top">${badge}<span class="acta-gcard-id">${esc(node.projectKey)}</span>` +
      `<span class="acta-gcard-gap"></span>` +
      `<span class="acta-gstack-tag">${icon("layers", "acta-gcard-ic")}stack</span></div>` +
      `<div class="acta-gstack-name">${esc(node.project)}</div>` +
      `<div class="acta-gstack-meta">${node.count} linked task${node.count === 1 ? "" : "s"}${blocking}</div>` +
      `<div class="acta-gstack-dots">${dots}${rest > 0 ? `<span class="acta-gstack-n">+${rest}</span>` : ""}</div>` +
      `<span class="acta-gcard-gap"></span>${expand}`
    );
  }

  function cardHtml(node, level) {
    if (node.stack) return stackHtml(node, level);
    const status = STATUS_COLOR[node.status] || STATUS_COLOR.planned;
    const done = node.status === "done";
    const cancelled = node.status === "cancelled";
    const mark = done
      ? `<span class="acta-gcard-glyph" style="color:#10b981">${icon("circle-check", "acta-gcard-ic")}</span>`
      : cancelled
        ? `<span class="acta-gcard-glyph" style="color:#71717a">${icon("circle-x", "acta-gcard-ic")}</span>`
        : `<span class="acta-gcard-dot" style="background:${status}"></span>`;

    if (level === "mini") {
      return `${mark}<span class="acta-gcard-id">${esc(node.slug)}</span>`;
    }

    const who = node.who
      ? `<span class="acta-gcard-av" style="background:${esc(node.who.c)}" title="${esc(node.who.name)}">${esc(node.who.i)}</span>`
      : `<span class="acta-gcard-av acta-gcard-av-none" title="Unassigned">?</span>`;

    if (level === "compact") {
      return (
        `${mark}<span class="acta-gcard-id">${esc(node.slug)}</span>` +
        `<span class="acta-gcard-ttl">${esc(node.title)}</span>${who}`
      );
    }

    const prio = PRIORITY[node.priority] || PRIORITY[0];
    const labels = (node.labels || []).slice(0, 3);
    const more = (node.labels || []).length - labels.length;
    const chips = labels
      .map(
        (l) =>
          `<span class="acta-gcard-lab" style="--lab:${esc(l.c)}"><span class="acta-gcard-labdot"></span>${esc(l.n)}</span>`,
      )
      .join("");
    const overflow = more > 0 ? `<span class="acta-gcard-more">+${more}</span>` : "";
    // On an expanded stack the project chip is how you fold it back up —
    // the affordance sits where the card already says which project it is.
    const project = !node.external
      ? ""
      : node.foldable
        ? `<button type="button" class="acta-gcard-proj is-fold" data-graph-collapse="${node.projectId}" title="Fold ${esc(node.projectKey)} back into one card">${icon("layers", "acta-gcard-ic")}${esc(node.projectKey)}</button>`
        : `<span class="acta-gcard-proj">${icon("folder", "acta-gcard-ic")}${esc(node.projectKey)}</span>`;
    const lock = node.blocked ? `<span class="acta-gcard-lock" title="Blocked by open work">${icon("lock", "acta-gcard-ic")}</span>` : "";
    const due = node.due
      ? `<span class="acta-gcard-due${node.overdue ? " is-overdue" : node.dueToday ? " is-today" : ""}">${icon("calendar", "acta-gcard-ic")}${esc(node.due)}</span>`
      : "";
    const size = node.size ? `<span class="acta-gcard-size">${icon("gauge", "acta-gcard-ic")}${esc(node.size)}</span>` : "";

    return (
      `<div class="acta-gcard-top">${mark}<span class="acta-gcard-id">${esc(node.slug)}</span>${project}` +
      `<span class="acta-gcard-gap"></span>${lock}` +
      `<span class="acta-gcard-prio" style="color:${prio.color}">${icon(prio.icon, "acta-gcard-ic")}${prio.name}</span></div>` +
      `<div class="acta-gcard-title">${esc(node.title)}</div>` +
      `<div class="acta-gcard-labels">${chips}${overflow}</div>` +
      `<div class="acta-gcard-foot">${who}<span class="acta-gcard-who">${esc(node.who ? node.who.name : "Unassigned")}</span>` +
      `<span class="acta-gcard-gap"></span>${due}${size}</div>`
    );
  }

  function applyFilters(state) {
    const filters = readFilters();
    if (filters) filters.activeCycleId = state.activeCycleId;
    state.filters = filters;
    let matched = 0;
    // The same pass marks the board's cards and the side list's rows — one
    // filter set drives both (design 1i).
    (state.allById || state.model.byId).forEach((node) => {
      node.matches = matchesFilters(node, filters);
    });
    state.model.byId.forEach((node) => {
      // A stack matches when anything inside it does — folding work away
      // must not also filter it away.
      node.matches = node.stack
        ? node.members.some((id) => {
            const member = state.allById && state.allById.get(id);
            return member ? member.matches !== false : true;
          })
        : matchesFilters(node, filters);
      if (node.matches) matched += 1;
    });
    state.matched = matched;
    const label = state.panel && state.panel.querySelector("[data-graph-match-count]");
    if (label) {
      label.hidden = !(filters && filters.active);
      label.textContent = `${matched}/${state.model.byId.size}`;
    }
  }

  function applyCardState(el, node, state) {
    el.classList.toggle("is-stack", !!node.stack);
    el.classList.toggle("is-blocked", !!node.blocked && node.status !== "done" && node.status !== "cancelled");
    el.classList.toggle("is-done", node.status === "done");
    el.classList.toggle("is-cancelled", node.status === "cancelled");
    el.classList.toggle("is-external", !!node.external);
    el.classList.toggle("is-selected", state.selected === node.id);
    // A filtered-out card stays in place and dims — pulling it out would
    // break the chain it sits in, which is the whole point of the board.
    el.classList.toggle("is-filtered-out", node.matches === false);
    if (state.selected == null) {
      el.classList.remove("is-dim", "is-path");
      return;
    }
    const onPath = state.selected === node.id || state.path.has(node.id);
    el.classList.toggle("is-path", onPath);
    el.classList.toggle("is-dim", !onPath);
  }

  // Only the cards the viewport can see exist in the DOM. Everything else
  // is a line in a Map until the user pans towards it.
  function syncCards(state) {
    const { pos, zoom, pan, host } = state;
    const left = (-pan.x - OVERSCAN) / zoom;
    const top = (-pan.y - OVERSCAN) / zoom;
    const right = (-pan.x + host.clientWidth + OVERSCAN) / zoom;
    const bottom = (-pan.y + host.clientHeight + OVERSCAN) / zoom;
    const wanted = new Set();

    state.visibleIds.length = 0;
    pos.forEach((box, id) => {
      if (box.x > right || box.x + box.w < left || box.y > bottom || box.y + box.h < top) return;
      wanted.add(id);
      state.visibleIds.push(id);
    });

    state.cards.forEach((el, id) => {
      if (wanted.has(id)) return;
      el.remove();
      state.cards.delete(id);
    });

    const frag = document.createDocumentFragment();
    wanted.forEach((id) => {
      const node = state.model.byId.get(id);
      const box = pos.get(id);
      let el = state.cards.get(id);
      if (!el) {
        el = document.createElement("div");
        el.className = "acta-gcard";
        el.dataset.graphNode = String(id);
        el.tabIndex = 0;
        el.innerHTML = cardHtml(node, state.level);
        state.cards.set(id, el);
        frag.appendChild(el);
      }
      el.dataset.level = state.level;
      el.style.transform = `translate(${box.x}px, ${box.y}px)`;
      el.style.width = `${box.w}px`;
      el.style.height = `${box.h}px`;
      applyCardState(el, node, state);
    });
    if (frag.childNodes.length) state.stage.appendChild(frag);
  }

  function applyTransform(state) {
    state.stage.style.transform = `translate(${state.pan.x}px, ${state.pan.y}px) scale(${state.zoom})`;
    const label = state.panel && state.panel.querySelector("[data-graph-zoom]");
    if (label) label.textContent = `${Math.round(state.zoom * 100)}%`;
  }

  // Zoom crossed into another detail level: the cards change size, so the
  // layout has to be recomputed and every card re-rendered.
  // Board state lives on the host under names of its own: sharing the
  // buttons' attribute names made ``closest`` match the board, so every
  // click on empty canvas counted as a press of the toolbar switch.
  function storageKey(state) {
    return "acta:graph-size:" + (state.host.dataset.project || "");
  }

  function setPinned(state, value) {
    state.pinned = value;
    try {
      if (value) window.localStorage.setItem(storageKey(state), value);
      else window.localStorage.removeItem(storageKey(state));
    } catch (e) {
      /* private window: the choice simply does not persist */
    }
    state.panel.querySelectorAll("[data-graph-size]").forEach((btn) => {
      btn.classList.toggle("is-on", (btn.dataset.graphSize || "") === (value || ""));
    });
    if (relevel(state)) redraw(state);
  }

  function relevel(state) {
    const level = state.pinned || levelFor(state.zoom);
    if (level === state.level) return false;
    state.level = level;
    const laid = layout(state.model, level);
    state.pos = laid.pos;
    state.width = laid.width;
    state.height = laid.height;
    state.cards.forEach((el) => el.remove());
    state.cards.clear();
    renderEdges(state);
    drawMinimap(state);
    return true;
  }

  // Minimap: one rect per task, plus the viewport. Drawn once per layout;
  // only the viewport rectangle moves while panning.
  function drawMinimap(state) {
    const box = state.panel && state.panel.querySelector("[data-graph-minimap]");
    if (!box) return;
    state.minimap = box;
    const w = box.clientWidth || 200;
    const h = box.clientHeight || 120;
    const scale = Math.min(w / (state.width || 1), h / (state.height || 1));
    state.minimapScale = scale;
    state.minimapPad = { x: (w - state.width * scale) / 2, y: (h - state.height * scale) / 2 };
    const rects = [];
    state.pos.forEach((node, id) => {
      const task = state.model.byId.get(id);
      const color = task && task.status === "done" ? "var(--border-strong)" : "var(--placeholder-foreground)";
      rects.push(
        `<rect x="${(node.x * scale + state.minimapPad.x).toFixed(1)}" y="${(node.y * scale + state.minimapPad.y).toFixed(1)}" width="${Math.max(2, node.w * scale).toFixed(1)}" height="${Math.max(1.5, node.h * scale).toFixed(1)}" rx="1" fill="rgb(${color})"/>`,
      );
    });
    box.innerHTML =
      `<svg width="${w}" height="${h}">${rects.join("")}<rect data-graph-viewport fill="rgb(79 70 229 / 0.12)" stroke="#6366f1" stroke-width="1" rx="2"/></svg>`;
    state.viewportRect = box.querySelector("[data-graph-viewport]");
  }

  function drawViewport(state) {
    if (!state.viewportRect) return;
    const s = state.minimapScale || 0;
    const pad = state.minimapPad || { x: 0, y: 0 };
    const x = (-state.pan.x / state.zoom) * s + pad.x;
    const y = (-state.pan.y / state.zoom) * s + pad.y;
    state.viewportRect.setAttribute("x", x.toFixed(1));
    state.viewportRect.setAttribute("y", y.toFixed(1));
    state.viewportRect.setAttribute("width", Math.max(4, (state.host.clientWidth / state.zoom) * s).toFixed(1));
    state.viewportRect.setAttribute("height", Math.max(4, (state.host.clientHeight / state.zoom) * s).toFixed(1));
  }

  // Bottom-left counter: what is on the board versus what is being held
  // back. The hidden figure is only honest when the payload carries the
  // isolated tasks — past the server's size limit it does not, and the
  // switch refetches instead.
  function drawCounter(state) {
    const el = state.panel && state.panel.querySelector("[data-graph-counter]");
    if (!el) return;
    const linked = state.linkedCount;
    const hidden = state.allData.truncated ? null : state.allData.nodes.length - linked;
    el.textContent = state.allData.truncated
      ? `${linked} linked`
      : `${linked} linked · ${state.looseIds.length} unlinked`;
  }

  function redraw(state) {
    applyTransform(state);
    syncCards(state);
    drawViewport(state);
    CAMERA.set(state.host.dataset.project || "", { zoom: state.zoom, pan: { x: state.pan.x, y: state.pan.y } });
  }

  // Fitting and the detail level chase each other: a smaller zoom means
  // smaller cards, which means a smaller board, which would fit at a larger
  // zoom. Three passes is always enough to settle — there are only three
  // levels — and each pass is one dagre run on a graph of this size.
  function fit(state) {
    const w = state.host.clientWidth;
    const h = state.host.clientHeight;
    if (!w || !h || !state.width || !state.height) return;
    for (let pass = 0; pass < 3; pass += 1) {
      const scale = Math.min(1, Math.min(w / (state.width + 60), h / (state.height + 60)));
      state.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, scale));
      if (!relevel(state)) break;
    }
    state.pan.x = (w - state.width * state.zoom) / 2;
    state.pan.y = Math.max(16, (h - state.height * state.zoom) / 2);
    redraw(state);
  }


  // ---- docked list ---------------------------------------------------------

  function dockGroups(state) {
    const groups = new Map();
    state.looseIds.forEach((id) => {
      const node = state.allById.get(id);
      // One filter set drives the board and this list together (design 1i),
      // so there is nothing to search or group by here.
      if (!node || node.matches === false) return;
      const key = node.projectKey || "";
      if (!groups.has(key)) {
        groups.set(key, { key, name: node.project || "", icon: node.projectIcon, cls: node.projectIconClass, rows: [] });
      }
      groups.get(key).rows.push(node);
    });
    return [...groups.values()].sort((a, b) => b.rows.length - a.rows.length);
  }

  function drawDock(state) {
    const dock = state.panel && state.panel.querySelector("[data-graph-dock]");
    if (!dock) return;
    // Two controls carry this: the rail button and its twin in the expanded
    // sidebar. Both show the count and both reflect whether the list is open.
    document.querySelectorAll("[data-graph-unlinked-count]").forEach((badge) => {
      badge.textContent = String(state.looseIds.length);
      badge.hidden = !state.looseIds.length;
    });
    document.querySelectorAll("[data-graph-unlinked-toggle]").forEach((btn) => {
      btn.classList.toggle("is-active", !!state.dockOpen);
      btn.setAttribute("aria-checked", state.dockOpen ? "true" : "false");
    });
    if (!state.dockOpen) {
      dock.hidden = true;
      return;
    }
    dock.hidden = false;

    const groups = dockGroups(state);
    const shown = groups.reduce((n, g) => n + g.rows.length, 0);
    const body = groups
      .map((g) => {
        const rows = g.rows.slice(0, DOCK_ROWS_PER_GROUP);
        const rest = g.rows.length - rows.length;
        const items = rows
          .map(
            (n) =>
              `<button type="button" class="acta-gdock-row" data-graph-dock-task="${n.id}">` +
              `<span class="acta-gcard-dot" style="background:${STATUS_COLOR[n.status] || STATUS_COLOR.planned}"></span>` +
              `<span class="acta-gdock-id">${esc(n.slug)}</span>` +
              `<span class="acta-gdock-ttl">${esc(n.title)}</span>` +
              (n.who
                ? `<span class="acta-gcard-av" style="background:${esc(n.who.c)}" title="${esc(n.who.name)}">${esc(n.who.i)}</span>`
                : `<span class="acta-gcard-av acta-gcard-av-none">?</span>`) +
              `</button>`,
          )
          .join("");
        const more = rest > 0 ? `<span class="acta-gdock-more">+ ${rest} more</span>` : "";
        return (
          `<div class="acta-gdock-group"><div class="acta-gdock-head">` +
          `<span class="acta-gdock-pic ${esc(g.cls || "")}">${icon(g.icon || "folder", "acta-gcard-ic")}</span>` +
          `<span class="acta-gdock-key">${esc(g.key)}</span>` +
          `<span class="acta-gdock-name">${esc(g.name)}</span>` +
          `<span class="acta-gcard-gap"></span>` +
          `<span class="acta-gdock-count">${g.rows.length}</span></div>${items}${more}</div>`
        );
      })
      .join("");

    const filters = state.filters;
    const banner =
      filters && filters.active
        ? `<div class="acta-gdock-filtered">${icon("filter", "acta-gcard-ic")}` +
          `<span>Rail filters apply</span>` +
          `<span class="acta-gcard-gap"></span>` +
          `<span class="acta-gdock-of">${shown} of ${state.looseIds.length}</span></div>`
        : "";

    dock.innerHTML =
      `<div class="acta-gdock-top">` +
      `<span class="acta-gdock-pic">${icon("unlink", "acta-gcard-ic")}</span>` +
      `<span class="acta-gdock-label">Unlinked</span>` +
      `<span class="acta-gdock-total">${state.looseIds.length}</span>` +
      `<span class="acta-gcard-gap"></span>` +
      `<button type="button" class="acta-gstep" data-graph-dock-hide title="Hide list">` +
      icon("x", "acta-gcard-ic") +
      `</button></div>` +
      banner +
      `<p class="acta-gdock-hint">${icon("link", "acta-gcard-ic")}Drag a row onto a card to link it</p>` +
      `<div class="acta-gdock-body">${body || '<p class="acta-gdock-empty">Nothing here matches the filters.</p>'}</div>`;
  }

  // ---- drag a row onto a card to link it ----------------------------------

  // The side list is the only place a task with no links can be grabbed, so
  // dragging a row onto a card is the board's own way to give it one. A
  // drag cannot say WHICH kind of link it means, so the drop asks instead
  // of guessing — nothing is written until that is answered.
  function bindDockDrag(state) {
    const dock = state.panel.querySelector("[data-graph-dock]");
    if (!dock) return;
    let drag = null;

    const highlight = (card) => {
      if (drag.over === card) return;
      if (drag.over) drag.over.classList.remove("is-droptarget");
      drag.over = card;
      if (card) card.classList.add("is-droptarget");
    };

    const finish = (pointerId) => {
      const was = drag;
      drag = null;
      if (!was) return null;
      if (was.row.hasPointerCapture(pointerId)) was.row.releasePointerCapture(pointerId);
      if (was.ghost) was.ghost.remove();
      if (was.over) was.over.classList.remove("is-droptarget");
      state.host.classList.remove("is-linking");
      return was;
    };

    dock.addEventListener("pointerdown", (e) => {
      const row = e.target.closest("[data-graph-dock-task]");
      if (!row || e.button !== 0) return;
      drag = { id: Number(row.dataset.graphDockTask), x: e.clientX, y: e.clientY, row: row, moved: false, over: null };
      // Capture on the row, so the moves keep arriving here once the
      // pointer leaves the list — no listeners on the document, which
      // would outlive the panel the sidebar replaces on every filter.
      row.setPointerCapture(e.pointerId);
    });

    dock.addEventListener("pointermove", (e) => {
      if (!drag) return;
      if (!drag.moved) {
        if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) < 4) return;
        drag.moved = true;
        const node = state.allById.get(drag.id);
        drag.ghost = document.createElement("div");
        drag.ghost.className = "acta-gdrag";
        drag.ghost.innerHTML =
          `${icon("link", "acta-gcard-ic")}<span class="acta-gdock-id">${esc(node ? node.slug : "")}</span>` +
          `<span class="acta-gdock-ttl">${esc(node ? node.title : "")}</span>`;
        document.body.appendChild(drag.ghost);
        state.host.classList.add("is-linking");
      }
      drag.ghost.style.transform = `translate(${e.clientX + 14}px, ${e.clientY + 14}px)`;
      // The ghost must not shadow the card underneath the cursor.
      drag.ghost.style.visibility = "hidden";
      const under = document.elementFromPoint(e.clientX, e.clientY);
      drag.ghost.style.visibility = "";
      const card = under && under.closest ? under.closest("[data-graph-node]") : null;
      const node = card && state.model.byId.get(Number(card.dataset.graphNode));
      // A stack is several tasks at once — there is no single task to link.
      highlight(node && !node.stack ? card : null);
    });

    dock.addEventListener("pointerup", (e) => {
      const was = finish(e.pointerId);
      if (!was || !was.moved) return;
      // The browser follows this pointerup with a click; without swallowing
      // it the drag would also open the row's task.
      const swallow = (ev) => {
        ev.stopPropagation();
        ev.preventDefault();
      };
      window.addEventListener("click", swallow, { capture: true, once: true });
      window.setTimeout(() => window.removeEventListener("click", swallow, true), 400);
      if (was.over) openLinkMenu(state, was.id, Number(was.over.dataset.graphNode), e.clientX, e.clientY);
    });

    dock.addEventListener("pointercancel", (e) => finish(e.pointerId));
  }

  function openLinkMenu(state, sourceId, targetId, clientX, clientY) {
    const menu = state.panel.querySelector("[data-graph-linkmenu]");
    const source = state.allById.get(sourceId);
    const target = state.model.byId.get(targetId);
    if (!menu || !source || !target) return;
    const rect = state.panel.getBoundingClientRect();
    menu.hidden = false;
    menu.style.left = `${Math.max(8, Math.min(clientX - rect.left, rect.width - 260))}px`;
    menu.style.top = `${Math.max(8, Math.min(clientY - rect.top, rect.height - 170))}px`;
    menu.innerHTML =
      `<p class="acta-glinkmenu-head"><span class="acta-gdock-id">${esc(source.slug)}</span>` +
      `<span class="acta-gdock-ttl">${esc(source.title)}</span></p>` +
      `<button type="button" data-graph-link="blocks">${icon("lock", "acta-gcard-ic")}` +
      `blocks <b>${esc(target.slug)}</b></button>` +
      `<button type="button" data-graph-link="blocked_by">${icon("lock", "acta-gcard-ic")}` +
      `blocked by <b>${esc(target.slug)}</b></button>` +
      `<button type="button" data-graph-link="related">${icon("link", "acta-gcard-ic")}` +
      `related to <b>${esc(target.slug)}</b></button>` +
      `<button type="button" data-graph-link-cancel class="acta-glinkmenu-cancel">Cancel</button>`;
    state.pendingLink = { source: sourceId, target: targetId };
  }

  function closeLinkMenu(state) {
    const menu = state.panel && state.panel.querySelector("[data-graph-linkmenu]");
    if (menu) {
      menu.hidden = true;
      menu.innerHTML = "";
    }
    state.pendingLink = null;
  }

  function toast(message, level) {
    if (window.actaToast) window.actaToast(message, level);
  }

  function submitLink(state, kind) {
    const pending = state.pendingLink;
    const url = state.host.dataset.linkUrl;
    if (!pending || !url) return;
    closeLinkMenu(state);
    const body = new URLSearchParams({
      kind: kind,
      source: String(pending.source),
      target: String(pending.target),
    });
    window
      .fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "X-CSRFToken": (window.acta && window.acta.csrfToken()) || "",
          "Content-Type": "application/x-www-form-urlencoded",
        },
        body: body.toString(),
      })
      .then((res) => res.json().catch(() => ({ ok: false })))
      .then((data) => {
        if (!data || !data.ok || !data.edge) {
          toast(data && data.error ? data.error : "Could not link those tasks", "error");
          return;
        }
        // The board redraws from the payload the server rendered, so the
        // new edge is replayed on top of it rather than refetched.
        ADDED_EDGES.push(data.edge);
        toast("Link added", "success");
        rebuild();
      })
      .catch(() => toast("Could not link those tasks", "error"));
  }

  // ---- selection -----------------------------------------------------------

  function select(state, id) {
    state.selected = id;
    if (id == null) {
      state.path = new Set();
    } else {
      const up = chain(state.model, id, "up");
      const down = chain(state.model, id, "down");
      state.path = new Set([...up, ...down]);
      state.up = up.size;
      state.down = down.size;
    }
    state.cards.forEach((el, nodeId) => applyCardState(el, state.model.byId.get(nodeId), state));
    paintEdgeFocus(state);
    paintSelectionBar(state);
  }

  function paintEdgeFocus(state) {
    const dim = state.selected != null;
    state.svg.classList.toggle("is-focused", dim);
    if (!dim) {
      state.svg.querySelectorAll("[data-edge]").forEach((el) => el.classList.remove("is-dim"));
      return;
    }
    const live = new Set([state.selected, ...state.path]);
    state.svg.querySelectorAll("[data-edge]").forEach((el) => {
      const edge = state.edgeIndex.get(Number(el.dataset.edge));
      const on = edge && live.has(edge.source) && live.has(edge.target);
      el.classList.toggle("is-dim", !on);
    });
  }

  function paintSelectionBar(state) {
    const bar = state.panel && state.panel.querySelector("[data-graph-selection]");
    if (!bar) return;
    if (state.selected == null) {
      bar.hidden = true;
      return;
    }
    const node = state.model.byId.get(state.selected);
    bar.hidden = false;
    if (node.stack) {
      bar.innerHTML =
        `<span class="acta-gstack-pic ${esc(node.projectIconClass || "")}">${icon(node.projectIcon || "folder", "acta-gcard-ic")}</span>` +
        `<span class="acta-gsel-id">${esc(node.projectKey)}</span>` +
        `<span class="acta-gsel-count">${node.count} linked · ${node.blocking} blocking here</span>` +
        `<button type="button" class="acta-gsel-open" data-graph-expand="${node.projectId}">` +
        `${icon("maximize-2", "acta-gcard-ic")}Expand</button>`;
      return;
    }
    bar.innerHTML =
      `<span class="acta-gcard-dot" style="background:${STATUS_COLOR[node.status] || STATUS_COLOR.planned}"></span>` +
      `<span class="acta-gsel-id">${esc(node.slug)}</span>` +
      `<span class="acta-gsel-count">${state.up} upstream · ${state.down} downstream</span>` +
      `<button type="button" class="acta-gsel-open" data-graph-open>${icon("maximize-2", "acta-gcard-ic")}Open</button>`;
  }

  // Walk the graph from the selected card. Down and up follow the edges,
  // left and right move along the tier — which is what the eye expects
  // from a layered board.
  function step(state, key) {
    const here = state.pos.get(state.selected);
    if (!here) return null;
    if (key === "ArrowDown" || key === "ArrowUp") {
      const list = (key === "ArrowDown" ? state.model.out : state.model.into).get(state.selected) || [];
      const ids = list.map((e) => (key === "ArrowDown" ? e.target : e.source)).filter((id) => state.pos.has(id));
      if (!ids.length) return null;
      ids.sort((a, b) => Math.abs(state.pos.get(a).x - here.x) - Math.abs(state.pos.get(b).x - here.x));
      return ids[0];
    }
    const forward = key === "ArrowRight";
    let best = null;
    state.pos.forEach((box, id) => {
      if (id === state.selected) return;
      // Same tier: tops within half a card of each other.
      if (Math.abs(box.y - here.y) > here.h / 2) return;
      if (forward ? box.x <= here.x : box.x >= here.x) return;
      if (!best || Math.abs(box.x - here.x) < Math.abs(state.pos.get(best).x - here.x)) best = id;
    });
    return best;
  }

  // Bring a node into view without changing the zoom — used by the keyboard
  // walk, which can easily step off-screen.
  function reveal(state, id) {
    const box = state.pos.get(id);
    if (!box) return;
    const w = state.host.clientWidth;
    const h = state.host.clientHeight;
    const left = box.x * state.zoom + state.pan.x;
    const top = box.y * state.zoom + state.pan.y;
    const right = left + box.w * state.zoom;
    const bottom = top + box.h * state.zoom;
    const margin = 32;
    if (left < margin) state.pan.x += margin - left;
    else if (right > w - margin) state.pan.x -= right - (w - margin);
    if (top < margin) state.pan.y += margin - top;
    else if (bottom > h - margin) state.pan.y -= bottom - (h - margin);
    redraw(state);
  }

  // Folding is a view state, not a server one: the set of open projects
  // lives in the module, so a panel refetch keeps whatever the user opened.
  function setFolded(projectId, expanded) {
    if (expanded) EXPANDED.add(projectId);
    else EXPANDED.delete(projectId);
    rebuild();
  }

  function openTask(state, id) {
    const node = state.model.byId.get(id);
    if (!node) return;
    // A stack has nothing to open but itself.
    if (node.stack) {
      setFolded(node.projectId, true);
      return;
    }
    if (!node.url) return;
    if (window.htmx) {
      const url = node.url + (node.url.indexOf("?") === -1 ? "?" : "&") + "modal=1";
      window.htmx.ajax("GET", url, { target: "#modal-root", swap: "innerHTML" });
    } else {
      window.location.assign(node.url);
    }
  }

  // ---- wiring --------------------------------------------------------------

  function bind(state) {
    const host = state.host;

    // Trackpad-first, the way every canvas app behaves: two fingers move the
    // board, pinch zooms. A pinch arrives as a wheel event with ``ctrlKey``
    // set — that is how browsers report it — and ⌘/ctrl + wheel gives mouse
    // users the same zoom. Shift swaps the axis, as it does everywhere else.
    host.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const dx = wheelDelta(e.deltaX, e.deltaMode);
        const dy = wheelDelta(e.deltaY, e.deltaMode);
        if (!e.ctrlKey && !e.metaKey) {
          state.pan.x -= e.shiftKey && !dx ? dy : dx;
          state.pan.y -= e.shiftKey && !dx ? 0 : dy;
          redraw(state);
          return;
        }
        const rect = host.getBoundingClientRect();
        const px = e.clientX - rect.left;
        const py = e.clientY - rect.top;
        // A trackpad pinch arrives as a stream of small deltas; one notch of
        // a mouse wheel arrives as a single huge one (100 px in Chrome,
        // three *lines* in Firefox). Clamping before scaling makes both
        // feel the same — about 1.2x per notch, matching the toolbar's
        // stepper — instead of 2.7x, which is what an unclamped 100 did.
        const step = Math.max(-WHEEL_CLAMP, Math.min(WHEEL_CLAMP, dy));
        const factor = Math.exp(-step * 0.004);
        const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, state.zoom * factor));
        if (next === state.zoom) return;
        // Keep the point under the cursor fixed while the scale changes.
        state.pan.x = px - ((px - state.pan.x) / state.zoom) * next;
        state.pan.y = py - ((py - state.pan.y) / state.zoom) * next;
        state.zoom = next;
        relevel(state);
        redraw(state);
      },
      { passive: false },
    );

    let dragging = null;
    host.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || e.target.closest("[data-graph-node]")) return;
      dragging = { x: e.clientX, y: e.clientY, px: state.pan.x, py: state.pan.y, moved: false };
      host.setPointerCapture(e.pointerId);
      host.classList.add("is-panning");
    });
    host.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      state.pan.x = dragging.px + (e.clientX - dragging.x);
      state.pan.y = dragging.py + (e.clientY - dragging.y);
      if (Math.abs(e.clientX - dragging.x) + Math.abs(e.clientY - dragging.y) > 3) dragging.moved = true;
      redraw(state);
    });
    host.addEventListener("pointerup", (e) => {
      const was = dragging;
      dragging = null;
      host.classList.remove("is-panning");
      if (host.hasPointerCapture(e.pointerId)) host.releasePointerCapture(e.pointerId);
      if (was && !was.moved && !e.target.closest("[data-graph-node]")) select(state, null);
    });

    host.addEventListener("click", (e) => {
      const expand = e.target.closest("[data-graph-expand]");
      if (expand) {
        setFolded(Number(expand.dataset.graphExpand), true);
        return;
      }
      const collapse = e.target.closest("[data-graph-collapse]");
      if (collapse) {
        setFolded(Number(collapse.dataset.graphCollapse), false);
        return;
      }
      const card = e.target.closest("[data-graph-node]");
      if (!card) return;
      const id = Number(card.dataset.graphNode);
      // Modifier clicks keep the browser's own meaning — a new tab.
      if (e.metaKey || e.ctrlKey) {
        const node = state.model.byId.get(id);
        if (node && node.url) window.open(node.url, "_blank", "noopener");
        return;
      }
      if (state.selected === id) {
        openTask(state, id);
        return;
      }
      select(state, id);
    });

    host.addEventListener("dblclick", (e) => {
      const card = e.target.closest("[data-graph-node]");
      if (card) openTask(state, Number(card.dataset.graphNode));
    });

    host.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        select(state, null);
        return;
      }
      if (e.key === "Enter") {
        const card = e.target.closest("[data-graph-node]");
        if (card) openTask(state, Number(card.dataset.graphNode));
        return;
      }
      if (e.key.indexOf("Arrow") !== 0 || state.selected == null) return;
      e.preventDefault();
      const next = step(state, e.key);
      if (next != null) {
        select(state, next);
        reveal(state, next);
      }
    });

    const panel = state.panel;
    panel.addEventListener("click", (e) => {
      // The selection bar carries an Expand of its own, and it sits outside
      // the board — the stage's own handler never sees it.
      const expand = e.target.closest("[data-graph-expand]");
      if (expand) {
        setFolded(Number(expand.dataset.graphExpand), true);
        return;
      }
      const kind = e.target.closest("[data-graph-link]");
      if (kind) {
        submitLink(state, kind.dataset.graphLink);
        return;
      }
      if (e.target.closest("[data-graph-link-cancel]")) {
        closeLinkMenu(state);
        return;
      }
      if (e.target.closest("[data-graph-open]") && state.selected != null) {
        openTask(state, state.selected);
        return;
      }
      const zoomBtn = e.target.closest("[data-graph-zoom-by]");
      if (zoomBtn) {
        const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, state.zoom * Number(zoomBtn.dataset.graphZoomBy)));
        // Anchored at the middle of the board, the way the pinch is
        // anchored at the cursor — stepping the zoom otherwise grows the
        // board out of its own top-left corner and walks away from
        // whatever the user was looking at.
        const cx = state.host.clientWidth / 2;
        const cy = state.host.clientHeight / 2;
        state.pan.x = cx - ((cx - state.pan.x) / state.zoom) * next;
        state.pan.y = cy - ((cy - state.pan.y) / state.zoom) * next;
        state.zoom = next;
        relevel(state);
        redraw(state);
        return;
      }
      if (e.target.closest("[data-graph-fit]")) fit(state);
      if (e.target.closest("[data-graph-dock-hide]")) {
        DOCK_OPEN = false;
        state.dockOpen = false;
        drawDock(state);
        redraw(state);
        return;
      }
      const row = e.target.closest("[data-graph-dock-task]");
      if (row) {
        openTask(state, Number(row.dataset.graphDockTask));
        return;
      }
      const size = e.target.closest("[data-graph-size]");
      if (size) setPinned(state, size.dataset.graphSize || null);
    });

    // Edge labels on hover. The paths opt back into hit-testing (the layer
    // itself stays transparent to the pointer) so the chip can follow the
    // line the cursor is actually over.
    state.svg.addEventListener("pointerover", (e) => {
      const path = e.target.closest("[data-edge]");
      if (!path) return;
      const edge = state.edgeIndex.get(Number(path.dataset.edge));
      if (!edge) return;
      const chip = state.panel.querySelector("[data-graph-edge-label]");
      if (!chip) return;
      chip.textContent = edge.resolved ? "blocked — now done" : edge.kind === "parent" ? "subtask" : edge.kind;
      chip.dataset.kind = edge.resolved ? "resolved" : edge.kind;
      chip.hidden = false;
      const rect = state.host.getBoundingClientRect();
      chip.style.left = `${e.clientX - rect.left + 12}px`;
      chip.style.top = `${e.clientY - rect.top + 12}px`;
    });
    state.svg.addEventListener("pointerout", (e) => {
      if (e.target.closest("[data-edge]")) {
        const chip = state.panel.querySelector("[data-graph-edge-label]");
        if (chip) chip.hidden = true;
      }
    });

    bindDockDrag(state);

    const ro = window.ResizeObserver ? new ResizeObserver(() => redraw(state)) : null;
    if (ro) ro.observe(host);
    state.observer = ro;
  }

  // ---- entry point ---------------------------------------------------------

  function render() {
    const host = document.querySelector("[data-task-graph]");
    const payload = document.getElementById("task-graph-data");
    if (!host || !payload || !window.dagre) return;
    SPRITE = host.dataset.sprite || SPRITE;
    if (G && G.host === host) return; // already drawn on this panel
    if (G && G.observer) G.observer.disconnect();

    let data;
    try {
      data = JSON.parse(payload.textContent);
    } catch (err) {
      return;
    }
    if (!data || !data.nodes || !data.nodes.length) return;

    // Links made since the server rendered this payload.
    if (ADDED_EDGES.length) {
      const have = new Set(data.edges.map((e) => `${e.kind}|${e.source}|${e.target}`));
      ADDED_EDGES.forEach((edge) => {
        const key = `${edge.kind}|${edge.source}|${edge.target}`;
        if (have.has(key)) return;
        have.add(key);
        data.edges.push(edge);
      });
    }

    const full = buildModel(data);
    const linked = connectedIds(full);
    const filters = readFilters();
    if (filters) filters.activeCycleId = data.activeCycleId;
    // The board is the linked structure. Everything else is the side list,
    // which reads the full payload through ``looseIds``.
    let nodes = data.nodes.filter((n) => linked.has(n.id));
    if (ONLY_MATCHING && filters && filters.active) nodes = nodes.filter((n) => matchesFilters(n, filters));
    const model = foldStacks(nodes.length === data.nodes.length ? full : buildModel({ nodes, edges: data.edges }));

    host.innerHTML = "";
    const stage = document.createElement("div");
    stage.className = "acta-gstage";
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "acta-gedges");
    stage.appendChild(svg);
    host.appendChild(stage);

    G = {
      host,
      panel: host.closest("[data-graph-panel]") || host.parentElement,
      stage,
      svg,
      model,
      allData: data,
      activeCycleId: data.activeCycleId,
      dockOpen: DOCK_OPEN,
      linkedCount: linked.size,
      edgeCount: full.edges.length,
      cards: new Map(),
      visibleIds: [],
      selected: null,
      path: new Set(),
      up: 0,
      down: 0,
      zoom: 1,
      pan: { x: 0, y: 0 },
      level: null,
      pinned: null,
    };

    // The board draws the links; tasks that have none are not a mode of it
    // but their own side list, opened from the filter rail (design 1j).
    // Every task the payload carries, linked or not — the board draws from
    // the model, the side list reads this.
    G.allById = new Map(data.nodes.map((n) => [n.id, n]));
    G.looseIds = data.nodes.filter((n) => !linked.has(n.id)).map((n) => n.id);

    applyFilters(G);
    // The panel comes back from the server on every filter change, so the
    // switch's own state is restored rather than read from the markup.
    const matchBtn = G.panel.querySelector("button[data-graph-only-matching]");
    if (matchBtn) matchBtn.classList.toggle("is-on", ONLY_MATCHING);
    const saved = CAMERA.get(host.dataset.project || "");
    if (saved) {
      G.zoom = saved.zoom;
      G.pan = { x: saved.pan.x, y: saved.pan.y };
    }
    G.level = levelFor(G.zoom);
    const laid = layout(model, G.level);
    G.pos = laid.pos;
    G.width = laid.width;
    G.height = laid.height;
    renderEdges(G);
    drawMinimap(G);
    drawDock(G);
    drawCounter(G);
    bind(G);
    try {
      G.pinned = window.localStorage.getItem(storageKey(G)) || null;
    } catch (e) {
      G.pinned = null;
    }
    if (G.pinned) setPinned(G, G.pinned);
    if (saved) {
      relevel(G);
      redraw(G);
      // The board may have shrunk under the old camera — if it now looks at
      // nothing, fall back to fitting rather than showing an empty field.
      if (!G.visibleIds.length) fit(G);
    } else {
      fit(G);
    }
  }

  window.actaRenderTaskGraph = render;

  function rebuild() {
    if (G && G.observer) G.observer.disconnect();
    G = null;
    render();
  }

  // The sidebar never round-trips, so a chip change reaches the board as a
  // plain form event. Re-running the filter pass is a class toggle per
  // visible card unless "only matching" is on, which changes the node set
  // and therefore needs a fresh layout.
  // CAPTURE phase on purpose. The chips that never reach the URL (size,
  // cycle) stop the event at their own handler, so a bubble-phase listener
  // on the document simply never hears them — those two filters looked
  // dead while the rest worked. Capture also covers chips bound to the form
  // by the ``form`` attribute rather than by nesting.
  document.addEventListener(
    "change",
    (e) => {
      const owned =
        (e.target.form && e.target.form.id === "filter-form") || (e.target.closest && e.target.closest("#filter-form"));
      if (!G || !owned) return;
      if (ONLY_MATCHING) {
        // Dropping the unmatched cards changes the node set, so this is a
        // fresh layout rather than a class toggle per card.
        rebuild();
        return;
      }
      applyFilters(G);
      G.cards.forEach((el, id) => applyCardState(el, G.model.byId.get(id), G));
      drawDock(G);
    },
    true,
  );

  document.addEventListener("click", (e) => {
    const rail = e.target.closest("[data-graph-unlinked-toggle]");
    if (rail && G) {
      DOCK_OPEN = !DOCK_OPEN;
      G.dockOpen = DOCK_OPEN;
      drawDock(G);
      redraw(G);
      return;
    }
    const btn = e.target.closest("button[data-graph-only-matching]");
    if (!btn || !G) return;
    // On (the default) draws only what the filters match; off brings the
    // rest back dimmed, for the times the chain matters more than the
    // filter — a blocker filtered out of view still explains the lock
    // under it.
    ONLY_MATCHING = !ONLY_MATCHING;
    btn.classList.toggle("is-on", ONLY_MATCHING);
    rebuild();
  });

})();
