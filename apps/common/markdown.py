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
ALLOWED_ATTRS = {
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
}
ALLOWED_PROTOCOLS = [
    "http",
    "https",
    "mailto",
]

_MD_EXTENSIONS = [
    "fenced_code",
    "tables",
    "nl2br",
    "sane_lists",
]


def render_markdown(text: str | None) -> str:
    """Render Markdown source to sanitized HTML.

    Empty input returns an empty string. The output is safe to inject
    directly into a template via ``{{ value|safe }}`` since bleach has
    already enforced the tag / attribute allowlists.

    Args:
        text: Raw Markdown source. ``None`` is treated as empty.

    Returns:
        HTML string sanitized against XSS-prone tags and attributes.
    """
    if not text:
        return ""
    rendered = markdown.markdown(text, extensions=_MD_EXTENSIONS, output_format="html")
    return bleach.clean(
        rendered,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )
