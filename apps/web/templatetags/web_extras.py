"""Small template filters used by the ``web`` app templates."""

import datetime
import html

from django import template
from django.utils import timezone
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from apps.common.markdown import render_markdown

register = template.Library()


@register.simple_tag(takes_context=True)
def task_filter_attrs(context, task):
    """Emit ``data-*`` attributes that drive client-side filtering.

    The client-side filter handler (acta.js) reads these attributes
    off each task row / card / list-item to decide visibility without
    a server round-trip. The same attribute set is rendered on every
    surface a task appears on — kanban card, table row, list row —
    so the filter logic stays single-pass.

    Attribute set, mirroring ``apply_task_filters`` in
    ``apps/web/filters.py``:

    * ``data-status`` — internal status key (``planned`` / ``to-do``…)
    * ``data-priority`` — integer 0..4
    * ``data-assignee-id`` — ``Task.assignee_id`` or empty
    * ``data-assignee-me`` — ``"1"`` when assigned to ``request.user``
    * ``data-project-id`` — ``Task.project_id``
    * ``data-workspace-id`` — ``Task.project.workspace_id``
    * ``data-label-ids`` — space-separated label PKs
    * ``data-archived`` — ``"1"`` if ``archived_at`` is set
    * ``data-overdue`` — ``"1"`` when ``due_date < today`` and not done
    * ``data-done-this-week`` — ``"1"`` when ``status == 'done'`` and
      ``updated_at`` within the last 7 days (for the Done column's
      "++ N this week" substatus recompute on client-side filter)
    * ``data-search-haystack`` — lowercased title + first 160 chars
      of description, used for substring search

    Args:
        context: Template context (carries ``request``).
        task: A :class:`Task` instance (with ``labels`` prefetched).

    Returns:
        A safe HTML string with all attributes; drop into any task
        wrapper as ``{% task_filter_attrs task %}``.
    """
    request = context.get("request")
    me_id = request.user.id if request and request.user.is_authenticated else None
    is_me = "1" if me_id and task.assignee_id == me_id else "0"
    label_ids = " ".join(str(label.id) for label in task.labels.all())
    description = (task.description or "")[:160]
    haystack = ((task.title or "") + " " + description).lower()
    today = timezone.localdate()
    week_cutoff = timezone.now() - datetime.timedelta(days=7)
    is_overdue = "1" if (task.due_date and task.due_date < today and task.status != "done") else "0"
    is_done_this_week = "1" if (task.status == "done" and task.updated_at and task.updated_at >= week_cutoff) else "0"
    attrs = (
        f'data-status="{html.escape(task.status or "")}" '
        f'data-priority="{task.priority or 0}" '
        f'data-assignee-id="{task.assignee_id or ""}" '
        f'data-assignee-me="{is_me}" '
        f'data-project-id="{task.project_id}" '
        f'data-workspace-id="{task.project.workspace_id}" '
        f'data-label-ids="{html.escape(label_ids)}" '
        f'data-archived="{"1" if task.archived_at else "0"}" '
        f'data-overdue="{is_overdue}" '
        f'data-done-this-week="{is_done_this_week}" '
        f'data-search-haystack="{html.escape(haystack, quote=True)}"'
    )
    return mark_safe(attrs)


@register.inclusion_tag("web/projects/_project_favourite_star.html")
def project_star(project, favourite_ids):
    """Render the project favourite-star toggle for a list card.

    ``favourite_ids`` is the set of project ids the current user has
    starred — passed once from the view (``ProjectListView`` adds
    ``favourite_project_ids`` to context) so each card's render is a
    set membership check, not a separate ``filter().exists()`` query.

    Args:
        project: The :class:`Project` being rendered.
        favourite_ids: Iterable of project ids the user has favourited.

    Returns:
        Context dict for ``_project_favourite_star.html``.
    """
    return {
        "project": project,
        "is_favourite": project.id in (favourite_ids or set()),
    }


@register.simple_tag
def open_task_modal_attrs(task):
    """Emit ``hx-*`` attributes that open the task in a modal on click.

    Plain click → ``HX-Get`` the task URL with ``?modal=1`` and swap
    the response into ``#modal-root``. Ctrl/Cmd/Shift/middle-click
    fail the ``hx-trigger`` filter, so HTMX skips the request and the
    surrounding ``<a href="…">`` falls through to native browser
    behaviour (open in new tab, etc.).

    ``hx-push-url`` is the bare task URL (no ``?modal=1``) so the
    address bar reflects the task while the modal is open; closing
    the modal pops the entry and returns to the previous view.

    Args:
        task: A :class:`Task` instance (needs ``project.slug_prefix``
            and ``number``).

    Returns:
        Safe HTML attribute string; drop into any ``<a>`` element that
        links to the task detail page.
    """
    url = f"/projects/{task.project.slug_prefix}/{task.number}/"
    attrs = (
        f'hx-get="{url}?modal=1" '
        f'hx-target="#modal-root" '
        f'hx-swap="innerHTML" '
        f'hx-push-url="{url}" '
        f'hx-trigger="click[!ctrlKey&amp;&amp;!metaKey&amp;&amp;!shiftKey]"'
    )
    return mark_safe(attrs)


@register.simple_tag
def url_replace(request, key, value):
    """Return the current querystring with one key replaced.

    Used for pagination links that need to keep every other filter
    param intact while bumping ``page``.

    Args:
        request: The active ``HttpRequest``.
        key: Querystring key to overwrite.
        value: New value for that key.

    Returns:
        URL-encoded querystring (no leading ``?``).
    """
    params = request.GET.copy()
    params[key] = value
    return params.urlencode()


@register.simple_tag
def sort_url(request, key):
    """Build the ``?order=`` URL for clicking a sortable column header.

    Cycles the column's state on each click: not-active → asc → desc →
    not-active. All other querystring params (filters, view mode) are
    preserved.

    Args:
        request: The active ``HttpRequest``.
        key: Column key from ``apps.web.filters.SORTABLE_COLUMNS``.

    Returns:
        Relative URL with the new ``order`` querystring applied.
    """
    current = request.GET.get("order", "")
    current_key = current.lstrip("-")
    current_dir = "desc" if current.startswith("-") else "asc"

    if current_key != key:
        next_order = key
    elif current_dir == "asc":
        next_order = f"-{key}"
    else:
        next_order = ""

    params = request.GET.copy()
    if next_order:
        params["order"] = next_order
    else:
        params.pop("order", None)
    qs = params.urlencode()
    return f"?{qs}" if qs else request.path


@register.simple_tag
def sort_indicator(request, key):
    """Return ``↑``/``↓`` for the active sort column, empty otherwise.

    Used inside sortable ``<th>`` blocks alongside the column label so
    users can spot the current sort direction at a glance.

    Args:
        request: The active ``HttpRequest``.
        key: Column key being rendered.

    Returns:
        ``"↑"`` if the column is sorted ascending, ``"↓"`` if descending,
        ``""`` if this column is not the active sort.
    """
    current = request.GET.get("order", "")
    if current.lstrip("-") != key:
        return ""
    return "↓" if current.startswith("-") else "↑"


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
    except (KeyError, TypeError, IndexError):
        # ``IndexError`` covers the edge case where ``mapping`` falls
        # back to Django's default ``string_if_invalid`` empty string
        # and ``key`` is an integer — ``"" [1]`` raises IndexError, not
        # the usual KeyError. Treat any "no such entry" outcome the
        # same way: render nothing.
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
