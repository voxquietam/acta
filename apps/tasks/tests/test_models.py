"""Task model validation invariants (``clean`` method)."""

from django.core.exceptions import ValidationError

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory


@pytest.mark.django_db
class TestTaskClean:
    """``Task.clean`` enforces cross-field invariants beyond field validators."""

    def test_subtask_depth_limit_one(self):
        """A grandchild (depth 2) is rejected."""
        parent = TaskFactory()
        child = TaskFactory(project=parent.project, parent=parent)
        grandchild = Task(
            project=parent.project,
            parent=child,
            title="grandchild",
            reporter=parent.reporter,
        )
        with pytest.raises(ValidationError) as exc:
            grandchild.clean()
        assert "parent" in exc.value.message_dict

    def test_subtask_must_share_project(self):
        """A subtask whose ``project`` differs from its parent is rejected."""
        parent = TaskFactory()
        other_project = ProjectFactory(workspace=parent.project.workspace)
        bad = Task(
            project=other_project,
            parent=parent,
            title="bad",
            reporter=parent.reporter,
        )
        with pytest.raises(ValidationError) as exc:
            bad.clean()
        assert "parent" in exc.value.message_dict

    def test_size_must_be_fibonacci(self):
        """Sizes outside the allowed Fibonacci set are rejected."""
        task = TaskFactory()
        task.size = 4
        with pytest.raises(ValidationError) as exc:
            task.clean()
        assert "size" in exc.value.message_dict

    def test_size_none_is_allowed(self):
        """``size`` may be ``None`` (no estimate)."""
        task = TaskFactory()
        task.size = None
        task.clean()

    def test_status_must_be_known(self):
        """Unknown status strings are rejected."""
        task = TaskFactory()
        task.status = "wat"
        with pytest.raises(ValidationError) as exc:
            task.clean()
        assert "status" in exc.value.message_dict

    @pytest.mark.parametrize(
        "status",
        [
            Task.STATUS_PLANNED,
            Task.STATUS_TODO,
            Task.STATUS_IN_PROGRESS,
            Task.STATUS_IN_REVIEW,
            Task.STATUS_DONE,
        ],
    )
    def test_known_statuses_pass(self, status):
        """All five enum values are accepted."""
        task = TaskFactory()
        task.status = status
        task.clean()
