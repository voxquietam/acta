"""Small template filters used by the ``web`` app templates."""

from django import template

register = template.Library()


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
