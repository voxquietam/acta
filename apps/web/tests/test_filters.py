"""Backend filter wiring in :func:`apps.web.filters.apply_task_filters`."""

from django.http import QueryDict

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web.filters import apply_task_filters


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
