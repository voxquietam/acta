"""Small template filters used by the ``web`` app templates."""

from django import template
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from apps.common.markdown import render_markdown

register = template.Library()


_EVENT_LABELS = {
    "task.created": _("created the task"),
    "task.status_changed": _("changed status"),
    "task.assigned": _("changed assignee"),
    "task.due_changed": _("changed due date"),
    "task.priority_changed": _("changed priority"),
    "task.labels_changed": _("changed labels"),
    "task.parent_changed": _("changed parent"),
    "task.updated": _("updated fields"),
    "task.deleted": _("deleted the task"),
    "comment.created": _("added a comment"),
    "comment.edited": _("edited a comment"),
    "comment.deleted": _("deleted a comment"),
    "member.added": _("added a member"),
    "member.removed": _("removed a member"),
    "member.role_changed": _("changed a member role"),
}


@register.filter(name="get_item")
def get_item(mapping, key):
    """Return ``mapping[key]`` from inside a template.

    Django's template engine cannot index dicts by a dynamic variable
    using ``{{ d.key }}`` when ``key`` is a string variable, so this
    filter wraps the lookup. Used in the project table view to render
    status / priority display names from the ``STATUS_LABELS`` and
    ``PRIORITY_CHOICES`` dicts passed in context.

    Args:
        mapping: A dict-like object.
        key: The key to look up. Returns an empty string when missing.

    Returns:
        The mapped value, or an empty string if ``key`` is not present.
    """
    if mapping is None:
        return ""
    try:
        return mapping[key]
    except (KeyError, TypeError):
        return ""


@register.filter(name="markdown", is_safe=True)
def markdown_filter(text):
    """Render a Markdown string to sanitized HTML, ready to inject as-is.

    Thin wrapper over :func:`apps.common.markdown.render_markdown`
    marked as safe so the template doesn't need an additional
    ``|safe``. ``bleach`` has already enforced the tag/attribute
    allowlist server-side, so the output is XSS-safe.

    Args:
        text: Raw Markdown source. ``None`` is treated as empty.

    Returns:
        A :class:`SafeString` containing sanitized HTML.
    """
    return mark_safe(render_markdown(text))


@register.filter(name="event_label")
def event_label(event_type):
    """Return a human-readable label for an :class:`ActivityLog` event_type.

    Falls back to the raw event_type when the mapping has no entry, so a
    new event_type never breaks the template — it just shows the code
    until a translation is added.

    Args:
        event_type: The ``ActivityLog.event_type`` string, e.g.
            ``"task.status_changed"``.

    Returns:
        A translated, lowercase verb phrase suitable for the timeline.
    """
    return _EVENT_LABELS.get(event_type, event_type)
