"""Shared task-list filter logic.

Used by AllTasksView, MyWorkView, and ProjectDetailView — they all
filter the same kind of Task queryset by the same set of querystring
params. This module centralises the parsing + filter-application so
the three views share one canonical implementation.
"""

from django.contrib.auth import get_user_model
from django.db.models import Case, F, IntegerField, Q, Value, When
from django.db.models.functions import Lower
from django.utils import timezone

from apps.labels.models import Label
from apps.projects.models import Project
from apps.tasks.models import Task


def apply_task_filters(qs, params, *, request_user, default_show_done=True):
    """Apply querystring filters to a Task queryset.

    Each field is handled by a focused helper so this function stays
    flat and the per-field logic (include + exclude pair) is local.
    Add a new filter dimension by adding a helper and one extra call
    here.

    Args:
        qs: Base Task queryset (already scoped).
        params: ``request.GET``-like mapping with ``getlist``.
        request_user: For the ``assignee=me`` filter shortcut.
        default_show_done: When False, done tasks are excluded unless
            the caller opts in via explicit ``status``. All pages
            currently default True (done stays visible) — the flag is
            kept as a seam for a future per-user preference where the
            user can toggle "always hide done" once and for all in
            their settings.
    """
    qs = _filter_status(qs, params, default_show_done=default_show_done)
    qs = _filter_int_field(qs, params, field="priority", include="priority", exclude="xpriority")
    qs = _filter_int_field(qs, params, field="project_id", include="project", exclude="xproject")
    qs = _filter_int_field(
        qs,
        params,
        field="project__workspace_id",
        include="workspace",
        exclude="xworkspace",
    )
    qs = _filter_assignee(qs, params, request_user)
    qs = _filter_labels(qs, params)
    qs = _filter_search(qs, params)
    return qs


def _filter_status(qs, params, *, default_show_done):
    """Apply ``status`` / ``xstatus`` (logical workflow column)."""
    statuses = params.getlist("status")
    if statuses:
        qs = qs.filter(status__in=statuses)
    elif not default_show_done:
        qs = qs.exclude(status=Task.STATUS_DONE)
    excluded = params.getlist("xstatus")
    if excluded:
        qs = qs.exclude(status__in=excluded)
    return qs


def _filter_int_field(qs, params, *, field, include, exclude):
    """Generic include/exclude pair for an integer-FK or enum column.

    Args:
        qs: Queryset to narrow.
        params: ``request.GET``-like mapping.
        field: ORM field path (e.g. ``"priority"``, ``"project_id"``).
        include: Querystring key for inclusion.
        exclude: Querystring key for exclusion (``x<include>``).
    """
    ins = _safe_int_list(params.getlist(include))
    if ins:
        qs = qs.filter(**{f"{field}__in": ins})
    outs = _safe_int_list(params.getlist(exclude))
    if outs:
        qs = qs.exclude(**{f"{field}__in": outs})
    return qs


def _assignee_q(values, request_user):
    """Build a ``Q`` clause from a list of ``assignee`` querystring values."""
    q = Q()
    user_ids = []
    for a in values:
        if a == "me":
            q |= Q(assignee=request_user)
        elif a == "unassigned":
            q |= Q(assignee__isnull=True)
        else:
            try:
                user_ids.append(int(a))
            except (TypeError, ValueError):
                pass
    if user_ids:
        q |= Q(assignee_id__in=user_ids)
    return q


def _filter_assignee(qs, params, request_user):
    """Apply ``assignee`` / ``xassignee`` with ``me`` / ``unassigned`` tokens."""
    assignees = params.getlist("assignee")
    if assignees:
        qs = qs.filter(_assignee_q(assignees, request_user))
    excluded = params.getlist("xassignee")
    if excluded:
        qs = qs.exclude(_assignee_q(excluded, request_user))
    return qs


def _filter_labels(qs, params):
    """Apply ``label`` / ``xlabel`` — exclude uses a subquery."""
    ins = _safe_int_list(params.getlist("label"))
    if ins:
        qs = qs.filter(labels__id__in=ins).distinct()
    outs = _safe_int_list(params.getlist("xlabel"))
    if outs:
        # Plain ``exclude(labels__id__in=...)`` drops a task if ANY of
        # its labels matches; we want "drop tasks that carry this label
        # at all". Resolve matching ids in a subquery first.
        matching = Task.objects.filter(labels__id__in=outs).values_list("id", flat=True)
        qs = qs.exclude(id__in=matching)
    return qs


def _filter_search(qs, params):
    """Apply ``?q=`` full-text search over title + description."""
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
    return qs


def _safe_int_list(raw_values):
    """Parse a list of querystring values into ints, dropping non-numeric entries."""
    out = []
    for v in raw_values:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            pass
    return out


# Smart ordering rank for status: logical workflow order rather than
# alphabetical. ``default=99`` keeps any future / unknown status code
# from accidentally surfacing above meaningful ones.
_STATUS_ORDER = Case(
    When(status=Task.STATUS_PLANNED, then=Value(0)),
    When(status=Task.STATUS_TODO, then=Value(1)),
    When(status=Task.STATUS_IN_PROGRESS, then=Value(2)),
    When(status=Task.STATUS_IN_REVIEW, then=Value(3)),
    When(status=Task.STATUS_DONE, then=Value(4)),
    default=Value(99),
    output_field=IntegerField(),
)
# Priority needs a two-stage rank: the int values 1-4 already form the
# correct urgent → low sequence, but the special ``NO_PRIORITY=0`` value
# must sink to the bottom regardless of direction (it's "absence" of
# priority, not a low priority). Sort by the "is no-priority" flag
# first (ascending — real values first, no-prio last), then by the
# raw ``priority`` field in the user-chosen direction.
_PRIORITY_NOPRIO_LAST = Case(
    When(priority=Task.NO_PRIORITY, then=Value(1)),
    default=Value(0),
    output_field=IntegerField(),
)

# Columns the table header may sort by. ``"id"`` uses ``number`` so the
# sequential per-project counter sorts numerically (slug prefixes never
# differ within a single project, and across-project numeric ordering
# is still meaningful — newer tasks have higher numbers).
SORTABLE_COLUMNS = ("id", "title", "status", "priority", "size", "assignee", "project", "due", "updated")


def apply_task_ordering(qs, params, *, default_ordering=("-updated_at",)):
    """Apply a smart ``?order=`` clause to a Task queryset.

    For enum columns (``status``, ``priority``) the order is logical
    (planned → done; urgent → low) rather than alphabetical. For
    ``title`` we lowercase to ignore case. ``assignee`` sorts by name
    with unassigned rows sinking to the bottom in both directions
    (same for nullable ``size`` and ``due_date``).

    Args:
        qs: Base Task queryset.
        params: ``request.GET``-like mapping.
        default_ordering: Tuple of order_by terms used when ``order``
            is absent or invalid.

    Returns:
        The queryset with ``.order_by(...)`` applied.
    """
    raw = (params.get("order") or "").strip()
    direction = "desc" if raw.startswith("-") else "asc"
    key = raw.lstrip("-")
    if key not in SORTABLE_COLUMNS:
        return qs.order_by(*default_ordering)

    def directed(expr):
        return expr.desc() if direction == "desc" else expr.asc()

    def directed_nulls_last(field_name):
        f = F(field_name)
        return f.desc(nulls_last=True) if direction == "desc" else f.asc(nulls_last=True)

    if key == "id":
        # Group by project first so cross-project lists (All Tasks)
        # keep ``AUD-1, AUD-2 … AUD-205, MYP-1, MYP-2 …`` instead of
        # interleaving identical numbers from different projects.
        # Within a single project the secondary key is the only thing
        # that matters.
        clauses = [directed(F("project__slug_prefix")), directed(F("number"))]
    elif key == "title":
        clauses = [directed(Lower("title"))]
    elif key == "status":
        clauses = [directed(_STATUS_ORDER), "-priority", "-updated_at"]
    elif key == "priority":
        clauses = [_PRIORITY_NOPRIO_LAST.asc(), directed(F("priority")), "-updated_at"]
    elif key == "size":
        clauses = [directed_nulls_last("size"), "-updated_at"]
    elif key == "assignee":
        # Compound name sort: surname-style alphabetical, unassigned last.
        if direction == "desc":
            clauses = [
                F("assignee__first_name").desc(nulls_last=True),
                F("assignee__last_name").desc(nulls_last=True),
                F("assignee__username").desc(nulls_last=True),
            ]
        else:
            clauses = [
                F("assignee__first_name").asc(nulls_last=True),
                F("assignee__last_name").asc(nulls_last=True),
                F("assignee__username").asc(nulls_last=True),
            ]
    elif key == "project":
        clauses = [directed(Lower("project__name"))]
    elif key == "due":
        clauses = [directed_nulls_last("due_date"), "-priority"]
    else:  # "updated"
        clauses = [directed(F("updated_at"))]

    return qs.order_by(*clauses)


def filter_sidebar_context(
    request,
    *,
    available_projects=None,
    available_workspaces=None,
    available_labels=None,
    available_assignees=None,
    hide_assignee=False,
    hide_workspace=False,
    hide_project=False,
    preserved_params=None,
    form_url=None,
    htmx_target=None,
):
    """Build the context dict the ``_filters_sidebar.html`` partial expects.

    Args:
        request: The active ``HttpRequest``.
        available_projects / workspaces / labels: Optional querysets /
            lists. If ``None``, computed from the user's accessible
            data.
        hide_assignee / hide_workspace / hide_project: Sections the
            sidebar should not render (e.g. assignee on My Work,
            project on per-project view).
        form_url: Action URL for the filter form. Defaults to the
            current path.
        htmx_target: CSS selector for the HTMX swap target.

    Returns:
        Dict that should be merged into the view's context.
    """
    user = request.user
    params = request.GET

    if available_projects is None:
        available_projects = list(
            Project.objects.filter(workspace__memberships__user=user)
            .select_related("workspace")
            .order_by("workspace__name", "name")
            .distinct(),
        )
    if available_workspaces is None:
        available_workspaces = list(user.workspaces.order_by("name").distinct())
    if available_labels is None:
        available_labels = list(
            Label.objects.filter(workspace__memberships__user=user).order_by("name").distinct(),
        )
    if available_assignees is None:
        User = get_user_model()
        available_assignees = list(
            User.objects.filter(
                workspace_memberships__workspace__memberships__user=user,
            )
            .order_by("username")
            .distinct(),
        )

    selected_statuses = set(params.getlist("status"))
    selected_priorities = {int(p) for p in params.getlist("priority") if p.isdigit()}
    selected_projects = {int(p) for p in params.getlist("project") if p.isdigit()}
    selected_workspaces = {int(w) for w in params.getlist("workspace") if w.isdigit()}
    selected_labels = {int(i) for i in params.getlist("label") if i.isdigit()}
    selected_assignees = set(params.getlist("assignee"))

    # Excluded sets: right-click on a chip toggles a value into one of
    # these. Renders with a red strikethrough state; backend
    # ``apply_task_filters`` translates them into ``.exclude(...)``.
    excluded_statuses = set(params.getlist("xstatus"))
    excluded_priorities = {int(p) for p in params.getlist("xpriority") if p.isdigit()}
    excluded_projects = {int(p) for p in params.getlist("xproject") if p.isdigit()}
    excluded_workspaces = {int(w) for w in params.getlist("xworkspace") if w.isdigit()}
    excluded_labels = {int(i) for i in params.getlist("xlabel") if i.isdigit()}
    excluded_assignees = set(params.getlist("xassignee"))

    q = params.get("q", "")

    active_filter_count = (
        (1 if q else 0)
        + len(selected_assignees)
        + len(selected_statuses)
        + len(selected_priorities)
        + len(selected_workspaces)
        + len(selected_projects)
        + len(selected_labels)
        + len(excluded_assignees)
        + len(excluded_statuses)
        + len(excluded_priorities)
        + len(excluded_workspaces)
        + len(excluded_projects)
        + len(excluded_labels)
    )

    preserved_pairs = []
    for key in preserved_params or ():
        for value in params.getlist(key):
            preserved_pairs.append((key, value))

    return {
        "filter_form_url": form_url or request.path,
        "filter_htmx_target": htmx_target or "#task-list-wrapper",
        "filter_preserved_pairs": preserved_pairs,
        "filter_hide_assignee": hide_assignee,
        "filter_hide_workspace": hide_workspace,
        "filter_hide_project": hide_project,
        "selected_statuses": selected_statuses,
        "selected_priorities": selected_priorities,
        "selected_projects": selected_projects,
        "selected_workspaces": selected_workspaces,
        "selected_labels": selected_labels,
        "selected_assignees": selected_assignees,
        "excluded_statuses": excluded_statuses,
        "excluded_priorities": excluded_priorities,
        "excluded_projects": excluded_projects,
        "excluded_workspaces": excluded_workspaces,
        "excluded_labels": excluded_labels,
        "excluded_assignees": excluded_assignees,
        "q": q,
        "available_projects": available_projects,
        "available_workspaces": available_workspaces,
        "available_labels": available_labels,
        "available_assignees": available_assignees,
        "active_filter_count": active_filter_count,
        "status_labels": Task.STATUS_LABELS,
        "priority_labels": dict(Task.PRIORITY_CHOICES),
        "today": timezone.localdate(),
    }
