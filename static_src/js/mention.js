// @-mention extension for the description / comment TipTap editors.
//
// One Mention node serves two kinds:
//   user → markdown ``[@username](mention:<id>)``  → chip ``@username``
//   task → markdown ``[ACTA-128](task:<id>)``      → chip-link to the task
//
// The ``@`` picker shows two sections (Users + Issues), fed by the
// project-scoped ``mention-search`` endpoint (URL from the mount's
// ``data-mention-url``). Markdown serialization writes the link-style
// tokens that the server render pipeline (apps/common/markdown) turns
// into chips; ``parseHTML`` claims those token links (and the rendered
// chip spans) back into mention nodes so re-editing round-trips.

import Mention from "@tiptap/extension-mention";

function fetchMentionItems(mentionUrl, query) {
  const url = mentionUrl + "?q=" + encodeURIComponent(query || "");
  return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
    .then((r) => (r.ok ? r.json() : { users: [], tasks: [] }))
    .then((data) => {
      const users = (data.users || []).map((u) => ({ kind: "user", ...u }));
      const tasks = (data.tasks || []).map((t) => ({ kind: "task", ...t }));
      return [...users, ...tasks];
    })
    .catch(() => []);
}

const STATUS_DOT = {
  planned: "#71717a",
  "to-do": "#3b82f6",
  "in-progress": "#8b5cf6",
  "in-review": "#f59e0b",
  done: "#10b981",
};

// Minimal vanilla dropdown (no tippy dependency). Renders a Users
// section then an Issues section; tracks a flat selected index over the
// selectable rows so arrow-key navigation crosses sections seamlessly.
function createPicker() {
  let el = null;
  let items = [];
  let index = 0;
  let onPick = null;

  function row(item, i) {
    const selected = i === index ? " acta-mention-item--active" : "";
    if (item.kind === "user") {
      const initial = (item.name || item.username || "?").slice(0, 1).toUpperCase();
      return (
        `<button type="button" class="acta-mention-item${selected}" data-i="${i}">` +
        `<span class="acta-mention-av" style="background:${item.avatar_color}">${initial}</span>` +
        `<span class="acta-mention-name">${item.name}</span>` +
        `<span class="acta-mention-sub">@${item.username}</span>` +
        `</button>`
      );
    }
    const dot = STATUS_DOT[item.status] || "#71717a";
    return (
      `<button type="button" class="acta-mention-item${selected}" data-i="${i}">` +
      `<span class="acta-mention-dot" style="background:${dot}"></span>` +
      `<span class="acta-mention-slug">${item.slug}</span>` +
      `<span class="acta-mention-title">${item.title}</span>` +
      `</button>`
    );
  }

  function render() {
    if (!el) return;
    if (!items.length) {
      el.innerHTML = '<div class="acta-mention-empty">No matches</div>';
      return;
    }
    const users = items.map((it, i) => [it, i]).filter(([it]) => it.kind === "user");
    const tasks = items.map((it, i) => [it, i]).filter(([it]) => it.kind === "task");
    let html = "";
    if (users.length) {
      html += '<div class="acta-mention-head">Users</div>';
      html += users.map(([it, i]) => row(it, i)).join("");
    }
    if (tasks.length) {
      html += '<div class="acta-mention-head">Issues</div>';
      html += tasks.map(([it, i]) => row(it, i)).join("");
    }
    el.innerHTML = html;
    el.querySelectorAll(".acta-mention-item").forEach((b) => {
      b.addEventListener("mousedown", (e) => {
        e.preventDefault();
        const i = parseInt(b.getAttribute("data-i"), 10);
        if (onPick && items[i]) onPick(items[i]);
      });
    });
  }

  function position(rect) {
    if (!el || !rect) return;
    const margin = 8;
    const spaceBelow = window.innerHeight - rect.bottom - margin;
    const spaceAbove = rect.top - margin;
    // Flip above the caret when there isn't room below (and above is
    // roomier). Cap the height to whatever space the chosen side has.
    const placeAbove = spaceBelow < 220 && spaceAbove > spaceBelow;
    el.style.maxHeight = Math.min(288, placeAbove ? spaceAbove : spaceBelow) + "px";
    const ph = el.offsetHeight;
    const top = placeAbove ? rect.top - ph - 4 : rect.bottom + 4;
    let left = rect.left;
    const pw = el.offsetWidth;
    if (left + pw > window.innerWidth - margin) left = window.innerWidth - pw - margin;
    if (left < margin) left = margin;
    el.style.top = top + "px";
    el.style.left = left + "px";
  }

  return {
    mount(rect, list, pick) {
      el = document.createElement("div");
      el.className = "acta-mention-popup";
      document.body.appendChild(el);
      items = list || [];
      index = 0;
      onPick = pick;
      render();
      position(rect);
    },
    update(rect, list) {
      items = list || [];
      if (index >= items.length) index = 0;
      render();
      position(rect);
    },
    onKeyDown(event) {
      if (!items.length) return false;
      if (event.key === "ArrowDown") {
        index = (index + 1) % items.length;
        render();
        return true;
      }
      if (event.key === "ArrowUp") {
        index = (index - 1 + items.length) % items.length;
        render();
        return true;
      }
      if (event.key === "Enter") {
        if (onPick && items[index]) onPick(items[index]);
        return true;
      }
      return false;
    },
    destroy() {
      if (el) el.remove();
      el = null;
      items = [];
    },
  };
}

export function buildMention(mentionUrl) {
  return Mention.extend({
    addAttributes() {
      return {
        id: { default: null },
        label: { default: null },
        mtype: { default: "user" },
      };
    },
    renderHTML({ node }) {
      if (node.attrs.mtype === "task") {
        return ["span", { class: "acta-task-mention", "data-task-id": node.attrs.id }, node.attrs.label];
      }
      return ["span", { class: "acta-mention", "data-user-id": node.attrs.id }, "@" + node.attrs.label];
    },
    renderText({ node }) {
      return node.attrs.mtype === "task" ? node.attrs.label : "@" + node.attrs.label;
    },
    parseHTML() {
      return [
        {
          tag: 'a[href^="mention:"]',
          priority: 60,
          getAttrs: (el) => ({
            id: el.getAttribute("href").slice("mention:".length),
            label: (el.textContent || "").replace(/^@/, ""),
            mtype: "user",
          }),
        },
        {
          tag: 'a[href^="task:"]',
          priority: 60,
          getAttrs: (el) => ({
            id: el.getAttribute("href").slice("task:".length),
            label: el.textContent || "",
            mtype: "task",
          }),
        },
        {
          tag: "span[data-user-id]",
          priority: 60,
          getAttrs: (el) => ({
            id: el.getAttribute("data-user-id"),
            label: (el.textContent || "").replace(/^@/, ""),
            mtype: "user",
          }),
        },
        {
          tag: "span[data-task-id]",
          priority: 60,
          getAttrs: (el) => ({
            id: el.getAttribute("data-task-id"),
            label: el.textContent || "",
            mtype: "task",
          }),
        },
      ];
    },
    addStorage() {
      return {
        markdown: {
          serialize(state, node) {
            if (node.attrs.mtype === "task") {
              state.write("[" + node.attrs.label + "](task:" + node.attrs.id + ")");
            } else {
              state.write("[@" + node.attrs.label + "](mention:" + node.attrs.id + ")");
            }
          },
          parse: {},
        },
      };
    },
  }).configure({
    suggestion: {
      char: "@",
      items: ({ query }) => fetchMentionItems(mentionUrl, query),
      command: ({ editor, range, props }) => {
        const item = props;
        let attrs;
        if (item.kind === "task") {
          // Chip label = "SLUG Title" so the inserted reference reads
          // like the issue, not a bare code. Strip markdown-link-breaking
          // chars and cap the title; the slug stays the leading token so
          // the server can derive the task URL from it.
          const title = (item.title || "").replace(/[[\]()\n]/g, "").trim().slice(0, 40);
          const label = title ? item.slug + " " + title : item.slug;
          attrs = { id: String(item.id), label, mtype: "task" };
        } else {
          attrs = { id: String(item.id), label: item.username, mtype: "user" };
        }
        editor
          .chain()
          .focus()
          .insertContentAt(range, [
            { type: "mention", attrs },
            { type: "text", text: " " },
          ])
          .run();
      },
      render: () => {
        const picker = createPicker();
        return {
          onStart(props) {
            picker.mount(props.clientRect && props.clientRect(), props.items, (item) => props.command(item));
          },
          onUpdate(props) {
            picker.update(props.clientRect && props.clientRect(), props.items);
          },
          onKeyDown(props) {
            if (props.event.key === "Escape") {
              picker.destroy();
              return true;
            }
            return picker.onKeyDown(props.event);
          },
          onExit() {
            picker.destroy();
          },
        };
      },
    },
  });
}
