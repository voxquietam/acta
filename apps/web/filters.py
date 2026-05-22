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
from apps.web.nav import resolve_active_workspace


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
    qs = _filter_archived(qs, params)
    qs = _filter_status(qs, params, default_show_done=default_show_done)
    qs = _filter_int_field(qs, params, field="priority", include="priority", exclude="xpriority")
    qs = _filter_int_field(qs, params, field="size", include="size", exclude="xsize")
    qs = _filter_int_field(qs, params, field="project_id", include="project", exclude="xproject")
    # Workspace is no longer a filter axis — it's the global active-workspace
    # scope (see apps.web.nav.resolve_active_workspace); the queryset reaching
    # here is already scoped to one workspace.
    qs = _filter_assignee(qs, params, request_user)
    qs = _filter_labels(qs, params)
    qs = _filter_search(qs, params)
    return qs


def _filter_archived(qs, params):
    """Exclude archived tasks unless ``?show_archived=1`` is set.

    Archive is orthogonal to status — an archived task keeps its
    ``done`` (or whichever) status so unarchiving restores it. We
    hide them by default everywhere; the toggle in the filter sidebar
    flips the flag for one request.

    The view layer (``resolve_show_archived``) merges querystring +
    cookie into ``params`` before calling here, so a ``True`` cookie
    persists across navigations without the user re-toggling.
    """
    if "1" in params.getlist("show_archived"):
        return qs
    return qs.filter(archived_at__isnull=True)


def resolve_show_archived(request):
    """Resolve the effective ``show_archived`` for this request.

    Order: ``?show_archived=`` querystring → ``acta_show_archived``
    cookie → False. Returns ``"1"`` / ``"0"`` suitable for stuffing
    back into a mutable ``params`` copy that ``apply_task_filters``
    reads.

    The querystring carries **two** ``show_archived`` values when the
    toggle submits — a hidden ``0`` (so unchecked sends an explicit
    value) and the checkbox ``1`` if checked. ``params.get`` would
    pick the first ("0") and break the toggle; we look for ``"1"`` in
    the list explicitly.
    """
    raw_list = request.GET.getlist("show_archived")
    if raw_list:
        return "1" if "1" in raw_list else "0"
    return "1" if request.COOKIES.get("acta_show_archived") == "1" else "0"


def _filter_status(qs, params, *, default_show_done):
    """Apply ``status`` / ``xstatus`` (logical workflow column).

    Cancelled tasks are terminal and hidden by default in every list /
    table / kanban queryset — only an explicit ``?status=cancelled``
    (the sidebar status chip) brings them back. This is more aggressive
    than done (which ``default_show_done`` keeps visible) because a
    cancelled task is "won't do" and should stay out of the way.
    """
    statuses = params.getlist("status")
    if statuses:
        qs = qs.filter(status__in=statuses)
    else:
        if not default_show_done:
            qs = qs.exclude(status=Task.STATUS_DONE)
        qs = qs.exclude(status=Task.STATUS_CANCELLED)
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
    When(status=Task.STATUS_CANCELLED, then=Value(5)),
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
    available_labels=None,
    available_assignees=None,
    hide_assignee=False,
    hide_project=False,
    hide_status=False,
    preserved_params=None,
    extra_preserved=None,
    effective_params=None,
    form_url=None,
    htmx_target=None,
):
    """Build the context dict the ``_filters_sidebar.html`` partial expects.

    Workspace is NOT a filter axis — it's the global active-workspace scope
    (see :func:`apps.web.nav.resolve_active_workspace`); the project / label
    / assignee options below are all scoped to that active workspace.

    Args:
        request: The active ``HttpRequest``.
        available_projects / labels: Optional querysets / lists. If
            ``None``, computed (scoped to the active workspace).
        hide_assignee / hide_project / hide_status:
            Sections the sidebar should not render (e.g. assignee on
            My Work, status on kanban view where columns already group
            by status).
        preserved_params: Names of querystring params to round-trip on
            filter submit by reading their current values from
            ``request.GET``.
        extra_preserved: Mapping of name → resolved value to inject
            into the hidden inputs unconditionally. Useful when the
            value comes from a cookie / view default rather than from
            the current URL — e.g. ``view=table`` must travel with
            every filter submit even when the URL doesn't yet carry it.
        form_url: Action URL for the filter form. Defaults to the
            current path.
        htmx_target: CSS selector for the HTMX swap target.

    Returns:
        Dict that should be merged into the view's context.
    """
    user = request.user
    # ``effective_params`` lets the caller fold in values resolved
    # from cookies (e.g. show_archived) so the sidebar's selected /
    # toggle state reflects them. Falls back to ``request.GET`` when
    # the caller doesn't provide a merged view.
    params = effective_params if effective_params is not None else request.GET
    active = resolve_active_workspace(request)

    if available_projects is None:
        available_projects = (
            list(
                Project.objects.filter(workspace=active)
                .select_related("workspace")
                .order_by("workspace__name", "name")
                .distinct(),
            )
            if active
            else []
        )
    if available_labels is None:
        available_labels = list(Label.objects.filter(workspace=active).order_by("name").distinct()) if active else []
    if available_assignees is None:
        User = get_user_model()
        # The strip shows two groups: current members of any shared
        # workspace AND any user who shows up as ``assignee`` on a
        # task in a shared workspace. The second group catches "former
        # members" — users who were removed from the workspace but
        # still carry orphan task assignments. Without them, those
        # tasks were impossible to filter to from the UI (the assignee
        # avatar rendered fine but the strip didn't know about them).
        #
        # Two queries on purpose: we need to remember which group each
        # user came from so the template can paint the ``(former)``
        # marker on the second group. ``is_former`` is set on the
        # Python objects below; the request user is always pinned as
        # the leftmost "you" chip in the strip and excluded from both
        # queries here.
        active_member_ids = set(
            User.objects.filter(
                workspace_memberships__workspace=active,
            )
            .exclude(pk=user.pk)
            .values_list("pk", flat=True)
            .distinct()
            if active
            else []
        )
        former_assignee_ids = set(
            User.objects.filter(
                assigned_tasks__project__workspace=active,
            )
            .exclude(pk=user.pk)
            .exclude(pk__in=active_member_ids)
            .values_list("pk", flat=True)
            .distinct()
            if active
            else []
        )
        all_ids = active_member_ids | former_assignee_ids
        available_assignees = list(
            User.objects.filter(pk__in=all_ids).order_by("first_name", "last_name", "username"),
        )
        for u in available_assignees:
            u.is_former = u.pk in former_assignee_ids
        # Former members go to the END of the strip — they're still
        # filterable but visually de-prioritised so the active roster
        # reads first. Within each group keep alphabetical order.
        available_assignees.sort(key=lambda u: (u.is_former, (u.first_name or u.username or "").lower()))

    selected_statuses = set(params.getlist("status"))
    selected_priorities = {int(p) for p in params.getlist("priority") if p.isdigit()}
    selected_sizes = {int(s) for s in params.getlist("size") if s.isdigit()}
    selected_projects = {int(p) for p in params.getlist("project") if p.isdigit()}
    selected_labels = {int(i) for i in params.getlist("label") if i.isdigit()}
    selected_assignees = set(params.getlist("assignee"))
    show_archived = "1" in params.getlist("show_archived")

    # Excluded sets: right-click on a chip toggles a value into one of
    # these. Renders with a red strikethrough state; backend
    # ``apply_task_filters`` translates them into ``.exclude(...)``.
    excluded_statuses = set(params.getlist("xstatus"))
    excluded_priorities = {int(p) for p in params.getlist("xpriority") if p.isdigit()}
    excluded_sizes = {int(s) for s in params.getlist("xsize") if s.isdigit()}
    excluded_projects = {int(p) for p in params.getlist("xproject") if p.isdigit()}
    excluded_labels = {int(i) for i in params.getlist("xlabel") if i.isdigit()}
    excluded_assignees = set(params.getlist("xassignee"))

    q = params.get("q", "")

    active_filter_count = (
        (1 if q else 0)
        + len(selected_assignees)
        + len(selected_statuses)
        + len(selected_priorities)
        + len(selected_sizes)
        + len(selected_projects)
        + len(selected_labels)
        + len(excluded_assignees)
        + len(excluded_statuses)
        + len(excluded_priorities)
        + len(excluded_sizes)
        + len(excluded_projects)
        + len(excluded_labels)
        + (1 if show_archived else 0)
    )

    preserved_pairs = []
    consumed_keys = set()
    for key, value in (extra_preserved or {}).items():
        if value in (None, ""):
            continue
        preserved_pairs.append((key, value))
        consumed_keys.add(key)
    for key in preserved_params or ():
        if key in consumed_keys:
            continue
        for value in params.getlist(key):
            preserved_pairs.append((key, value))

    return {
        "filter_form_url": form_url or request.path,
        "filter_htmx_target": htmx_target or "#task-list-wrapper",
        "filter_preserved_pairs": preserved_pairs,
        "filter_hide_assignee": hide_assignee,
        "filter_hide_project": hide_project,
        "filter_hide_status": hide_status,
        "selected_statuses": selected_statuses,
        "selected_priorities": selected_priorities,
        "selected_sizes": selected_sizes,
        "selected_projects": selected_projects,
        "selected_labels": selected_labels,
        "selected_assignees": selected_assignees,
        "excluded_statuses": excluded_statuses,
        "excluded_priorities": excluded_priorities,
        "excluded_sizes": excluded_sizes,
        "excluded_projects": excluded_projects,
        "excluded_labels": excluded_labels,
        "excluded_assignees": excluded_assignees,
        "show_archived": show_archived,
        "q": q,
        "available_projects": available_projects,
        "available_labels": available_labels,
        "available_assignees": available_assignees,
        "active_filter_count": active_filter_count,
        "status_labels": Task.STATUS_LABELS,
        "priority_labels": dict(Task.PRIORITY_CHOICES),
        "size_values": Task.SIZE_VALUES,
        "today": timezone.localdate(),
    }
