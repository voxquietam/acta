"""Backend filter wiring in :func:`apps.web.filters.apply_task_filters`."""

from django.db import connection
from django.http import QueryDict
from django.test.utils import CaptureQueriesContext

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web.filters import apply_task_filters, project_facet_counts, visible_project_facets
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestProjectFacetCounts:
    """``project_facet_counts`` — tasks-per-project under the current
    filters, with the project axis itself excluded (faceted semantics)."""

    def _base(self, ws):
        return Task.objects.filter(project__workspace=ws)

    def test_counts_per_project(self):
        ws = WorkspaceFactory()
        p1 = ProjectFactory(workspace=ws)
        p2 = ProjectFactory(workspace=ws)
        TaskFactory(project=p1)
        TaskFactory(project=p1)
        TaskFactory(project=p2)
        counts = project_facet_counts(self._base(ws), QueryDict(""), request_user=ws.owner)
        assert counts == {p1.id: 2, p2.id: 1}

    def test_other_axis_filter_applies(self):
        ws = WorkspaceFactory()
        p1 = ProjectFactory(workspace=ws)
        p2 = ProjectFactory(workspace=ws)
        me = ws.owner
        other = WorkspaceFactory().owner
        TaskFactory(project=p1, assignee=me)
        TaskFactory(project=p1, assignee=other)
        TaskFactory(project=p2, assignee=me)
        counts = project_facet_counts(self._base(ws), QueryDict("assignee=me"), request_user=me)
        assert counts == {p1.id: 1, p2.id: 1}

    def test_project_axis_is_ignored(self):
        """Selecting one project must NOT collapse the others to 0 — the
        whole point of a facet is to keep the alternatives countable."""
        ws = WorkspaceFactory()
        p1 = ProjectFactory(workspace=ws)
        p2 = ProjectFactory(workspace=ws)
        TaskFactory(project=p1)
        TaskFactory(project=p2)
        counts = project_facet_counts(
            self._base(ws),
            QueryDict(f"project={p1.id}"),
            request_user=ws.owner,
        )
        assert counts == {p1.id: 1, p2.id: 1}

    def test_single_query(self):
        ws = WorkspaceFactory()
        p1 = ProjectFactory(workspace=ws)
        p2 = ProjectFactory(workspace=ws)
        TaskFactory(project=p1)
        TaskFactory(project=p2)
        base = self._base(ws)
        with CaptureQueriesContext(connection) as ctx:
            dict(project_facet_counts(base, QueryDict(""), request_user=ws.owner))
        assert len(ctx.captured_queries) == 1

    def test_visible_facets_hides_zero_keeps_selected(self):
        ws = WorkspaceFactory()
        p1 = ProjectFactory(workspace=ws)
        p2 = ProjectFactory(workspace=ws)
        p3 = ProjectFactory(workspace=ws)
        counts = {p1.id: 3}  # p2, p3 absent → 0
        rows = visible_project_facets(
            [p1, p2, p3],
            counts,
            selected_ids={p2.id},
            excluded_ids=set(),
        )
        # p1 kept (>0), p2 kept (selected despite 0), p3 dropped (0, not selected).
        assert [p.id for p in rows] == [p1.id, p2.id]
        assert p1.facet_count == 3
        assert p2.facet_count == 0


@pytest.mark.django_db
class TestSizeFilter:
    """``size`` / ``xsize`` narrow the queryset by the Fibonacci estimate."""

    def _qs(self, project, params):
        return apply_task_filters(
            Task.objects.filter(project=project),
            QueryDict(params),
            request_user=project.workspace.owner,
        )

    def test_include_single_size(self):
        project = ProjectFactory()
        t3 = TaskFactory(project=project, size=3)
        TaskFactory(project=project, size=5)
        TaskFactory(project=project, size=None)
        assert set(self._qs(project, "size=3").values_list("id", flat=True)) == {t3.id}

    def test_include_multiple_sizes(self):
        project = ProjectFactory()
        t3 = TaskFactory(project=project, size=3)
        t5 = TaskFactory(project=project, size=5)
        TaskFactory(project=project, size=8)
        assert set(self._qs(project, "size=3&size=5").values_list("id", flat=True)) == {t3.id, t5.id}

    def test_exclude_size(self):
        project = ProjectFactory()
        t3 = TaskFactory(project=project, size=3)
        t5 = TaskFactory(project=project, size=5)
        ids = set(self._qs(project, "xsize=3").values_list("id", flat=True))
        assert t3.id not in ids
        assert t5.id in ids


@pytest.mark.django_db
class TestDueFilter:
    """``due=overdue`` / ``soon`` / ``none`` — the dashboard deadline shortcuts."""

    def _ids(self, project, params):
        return set(
            apply_task_filters(
                Task.objects.filter(project=project),
                QueryDict(params),
                request_user=project.workspace.owner,
            ).values_list("id", flat=True)
        )

    def test_overdue_is_past_due_and_open(self):
        from datetime import timedelta

        from django.utils import timezone

        project = ProjectFactory()
        today = timezone.localdate()
        overdue = TaskFactory(project=project, due_date=today - timedelta(days=2), status=Task.STATUS_TODO)
        done_overdue = TaskFactory(project=project, due_date=today - timedelta(days=2), status=Task.STATUS_DONE)
        future = TaskFactory(project=project, due_date=today + timedelta(days=5), status=Task.STATUS_TODO)
        no_due = TaskFactory(project=project, due_date=None, status=Task.STATUS_TODO)
        ids = self._ids(project, "due=overdue")
        assert overdue.id in ids
        assert done_overdue.id not in ids  # finished tasks aren't "overdue"
        assert future.id not in ids
        assert no_due.id not in ids

    def test_soon_is_next_three_days(self):
        from datetime import timedelta

        from django.utils import timezone

        project = ProjectFactory()
        today = timezone.localdate()
        soon = TaskFactory(project=project, due_date=today + timedelta(days=2), status=Task.STATUS_TODO)
        today_due = TaskFactory(project=project, due_date=today, status=Task.STATUS_TODO)
        far = TaskFactory(project=project, due_date=today + timedelta(days=10), status=Task.STATUS_TODO)
        ids = self._ids(project, "due=soon")
        assert soon.id in ids
        assert today_due.id not in ids  # today is "overdue", not "soon"
        assert far.id not in ids

    def test_none_is_missing_due_date(self):
        from django.utils import timezone

        project = ProjectFactory()
        no_due = TaskFactory(project=project, due_date=None)
        has_due = TaskFactory(project=project, due_date=timezone.localdate())
        ids = self._ids(project, "due=none")
        assert no_due.id in ids
        assert has_due.id not in ids


@pytest.mark.django_db
class TestHygieneSentinels:
    """``label=none`` and ``desc=none`` power the dashboard hygiene cards."""

    def _ids(self, project, params):
        return set(
            apply_task_filters(
                Task.objects.filter(project=project),
                QueryDict(params),
                request_user=project.workspace.owner,
            ).values_list("id", flat=True)
        )

    def test_label_none(self):
        from apps.labels.tests.factories import LabelFactory

        project = ProjectFactory()
        bare = TaskFactory(project=project)
        tagged = TaskFactory(project=project)
        tagged.labels.add(LabelFactory(workspace=project.workspace))
        ids = self._ids(project, "label=none")
        assert bare.id in ids
        assert tagged.id not in ids

    def test_desc_none(self):
        project = ProjectFactory()
        empty = TaskFactory(project=project, description="")
        filled = TaskFactory(project=project, description="has content")
        ids = self._ids(project, "desc=none")
        assert empty.id in ids
        assert filled.id not in ids
