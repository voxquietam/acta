// Description editor — TipTap (ProseMirror) instance with a bubble
// menu on selection, Linear-style. Output is markdown via
// tiptap-markdown so the server-side render pipeline
// (apps/common/markdown.render_markdown) stays the source of truth
// when reading.
//
// Mount: any element with [data-description-editor] containing a
// nested .description-editor-mount (the editable surface) and a
// .description-editor-toolbar (a selection bubble: hidden by default,
// shown over a non-empty text selection and positioned from the
// selection's coords — see onSelectionUpdate / positionBubble; no tippy,
// no portaling). The initial markdown source is read from a sibling
// .description-editor-source hidden textarea.
//
// Save: on blur (editor losing focus), if the markdown has changed,
// posts the current markdown to the form's action via HTMX. The save
// flow is wired in the template via form.requestSubmit().
//
// Pagehide safety net: same beacon pattern as the title cell — if the
// user navigates away with unsaved changes, a sendBeacon POST queues
// the latest markdown during page unload.

import { Editor } from "@tiptap/core";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import Placeholder from "@tiptap/extension-placeholder";
import TaskList from "@tiptap/extension-task-list";
import TaskItem from "@tiptap/extension-task-item";
import Highlight from "@tiptap/extension-highlight";
import Typography from "@tiptap/extension-typography";
import { Markdown } from "tiptap-markdown";
import { buildMention } from "./mention.js";

const INSTANCES = new WeakMap();

// Custom selection bubble (no positioning library, no portaling — the
// two things that broke the tippy attempts). We position our own toolbar
// from the selection's viewport coords and toggle it from TipTap's own
// lifecycle callbacks. The toolbar stays inside the editor root, so its
// Alpine link-mode keeps working.
function selectionRect(editor) {
  // The rendered selection box in viewport coords. The DOM selection's
  // own getBoundingClientRect is authoritative (matches what the user
  // sees); editor.view.coordsAtPos drifted from it and put the bubble on
  // top of the word. Fall back to coordsAtPos only if there's no live
  // DOM range (rare).
  const sel = window.getSelection();
  if (sel && sel.rangeCount > 0 && !sel.isCollapsed) {
    const r = sel.getRangeAt(0).getBoundingClientRect();
    if (r.width || r.height) {
      return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
    }
  }
  const s = editor.view.coordsAtPos(editor.state.selection.from);
  const e = editor.view.coordsAtPos(editor.state.selection.to);
  return { top: s.top, bottom: e.bottom, left: s.left, right: e.right };
}

function positionBubble(editor, toolbar) {
  const rect = selectionRect(editor);
  const midX = (rect.left + rect.right) / 2;
  toolbar.style.position = "fixed";
  toolbar.style.zIndex = "50";
  toolbar.style.left = `${midX}px`;
  // Anchor by the bubble's own bottom edge (translateY -100%) so we never
  // read its height. Sit fully above the selection; flip below only when
  // too close to the viewport top.
  const ROOM_ABOVE = 56;
  if (rect.top < ROOM_ABOVE) {
    toolbar.style.top = `${rect.bottom + 8}px`;
    toolbar.style.transform = "translateX(-50%)";
  } else {
    toolbar.style.top = `${rect.top - 8}px`;
    toolbar.style.transform = "translate(-50%, -100%)";
  }
}

function showBubble(editor, toolbar) {
  if (!toolbar) return;
  // Take the toolbar out of flow BEFORE revealing it. The toolbar sits
  // in the DOM ahead of the editor mount; showing it in-flow first would
  // push the editor down by the toolbar's height, so the selection coords
  // we then read are skewed by exactly that height — which dropped the
  // bubble onto the selected word. Going fixed first means display never
  // shifts the layout.
  toolbar.style.position = "fixed";
  toolbar.style.display = "";
  positionBubble(editor, toolbar);
}

function hideBubble(toolbar) {
  if (toolbar) toolbar.style.display = "none";
}

function initEditor(root) {
  if (INSTANCES.has(root)) {
    return INSTANCES.get(root);
  }

  const mount = root.querySelector(".description-editor-mount");
  const toolbar = root.querySelector(".description-editor-toolbar");
  const source = root.querySelector(".description-editor-source");
  // ``[data-editor-output]`` is the hidden input the editor's markdown
  // value gets piped into so the surrounding form POSTs the latest
  // text. Description and comment forms both wire their own input
  // through this marker so the same JS works for both.
  const hidden = root.querySelector("[data-editor-output]");
  const fallback = root.querySelector(".description-editor-fallback");
  const autosave = root.dataset.noAutosave === undefined;
  if (!mount || !source || !hidden) {
    return null;
  }

  // Defensive cleanup: wipe any leftover ProseMirror markup inside the
  // mount before TipTap creates its own. When HTMX swaps the
  // description / comment partial in fast succession (e.g., user types
  // a comment while a peer SSE event lands), the WeakMap-based instance
  // guard above can race with TipTap's DOM insertion and we end up with
  // two ``.ProseMirror`` siblings in one mount — visually a duplicated
  // editor. Clearing innerHTML before init makes the second insertion
  // overwrite cleanly.
  mount.innerHTML = "";

  const initialMarkdown = source.value || "";

  const editor = new Editor({
    element: mount,
    extensions: [
      StarterKit.configure({
        // Heading default allows h1-h6; we cap at h3 to match how the
        // server renders markdown (h1/h2 collide with the page title).
        heading: { levels: [2, 3] },
        codeBlock: { HTMLAttributes: { class: "bg-zinc-800 rounded p-2 text-sm" } },
      }),
      Link.configure({
        // Click to navigate (opens in a new tab thanks to the
        // target="_blank" attribute). Editing a link goes through the
        // toolbar link button — select text containing the link, click
        // 🔗, the inline URL field is pre-filled with the current href.
        openOnClick: true,
        HTMLAttributes: { rel: "noopener noreferrer nofollow", target: "_blank" },
      }),
      Placeholder.configure({
        placeholder: root.dataset.placeholder || "Write a description…",
      }),
      TaskList,
      TaskItem.configure({
        nested: true,
      }),
      Highlight.configure({
        multicolor: false,
      }),
      Typography,
      Markdown.configure({
        html: false,
        breaks: false,
        transformPastedText: true,
      }),
      // @-mention picker (users + tasks) — only when the mount declares
      // a search endpoint via ``data-mention-url``.
      ...(root.dataset.mentionUrl ? [buildMention(root.dataset.mentionUrl)] : []),
    ],
    content: initialMarkdown,
    editorProps: {
      attributes: {
        // ``prose-invert`` is gated on the dark mode class so the
        // editor renders with dark text on the white surface in
        // light mode (without this it inherits the invert palette
        // both ways and shows pale-grey text on white). No min-height
        // here — the editor auto-grows with content; each mount sets its
        // own floor (comments stay short, descriptions reserve more).
        class: "prose dark:prose-invert prose-sm max-w-none focus:outline-none",
      },
    },
    onUpdate({ editor }) {
      // Keep the hidden input in sync so the form POST carries the
      // latest markdown without an extra step.
      hidden.value = editor.storage.markdown.getMarkdown();
      // Reactive signal for any surrounding Alpine form to enable /
      // disable its submit button. Comment form uses this to grey
      // out "Post comment" until the editor has content.
      root.dispatchEvent(
        new CustomEvent("editor:change", {
          detail: { empty: editor.isEmpty, markdown: hidden.value },
          bubbles: true,
        }),
      );
    },
    onSelectionUpdate({ editor }) {
      // Selection bubble: show the toolbar over a non-empty selection
      // while the editor is focused; hide it the moment the selection
      // collapses. Positioned from selection coords, no library.
      if (toolbar && editor.isFocused && !editor.state.selection.empty) {
        showBubble(editor, toolbar);
      } else {
        hideBubble(toolbar);
      }
    },
    onFocus({ editor }) {
      // Re-show if focus returns to a still-selected range (e.g. after
      // applying a link from the toolbar's own input).
      if (toolbar && !editor.state.selection.empty) {
        showBubble(editor, toolbar);
      }
    },
    onBlur({ editor, event }) {
      // Hide the bubble on blur — unless focus moved into the toolbar
      // itself (the link-URL input), so editing a link doesn't make its
      // own input vanish. Clicking a formatting button keeps editor focus
      // (mousedown preventDefault below), so onBlur doesn't fire for those.
      if (toolbar && !(event && event.relatedTarget && toolbar.contains(event.relatedTarget))) {
        hideBubble(toolbar);
      }
      // Comment editor opts out of blur-save: comments need an
      // explicit submit, not autosave on every focus change.
      if (!autosave) {
        return;
      }
      // Skip save when the whole window / tab lost focus
      // (Cmd+Tab to another app, mouse outside the browser). Saving
      // here races with mount / unmount timing and has been observed
      // to clobber the description with an intermediate empty value.
      // Real page-unload saves are handled by the pagehide beacon.
      if (!document.hasFocus()) {
        return;
      }
      const current = editor.storage.markdown.getMarkdown();
      hidden.value = current;
      const form = hidden.form;
      const baseline = root.dataset.baseline || "";
      if (current === baseline) {
        return;
      }
      // Bump the baseline up-front so a quick second blur with the
      // same text doesn't fire a duplicate save (the response itself
      // doesn't re-render the cell — see the endpoint comment).
      root.dataset.baseline = current;
      form.requestSubmit();
    },
  });

  // Initial sync so the hidden input matches what the editor shows.
  hidden.value = editor.storage.markdown.getMarkdown();

  let bubbleScroll = null;
  if (toolbar) {
    // Toolbar buttons reach this editor via
    // ``closest('.description-editor-toolbar')._editor``.
    toolbar._editor = editor;
    // Toolbar buttons must not steal focus from the editor on click,
    // otherwise the chain commands they dispatch fire against an
    // empty selection — and the blur would hide the bubble mid-click.
    // preventDefault on mousedown keeps focus inside the editor view.
    // Inputs (e.g. the link-URL field) are exempt — they *should* take
    // focus while the user types.
    toolbar.addEventListener("mousedown", (event) => {
      if (event.target instanceof HTMLInputElement) return;
      event.preventDefault();
    });
    // Hide the fixed-positioned bubble while scrolling (it would
    // otherwise drift away from the selection); it re-shows on the next
    // selection change. Capture-phase to catch scroll in any container.
    // Removed in the cleanup hook below.
    bubbleScroll = () => hideBubble(toolbar);
    window.addEventListener("scroll", bubbleScroll, true);
  }

  INSTANCES.set(root, editor);
  // Expose the editor on the DOM node so inline toolbar buttons can
  // dispatch commands via ``root._editor.chain().focus().toggleX()``.
  root._editor = editor;

  // Editor is ready — swap the server-rendered fallback for the live
  // TipTap surface. Done in a microtask so the layout transition is
  // a single paint.
  if (fallback) {
    // ``style.display`` instead of just ``classList.add('hidden')``
    // — the ``.prose`` rules carry the same specificity as ``.hidden``
    // and load after in some bundles, leaving the fallback visible
    // next to the live editor. Inline style wins unambiguously.
    fallback.classList.add("hidden");
    fallback.style.display = "none";
  }
  mount.classList.remove("hidden");
  mount.style.display = "";

  // Pagehide beacon: post the latest markdown if it differs from the
  // baseline. WeakMap entry will be garbage-collected when root is
  // removed from DOM during HTMX swap; new mount runs initEditor again.
  // Skipped for comments (no autosave) — half-typed comments
  // shouldn't be flushed as accidental posts on tab close.
  function onPageHide() {
    if (!autosave) return;
    const current = editor.storage.markdown.getMarkdown();
    const baseline = root.dataset.baseline || "";
    if (current === baseline) return;
    hidden.value = current;
    const form = hidden.form;
    if (!form) return;
    navigator.sendBeacon(form.action, new FormData(form));
  }
  window.addEventListener("pagehide", onPageHide, { once: false });

  // Destroy editor when root is removed (HTMX swap replaces it).
  // We rely on htmx:beforeCleanupElement.
  const cleanup = (event) => {
    if (event.detail && event.detail.elt === root) {
      window.removeEventListener("pagehide", onPageHide);
      if (bubbleScroll) window.removeEventListener("scroll", bubbleScroll, true);
      editor.destroy();
      document.body.removeEventListener("htmx:beforeCleanupElement", cleanup);
    }
  };
  document.body.addEventListener("htmx:beforeCleanupElement", cleanup);

  return editor;
}

function mountAll(within = document) {
  within.querySelectorAll("[data-description-editor]").forEach(initEditor);
}

// Initial mount on page load.
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => mountAll());
} else {
  mountAll();
}

// Re-mount after HTMX swaps (e.g., navigating tasks, re-rendering the
// description cell).
document.body.addEventListener("htmx:afterSwap", (event) => {
  mountAll(event.detail.target);
});
