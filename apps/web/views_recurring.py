"""Views for recurring-task rules — the ``/recurring/`` page and CRUD.

Phase 2 of recurring tasks (ADR 0028): a workspace-scoped list of rules
plus a create/edit modal and the pause / run-now / delete actions. The
headless engine (model + materializer) lives in ``apps.recurring``; this
module is the human surface over it. Rules are scoped to projects the
user can access in the active workspace, mirroring task creation.
"""

import datetime

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.labels.services import grouped_labels, trim_exclusive_conflicts
from apps.recurring import services
from apps.recurring.models import RecurringTask
from apps.tasks.models import Task
from apps.web.nav import resolve_active_workspace
from apps.web.views import _is_htmx_partial, _project_labels_qs, _project_members_qs, _user_accessible_projects

_SIZE_VALUES = {1, 2, 3, 5, 8, 13}


def _accessible_rules(request):
    """Return the recurring rules the user may see in the active workspace.

    Scoped to the active workspace and to projects the user can access,
    so the list never leaks rules from projects they're not a member of.
    """
    active = resolve_active_workspace(request)
    if active is None:
        return RecurringTask.objects.none()
    project_ids = _user_accessible_projects(request.user, active).values_list("id", flat=True)
    return (
        RecurringTask.objects.filter(workspace=active, project_id__in=project_ids)
        .select_related("project", "assignee")
        .prefetch_related("labels")
        .order_by("-is_active", "next_occurrence_date", "-created_at")
    )


def _changed_response():
    """``204`` that tells the list to re-fetch via ``acta:recurring-changed``.

    Every mutation (create / edit / toggle / run / delete) returns this; the
    ``#recurring-list`` container listens for the event and refetches with
    the active filter values, so one path keeps the list in sync.
    """
    resp = HttpResponse(status=204)
    resp["HX-Trigger"] = "acta:recurring-changed"
    return resp


def _list_context(request):
    """Filtered rules + the filter options and the selected filter values."""
    active = resolve_active_workspace(request)
    base = _accessible_rules(request)
    total_rules = base.count()
    f_project = (request.GET.get("project") or "").strip()
    f_assignee = (request.GET.get("assignee") or "").strip()
    f_state = (request.GET.get("state") or "").strip()
    f_freq = (request.GET.get("freq") or "").strip()
    qs = base
    if f_project:
        qs = qs.filter(project__slug_prefix=f_project)
    if f_assignee == "unassigned":
        qs = qs.filter(assignee__isnull=True)
    elif f_assignee.isdigit():
        qs = qs.filter(assignee_id=int(f_assignee))
    if f_state == "active":
        qs = qs.filter(is_active=True)
    elif f_state == "paused":
        qs = qs.filter(is_active=False)
    if f_freq in RecurringTask.Freq.values:
        qs = qs.filter(freq=f_freq)
    members = list(active.members.order_by("first_name", "last_name", "username")) if active else []
    return {
        "rules": list(qs),
        "total_rules": total_rules,
        "filter_projects": list(_user_accessible_projects(request.user, active)) if active else [],
        "filter_members": members,
        "freq_choices": RecurringTask.Freq.choices,
        "f_project": f_project,
        "f_assignee": f_assignee,
        "f_state": f_state,
        "f_freq": f_freq,
    }


@login_required
def recurring_list(request):
    """List recurring-task rules for the active workspace.

    Full page on a cold load; the inner fragment on an HTMX request so the
    list refreshes live on a filter change or after a create / edit / delete
    / toggle (``acta:recurring-changed`` drives the re-fetch, carrying the
    active filter values via ``hx-include``).
    """
    ctx = _list_context(request)
    if _is_htmx_partial(request):
        return render(request, "web/recurring/_recurring_inner.html", ctx)
    return render(request, "web/recurring/recurring.html", ctx)


def _editor_context(request, *, rule=None, selected_project=None):
    """Build the create/edit modal context (blueprint pickers + rule state)."""
    projects = list(_user_accessible_projects(request.user, resolve_active_workspace(request)))
    if selected_project is None:
        if rule is not None:
            selected_project = rule.project
        elif projects:
            requested = request.GET.get("project") or ""
            selected_project = next((p for p in projects if p.slug_prefix == requested), projects[0])
    members = list(_project_members_qs(selected_project)) if selected_project else []
    label_groups = grouped_labels(selected_project.workspace) if selected_project else []
    selected_label_ids = list(rule.labels.values_list("id", flat=True)) if rule is not None else []
    return {
        "rule": rule,
        "projects": projects,
        "selected_project": selected_project,
        "members": members,
        "label_groups": label_groups,
        "selected_label_ids": selected_label_ids,
        "status_labels": Task.STATUS_LABELS,
        "priority_labels": dict(Task.PRIORITY_CHOICES),
        "size_values": sorted(_SIZE_VALUES),
        "freq_choices": RecurringTask.Freq.choices,
        "end_mode_choices": RecurringTask.EndMode.choices,
        "weekday_choices": [
            (0, _("Mon")),
            (1, _("Tue")),
            (2, _("Wed")),
            (3, _("Thu")),
            (4, _("Fri")),
            (5, _("Sat")),
            (6, _("Sun")),
        ],
        "today": timezone.localdate().isoformat(),
    }


@login_required
def recurring_editor(request, pk=None):
    """Render the create/edit modal (GET) or persist the rule (POST).

    GET: the ``_recurring_editor.html`` modal, blank for ``pk is None`` or
    pre-filled from the rule otherwise. The project ``<select>`` re-fetches
    the modal on change so the assignee / label pickers re-scope.

    POST: validate + create or update, then ``204`` with
    ``HX-Trigger: acta:recurring-changed`` so the list refreshes and the
    modal closes.
    """
    rule = None
    if pk is not None:
        rule = get_object_or_404(_accessible_rules(request), pk=pk)
    if request.method == "POST":
        return _recurring_save(request, rule)
    return render(request, "web/recurring/_recurring_editor.html", _editor_context(request, rule=rule))


def _recurring_save(request, rule):
    """Validate the editor form and create or update a rule.

    Returns ``400`` (modal stays open) on any invalid field, else ``204``
    with the ``acta:recurring-changed`` trigger.
    """
    active = resolve_active_workspace(request)
    project_slug = (request.POST.get("project") or "").strip()
    project = next(
        (p for p in _user_accessible_projects(request.user, active) if p.slug_prefix == project_slug),
        None,
    )
    if project is None:
        return HttpResponseBadRequest("project required")
    title = (request.POST.get("title") or "").strip()
    if not title:
        return HttpResponseBadRequest("title required")
    if len(title) > 200:
        return HttpResponseBadRequest("title too long")

    parsed = _parse_schedule(request)
    if isinstance(parsed, HttpResponseBadRequest):
        return parsed

    assignee = _resolve_member(request.POST.get("assignee"), project)
    if assignee is HttpResponseBadRequest:
        return HttpResponseBadRequest("assignee not in workspace")

    priority = _safe_int(request.POST.get("priority"), default=Task.NO_PRIORITY)
    if priority not in {p[0] for p in Task.PRIORITY_CHOICES}:
        return HttpResponseBadRequest("invalid priority")
    size = request.POST.get("size") or ""
    size_val = None
    if size:
        size_val = _safe_int(size, default=None)
        if size_val not in _SIZE_VALUES:
            return HttpResponseBadRequest("invalid size")

    label_ids = _resolve_labels(request, project)
    if label_ids is HttpResponseBadRequest:
        return HttpResponseBadRequest("labels not in workspace")

    is_create = rule is None
    with transaction.atomic():
        if is_create:
            rule = RecurringTask(created_by=request.user)
        rule.workspace = project.workspace
        rule.project = project
        rule.title = title
        rule.description = request.POST.get("description") or ""
        rule.assignee = assignee
        rule.priority = priority
        rule.size = size_val
        rule.freq = parsed["freq"]
        rule.interval = parsed["interval"]
        rule.weekdays = parsed["weekdays"]
        rule.day_of_month = parsed["day_of_month"]
        rule.start_date = parsed["start_date"]
        rule.lead_time_days = parsed["lead_time_days"]
        rule.end_mode = parsed["end_mode"]
        rule.end_date = parsed["end_date"]
        rule.max_occurrences = parsed["max_occurrences"]
        rule.is_active = True
        # Recompute the cursor from the later of start_date / today so an
        # edit (or a past start_date) never backfills a pile of old
        # occurrences on the next materializer run.
        anchor = max(rule.start_date, timezone.localdate())
        rule.next_occurrence_date = services.occurrence_on_or_after(rule, anchor)
        rule.save()
        rule.labels.set(label_ids)
        log_event(
            workspace=rule.workspace,
            project=rule.project,
            actor=request.user,
            event_type="recurring.created" if is_create else "recurring.updated",
            target_type=ActivityLog.TARGET_TASK,
            target_id=rule.id,
            payload={"title": rule.title, "cadence": rule.human_cadence()},
        )
    return _changed_response()


@login_required
@require_POST
def recurring_toggle(request, pk):
    """Pause or resume a rule (``is_active``).

    Resuming a finished rule (no cursor) re-seeds it from today so it picks
    up again instead of staying inert.
    """
    rule = get_object_or_404(_accessible_rules(request), pk=pk)
    with transaction.atomic():
        rule.is_active = not rule.is_active
        if rule.is_active and rule.next_occurrence_date is None:
            anchor = max(rule.start_date, timezone.localdate())
            rule.next_occurrence_date = services.occurrence_on_or_after(rule, anchor)
        rule.save(update_fields=["is_active", "next_occurrence_date", "updated_at"])
    return _changed_response()


@login_required
@require_POST
def recurring_run_now(request, pk):
    """Spawn the rule's next occurrence right now (the "create now" action)."""
    rule = get_object_or_404(_accessible_rules(request), pk=pk)
    services.run_once(rule)
    return _changed_response()


@login_required
@require_POST
def recurring_delete(request, pk):
    """Delete a rule. Generated tasks survive (``Task.recurrence`` is SET_NULL)."""
    rule = get_object_or_404(_accessible_rules(request), pk=pk)
    workspace = rule.workspace
    project = rule.project
    title = rule.title
    rule_id = rule.id
    with transaction.atomic():
        rule.delete()
        log_event(
            workspace=workspace,
            project=project,
            actor=request.user,
            event_type="recurring.deleted",
            target_type=ActivityLog.TARGET_TASK,
            target_id=rule_id,
            payload={"title": title},
        )
    return _changed_response()


# ---------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------


def _safe_int(raw, *, default):
    """Parse ``raw`` to int, returning ``default`` on failure."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _resolve_member(raw, project):
    """Resolve an assignee id to a workspace member, or ``None`` when blank.

    Returns the ``HttpResponseBadRequest`` *class* sentinel when the id is
    set but not a member — the caller turns that into a 400.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    member_id = _safe_int(raw, default=None)
    if member_id is None:
        return HttpResponseBadRequest
    member = next((m for m in _project_members_qs(project) if m.pk == member_id), None)
    return member if member is not None else HttpResponseBadRequest


def _resolve_labels(request, project):
    """Validate the posted label ids against the project's workspace.

    Returns the trimmed list of ids, or the ``HttpResponseBadRequest`` class
    sentinel when any id is foreign to the workspace.
    """
    ids = []
    for raw in request.POST.getlist("labels"):
        val = _safe_int(raw, default=None)
        if val is None:
            return HttpResponseBadRequest
        ids.append(val)
    if not ids:
        return []
    valid = set(_project_labels_qs(project).filter(id__in=ids).values_list("id", flat=True))
    if valid != set(ids):
        return HttpResponseBadRequest
    return trim_exclusive_conflicts(ids)


def _parse_schedule(request):
    """Parse + validate the schedule fields into a dict, or return a 400."""
    freq = request.POST.get("freq") or RecurringTask.Freq.WEEKLY
    if freq not in RecurringTask.Freq.values:
        return HttpResponseBadRequest("invalid freq")
    interval = _safe_int(request.POST.get("interval"), default=1)
    if interval < 1 or interval > 365:
        return HttpResponseBadRequest("invalid interval")

    weekdays = []
    for raw in request.POST.getlist("weekdays"):
        val = _safe_int(raw, default=None)
        if val is None or val < 0 or val > 6:
            return HttpResponseBadRequest("invalid weekday")
        weekdays.append(val)
    weekdays = sorted(set(weekdays))

    day_of_month = None
    if request.POST.get("day_of_month"):
        day_of_month = _safe_int(request.POST.get("day_of_month"), default=None)
        if day_of_month is None or day_of_month < 1 or day_of_month > 31:
            return HttpResponseBadRequest("invalid day_of_month")

    start_date = _parse_date(request.POST.get("start_date"))
    if start_date is None:
        return HttpResponseBadRequest("start_date required")

    lead_time_days = _safe_int(request.POST.get("lead_time_days"), default=0)
    if lead_time_days < 0 or lead_time_days > 365:
        return HttpResponseBadRequest("invalid lead time")

    end_mode = request.POST.get("end_mode") or RecurringTask.EndMode.NEVER
    if end_mode not in RecurringTask.EndMode.values:
        return HttpResponseBadRequest("invalid end_mode")
    end_date = None
    max_occurrences = None
    if end_mode == RecurringTask.EndMode.ON_DATE:
        end_date = _parse_date(request.POST.get("end_date"))
        if end_date is None or end_date < start_date:
            return HttpResponseBadRequest("invalid end_date")
    elif end_mode == RecurringTask.EndMode.AFTER_COUNT:
        max_occurrences = _safe_int(request.POST.get("max_occurrences"), default=None)
        if max_occurrences is None or max_occurrences < 1:
            return HttpResponseBadRequest("invalid max_occurrences")

    return {
        "freq": freq,
        "interval": interval,
        "weekdays": weekdays,
        "day_of_month": day_of_month,
        "start_date": start_date,
        "lead_time_days": lead_time_days,
        "end_mode": end_mode,
        "end_date": end_date,
        "max_occurrences": max_occurrences,
    }


def _parse_date(raw):
    """Parse an ISO ``YYYY-MM-DD`` string to a date, or ``None``."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return None
