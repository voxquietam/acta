// Description editor — TipTap (ProseMirror) instance with a bubble
// menu on selection, Linear-style. Output is markdown via
// tiptap-markdown so the server-side render pipeline
// (apps/common/markdown.render_markdown) stays the source of truth
// when reading.
//
// Mount: any element with [data-description-editor] containing a
// nested .description-editor-mount (the editable surface) and a
// .description-editor-bubble (the floating toolbar template). The
// initial markdown source is read from a sibling
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
import Underline from "@tiptap/extension-underline";
import TaskList from "@tiptap/extension-task-list";
import TaskItem from "@tiptap/extension-task-item";
import Highlight from "@tiptap/extension-highlight";
import Typography from "@tiptap/extension-typography";
import { Markdown } from "tiptap-markdown";

const INSTANCES = new WeakMap();

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
        // bubble-menu link button — select text containing the link,
        // click 🔗, prompt is pre-filled with the current href.
        openOnClick: true,
        HTMLAttributes: { rel: "noopener noreferrer nofollow", target: "_blank" },
      }),
      Placeholder.configure({
        placeholder: root.dataset.placeholder || "Write a description…",
      }),
      Underline,
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
    ],
    content: initialMarkdown,
    editorProps: {
      attributes: {
        // ``prose-invert`` is gated on the dark mode class so the
        // editor renders with dark text on the white surface in
        // light mode (without this it inherits the invert palette
        // both ways and shows pale-grey text on white).
        class: "prose dark:prose-invert prose-sm max-w-none focus:outline-none min-h-[6rem]",
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
    onBlur({ editor }) {
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

  if (toolbar) {
    // Toolbar buttons must not steal focus from the editor on click,
    // otherwise the chain commands they dispatch fire against an
    // empty selection. preventDefault on mousedown keeps focus
    // inside the editor view. Inputs (e.g. the link-URL field) are
    // exempt — they *should* take focus while the user types.
    toolbar.addEventListener("mousedown", (event) => {
      if (event.target instanceof HTMLInputElement) return;
      event.preventDefault();
    });
  }

  INSTANCES.set(root, editor);
  // Expose the editor on the DOM node so inline toolbar buttons can
  // dispatch commands via ``root._editor.chain().focus().toggleX()``.
  root._editor = editor;

  // Editor is ready — swap the server-rendered fallback for the live
  // TipTap surface. Done in a microtask so the layout transition is
  // a single paint.
  if (fallback) {
    fallback.classList.add("hidden");
  }
  mount.classList.remove("hidden");

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
