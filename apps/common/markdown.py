"""Markdown rendering with HTML sanitization.

Shared by serializers that expose ``*_html`` companion fields next to
their raw markdown source: :class:`Task.description`,
:class:`Comment.body`, :class:`ProjectUpdate.body`. See
docs/decisions/0014-frontend-architecture.md for the rationale of
server-side rendering.
"""

import re

import bleach
import markdown

ALLOWED_TAGS = [
    "a",
    "blockquote",
    "br",
    "code",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "input",
    "li",
    "mark",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
]
_ALLOWED_ATTRS_BY_TAG = {
    "a": [
        "href",
        "title",
        "rel",
        "target",
    ],
    "img": [
        "src",
        "alt",
        "title",
    ],
    "li": [
        "class",
    ],
    "ul": [
        "class",
    ],
}
ALLOWED_PROTOCOLS = [
    "http",
    "https",
    "mailto",
]

# Mention tokens are stored in Markdown as links with custom schemes so
# they survive the TipTap ↔ markdown round-trip:
#   user: ``[@username](mention:<id>)``  → an inline chip + hover card
#   task: ``[ACTA-128](task:<id>)``      → a chip-link to the task
# These are rewritten to chips *after* the markdown render but *before*
# bleach, so the hardened attribute filter below governs what survives.
_USER_MENTION_RE = re.compile(r'<a href="mention:(\d+)">(.*?)</a>')
_TASK_MENTION_RE = re.compile(r'<a href="task:(\d+)">(.*?)</a>')

_MD_EXTENSIONS = [
    "fenced_code",
    "tables",
    "sane_lists",
    "pymdownx.tasklist",
    # ``==text==`` -> <mark>text</mark>. Matches the syntax TipTap's
    # Highlight extension serializes to, so a yellow-highlighted span
    # in the editor survives the round-trip through the server render.
    "pymdownx.mark",
]
_MD_EXTENSION_CONFIGS = {
    "pymdownx.tasklist": {
        "custom_checkbox": False,
        "clickable_checkbox": False,
    },
}


def _attr_filter(tag, name, value):
    """Per-tag attribute allowlist used by bleach.

    ``<input>`` is allowed only for the task-list extension; we restrict
    it hard to ``type="checkbox"`` with optional ``disabled`` / ``checked``.
    Any other ``<input>`` shape (text, hidden, etc.) is stripped, so the
    XSS surface from rendering user-supplied Markdown stays nil.

    Args:
        tag: The HTML tag being filtered (e.g. ``"a"``, ``"input"``).
        name: The attribute name.
        value: The attribute value.

    Returns:
        ``True`` if the attribute is allowed, ``False`` otherwise.
    """
    if tag == "input":
        if name == "type":
            return value == "checkbox"
        return name in {"disabled", "checked"}
    # Mention chips are emitted by :func:`_render_mentions`, but a user
    # could also type a literal ``<span class=...>`` in their Markdown —
    # so lock the surviving span/anchor mention attributes to exactly the
    # shapes we generate (no arbitrary class / data-* injection).
    if tag == "span":
        if name == "class":
            return value == "acta-mention"
        if name == "data-user-id":
            return value.isdigit()
        return False
    if tag == "a":
        if name == "class":
            return value == "acta-task-mention"
        if name == "data-task-id":
            return value.isdigit()
        return name in _ALLOWED_ATTRS_BY_TAG.get("a", [])
    return name in _ALLOWED_ATTRS_BY_TAG.get(tag, [])


def _render_mentions(html: str) -> str:
    """Rewrite mention link tokens into chip markup.

    Runs on the markdown-rendered HTML before bleach. User mentions
    become an inline ``<span class="acta-mention" data-user-id>`` (the
    hover-card + brand chip is wired client-side); task mentions become a
    chip ``<a class="acta-task-mention">`` whose href is derived from the
    slug label (``ACTA-128`` → ``/projects/ACTA/128/``), so no DB lookup
    is needed at render time.

    Args:
        html: HTML produced by ``markdown.markdown``.

    Returns:
        HTML with mention links replaced by chip markup.
    """

    def _task(match: re.Match) -> str:
        task_id, label = match.group(1), match.group(2)
        # The label may carry the title after the slug ("AUD-180 Title"),
        # so the URL is derived from the leading slug token only.
        slug = label.split(" ", 1)[0]
        prefix, _, number = slug.rpartition("-")
        href = f"/projects/{prefix}/{number}/" if prefix and number.isdigit() else "#"
        return f'<a class="acta-task-mention" data-task-id="{task_id}" href="{href}">{label}</a>'

    html = _USER_MENTION_RE.sub(r'<span class="acta-mention" data-user-id="\1">\2</span>', html)
    html = _TASK_MENTION_RE.sub(_task, html)
    return html


def render_markdown(text: str | None) -> str:
    """Render Markdown source to sanitized HTML.

    Empty input returns an empty string. The output is safe to inject
    directly into a template via ``{{ value|safe }}`` since bleach has
    already enforced the tag / attribute allowlists. Supports GitHub-style
    task lists (``- [ ]``, ``- [x]``) via the ``pymdownx.tasklist``
    extension; the rendered ``<input>`` elements are constrained to
    ``type="checkbox"`` by :func:`_attr_filter`.

    Args:
        text: Raw Markdown source. ``None`` is treated as empty.

    Returns:
        HTML string sanitized against XSS-prone tags and attributes.
    """
    if not text:
        return ""
    rendered = markdown.markdown(
        text,
        extensions=_MD_EXTENSIONS,
        extension_configs=_MD_EXTENSION_CONFIGS,
        output_format="html",
    )
    rendered = _render_mentions(rendered)
    cleaned = bleach.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes=_attr_filter,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
    # Force ``rel="noopener noreferrer nofollow"`` on every link so a
    # user-supplied ``[click](https://evil)`` can't use ``window.opener``
    # to navigate this tab once opened in a new one, and so we don't
    # bleed referrer or pagerank to arbitrary destinations.
    return bleach.linkifier.Linker(
        callbacks=[_force_safe_rel],
        skip_tags=[],
        parse_email=False,
    ).linkify(cleaned)


def _force_safe_rel(attrs, new=False):
    """Bleach linkify callback that hardens every rendered anchor.

    Args:
        attrs: Bleach's per-link attribute dict, keyed by
            ``(namespace, name)`` tuples.
        new: ``True`` for new auto-linked text, ``False`` for an
            anchor already in the input. We apply the same hardening
            in both cases.

    Returns:
        The mutated ``attrs`` dict. ``rel`` is always set to
        ``noopener noreferrer nofollow``; ``target="_blank"`` so the
        link opens in a new tab.
    """
    href = attrs.get((None, "href"), "")
    # Internal links (task-mention chips → ``/projects/...``) stay in the
    # same tab; only external destinations open in a new tab + get the
    # full nofollow hardening.
    if href.startswith("/"):
        attrs[(None, "rel")] = "noopener noreferrer"
        attrs.pop((None, "target"), None)
        return attrs
    attrs[(None, "rel")] = "noopener noreferrer nofollow"
    attrs[(None, "target")] = "_blank"
    return attrs
