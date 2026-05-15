"""Regression tests for query-count guarantees on the task endpoints.

A failing assertion here means a recent change reintroduced N+1 in a
hot path. Fix the offending viewset / serializer / bulk helper before
proceeding. See docs/CLAUDE.md "Database query discipline" and
``feedback_no_n_plus_one`` memory.
"""

import pytest

from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.bulk import _run_bulk_update
from apps.tasks.models import Task
from apps.tasks.serializers import TaskSerializer
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


def _list_queryset(workspace, user):
    """Mirror :meth:`TaskViewSet.get_queryset` so tests stay in sync."""
    return (
        Task.objects.filter(project__workspace=workspace, project__workspace__memberships__user=user)
        .select_related("project__workspace", "assignee", "reporter", "parent")
        .prefetch_related("labels")
        .distinct()
    )


@pytest.mark.django_db
class TestTaskListQueryCount:
    """List endpoint must be O(1) in row count."""

    def test_list_constant_queries_regardless_of_count(self, django_assert_num_queries):
        ws = WorkspaceFactory()
        user = ws.owner
        project = ProjectFactory(workspace=ws)
        labels = [LabelFactory(workspace=ws) for _ in range(3)]
        for _ in range(10):
            t = TaskFactory(project=project, reporter=user)
            t.labels.set(labels[:2])

        qs = _list_queryset(ws, user)
        # 1 main SELECT + 1 prefetch SELECT for labels = 2 queries total.
        with django_assert_num_queries(2):
            data = TaskSerializer(qs, many=True).data
            str(data)


@pytest.mark.django_db
class TestBulkUpdateQueryCount:
    """Bulk update query count must not scale with batch size."""

    def _seed(self, n):
        ws = WorkspaceFactory()
        user = ws.owner
        project = ProjectFactory(workspace=ws)
        ids = [TaskFactory(project=project, reporter=user).id for _ in range(n)]
        return user, ids

    def test_constant_queries_in_batch_size(self, django_assert_max_num_queries):
        user_5, ids_5 = self._seed(5)
        user_50, ids_50 = self._seed(50)

        # Same operation on 5 and 50 tasks must complete in the same
        # number of queries (within a small constant margin).
        with django_assert_max_num_queries(12):
            _run_bulk_update(user=user_5, ids=ids_5, updates={"priority": Task.HIGH})
        with django_assert_max_num_queries(12):
            _run_bulk_update(user=user_50, ids=ids_50, updates={"priority": Task.URGENT})
