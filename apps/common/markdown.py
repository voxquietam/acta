"""Markdown rendering with HTML sanitization.

Shared by serializers that expose ``*_html`` companion fields next to
their raw markdown source: :class:`Task.description`,
:class:`Comment.body`, :class:`ProjectUpdate.body`. See
docs/decisions/0014-frontend-architecture.md for the rationale of
server-side rendering.
"""

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
    "ol",
    "p",
    "pre",
    "strong",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
]
_ALLOWED_ATTRS_BY_TAG = {
    "a": [
        "href",
        "title",
        "rel",
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

_MD_EXTENSIONS = [
    "fenced_code",
    "tables",
    "sane_lists",
    "pymdownx.tasklist",
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
    return name in _ALLOWED_ATTRS_BY_TAG.get(tag, [])


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
    return bleach.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes=_attr_filter,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
