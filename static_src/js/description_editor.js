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
import BubbleMenu from "@tiptap/extension-bubble-menu";
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
  const bubble = root.querySelector(".description-editor-bubble");
  const source = root.querySelector(".description-editor-source");
  const hidden = root.querySelector('input[name="description"]');
  const fallback = root.querySelector(".description-editor-fallback");
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
        openOnClick: false,
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
      ...(bubble
        ? [
            BubbleMenu.configure({
              element: bubble,
              // Strict visibility: editor must own DOM focus AND have a
              // non-empty text selection. The plugin's default already
              // checks ``view.hasFocus()``, but in our flow we've seen
              // the bubble linger after focus moved to another element
              // (e.g. clicking a comment) — likely a stale selection
              // re-evaluation. Re-asserting both conditions here keeps
              // the bubble from popping up over the description while
              // the user is interacting with something else.
              shouldShow: ({ view, state, from, to }) => {
                // Belt-and-braces focus check: the plugin's default
                // ``view.hasFocus()`` can race with the live DOM state
                // when blur events haven't propagated yet, leading to
                // the bubble briefly re-showing after focus moved
                // elsewhere. ``document.activeElement`` reflects the
                // current focused node synchronously.
                if (document.activeElement !== view.dom) return false;
                if (!view.hasFocus()) return false;
                if (state.selection.empty) return false;
                const text = state.doc.textBetween(from, to);
                return text.length > 0;
              },
            }),
          ]
        : []),
    ],
    content: initialMarkdown,
    editorProps: {
      attributes: {
        class: "prose prose-invert prose-sm max-w-none focus:outline-none min-h-[6rem]",
      },
    },
    onUpdate({ editor }) {
      // Keep the hidden input in sync so the form POST carries the
      // latest markdown without an extra step.
      hidden.value = editor.storage.markdown.getMarkdown();
    },
    onBlur({ editor, event }) {
      // Bubble menu clicks fire blur; ignore those by checking whether
      // focus moved into the bubble UI.
      if (event && event.relatedTarget && bubble && bubble.contains(event.relatedTarget)) {
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
      form.requestSubmit();
    },
  });

  // Initial sync so the hidden input matches what the editor shows.
  hidden.value = editor.storage.markdown.getMarkdown();

  if (bubble) {
    // Prevent toolbar buttons from stealing focus on click. Without
    // this, mousedown on a button blurs the editor → the blur
    // handler below hides the bubble → the click never lands on the
    // button. preventDefault on mousedown keeps focus inside the
    // editor so the chain command fires against the real selection.
    // Exception: inputs *must* be focusable (e.g. the link-URL field
    // that replaces the toolbar in "link" mode).
    bubble.addEventListener("mousedown", (event) => {
      if (event.target instanceof HTMLInputElement) return;
      event.preventDefault();
    });

    // Force-hide the bubble when the editor loses focus to something
    // outside the bubble itself. tippy's visibility is driven by
    // ProseMirror transactions; clicking outside the editor (into
    // the comment textarea, another input, anywhere) doesn't fire
    // one, so the bubble would linger at its last selection position.
    editor.on("blur", ({ event }) => {
      if (event && event.relatedTarget && bubble.contains(event.relatedTarget)) {
        return;
      }
      bubble.style.display = "none";
    });
    editor.on("focus", () => {
      bubble.style.display = "";
    });

    // Click outside editor / bubble → force-blur the editor. Without
    // this, clicks on non-focusable elements (plain text, headings,
    // ``<span>``s) don't shift focus away from the editor view, so
    // the bubble lingers at the last selection. Force-blurring sends
    // ProseMirror through its real blur path, which lets the editor
    // ``blur`` handler above hide the bubble.
    const onDocumentMouseDown = (event) => {
      if (root.contains(event.target)) return;
      if (bubble && bubble.contains(event.target)) return;
      if (editor.isFocused) {
        editor.commands.blur();
      }
    };
    document.addEventListener("mousedown", onDocumentMouseDown);
    document.body.addEventListener("htmx:beforeCleanupElement", function cleanup(e) {
      if (e.detail && e.detail.elt === root) {
        document.removeEventListener("mousedown", onDocumentMouseDown);
        document.body.removeEventListener("htmx:beforeCleanupElement", cleanup);
      }
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
  function onPageHide() {
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
