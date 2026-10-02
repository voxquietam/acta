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
  const MIN_ZOOM = 0.12;
  const MAX_ZOOM = 1.6;
  // How far outside the viewport a card is still worth keeping in the DOM,
  // in screen pixels — a pan of less than this reveals no empty space.
  const OVERSCAN = 240;

  let G = null;
  // Whether the Unlinked list is open survives a re-render — the panel is
  // replaced on every filter change and the list should not blink shut.
  let DOCK_OPEN = false;
  // Zoom and pan survive a re-render. A filter chip replaces the whole panel
  // (the sidebar refetches it), and snapping back to "fit" every time threw
  // away wherever the user had navigated to.
  const CAMERA = new Map();

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

  function buildModel(data) {
    const byId = new Map();
    data.nodes.forEach((n) => byId.set(n.id, Object.assign({}, n)));
    const edges = data.edges.filter((e) => byId.has(e.source) && byId.has(e.target));
    const out = new Map();
    const into = new Map();
    edges.forEach((e) => {
      if (!out.has(e.source)) out.set(e.source, []);
      if (!into.has(e.target)) into.set(e.target, []);
      out.get(e.source).push(e);
      into.get(e.target).push(e);
    });
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
    const size = SIZES[level];
    const linked = new Set();
    model.edges.forEach((e) => {
      linked.add(e.source);
      linked.add(e.target);
    });

    const g = new window.dagre.graphlib.Graph({ multigraph: true });
    g.setGraph({
      rankdir: "TB",
      nodesep: level === "full" ? 36 : 20,
      ranksep: level === "full" ? 76 : 44,
      marginx: 40,
      marginy: 40,
    });
    g.setDefaultEdgeLabel(() => ({}));
    linked.forEach((id) => g.setNode(String(id), { width: size.w, height: size.h }));
    model.edges.forEach((e, i) => {
      // Related edges are symmetrical — letting them influence the ranking
      // drags unrelated work into tiers it does not belong to.
      if (e.kind === "related") return;
      g.setEdge(String(e.source), String(e.target), { weight: e.kind === "parent" ? 2 : 1 }, "e" + i);
    });
    window.dagre.layout(g);

    const pos = new Map();
    g.nodes().forEach((id) => {
      const n = g.node(id);
      if (n) pos.set(Number(id), { x: n.x - size.w / 2, y: n.y - size.h / 2, w: size.w, h: size.h });
    });
    const graph = g.graph();
    const width = graph.width || size.w + 80;
    const height = graph.height || 0;

    return { pos, width, height };
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

  function cardHtml(node, level) {
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
    const project = node.external
      ? `<span class="acta-gcard-proj">${icon("folder", "acta-gcard-ic")}${esc(node.projectKey)}</span>`
      : "";
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
      node.matches = matchesFilters(node, filters);
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
      `<div class="acta-gdock-body">${body || '<p class="acta-gdock-empty">Nothing here matches the filters.</p>'}</div>`;
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

  function openTask(state, id) {
    const node = state.model.byId.get(id);
    if (!node || !node.url) return;
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
        if (!e.ctrlKey && !e.metaKey) {
          const dx = e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX;
          const dy = e.shiftKey && !e.deltaX ? 0 : e.deltaY;
          state.pan.x -= dx;
          state.pan.y -= dy;
          redraw(state);
          return;
        }
        const rect = host.getBoundingClientRect();
        const px = e.clientX - rect.left;
        const py = e.clientY - rect.top;
        const factor = Math.exp(-e.deltaY * 0.01);
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
      if (e.target.closest("[data-graph-open]") && state.selected != null) {
        openTask(state, state.selected);
        return;
      }
      const zoomBtn = e.target.closest("[data-graph-zoom-by]");
      if (zoomBtn) {
        const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, state.zoom * Number(zoomBtn.dataset.graphZoomBy)));
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

    const full = buildModel(data);
    const linked = connectedIds(full);
    const onlyMatching = host.dataset.graphMatching === "1";
    const filters = readFilters();
    if (filters) filters.activeCycleId = data.activeCycleId;
    // The board is the linked structure. Everything else is the side list,
    // which reads the full payload through ``looseIds``.
    let nodes = data.nodes.filter((n) => linked.has(n.id));
    if (onlyMatching && filters && filters.active) nodes = nodes.filter((n) => matchesFilters(n, filters));
    const model = nodes.length === data.nodes.length ? full : buildModel({ nodes, edges: data.edges });

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
      if (G.host.dataset.graphMatching === "1") {
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
    const on = G.host.dataset.graphMatching !== "1";
    G.host.dataset.graphMatching = on ? "1" : "0";
    btn.classList.toggle("is-on", on);
    rebuild();
  });

})();
