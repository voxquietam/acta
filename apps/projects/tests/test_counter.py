"""Project task-number counter: atomic, monotonic, never reused."""

from django.db import transaction

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory


@pytest.mark.django_db(transaction=True)
class TestAllocateTaskNumber:
    """Single-number allocation under ``transaction.atomic``."""

    def test_first_allocation_returns_one(self):
        project = ProjectFactory()
        with transaction.atomic():
            n = project.allocate_task_number()
        assert n == 1

    def test_sequential_allocations_increment(self):
        project = ProjectFactory()
        numbers = []
        for _ in range(3):
            with transaction.atomic():
                numbers.append(project.allocate_task_number())
        assert numbers == [1, 2, 3]
        project.refresh_from_db()
        assert project.next_task_number == 4

    def test_task_save_uses_counter(self):
        project = ProjectFactory()
        t1 = TaskFactory(project=project)
        t2 = TaskFactory(project=project)
        assert (t1.number, t2.number) == (1, 2)

    def test_deleted_task_number_not_reused(self):
        project = ProjectFactory()
        t1 = TaskFactory(project=project)
        assert t1.number == 1
        t1.delete()
        t2 = TaskFactory(project=project)
        assert t2.number == 2

    def test_numbers_are_per_project(self):
        a = ProjectFactory()
        b = ProjectFactory(workspace=a.workspace)
        TaskFactory(project=a)  # AAA-1
        TaskFactory(project=a)  # AAA-2
        first_b = TaskFactory(project=b)
        assert first_b.number == 1


@pytest.mark.django_db(transaction=True)
class TestAllocateTaskNumbersBulk:
    """Bulk allocation for project moves."""

    def test_returns_consecutive_range(self):
        project = ProjectFactory()
        with transaction.atomic():
            nums = project.allocate_task_numbers(5)
        assert nums == [1, 2, 3, 4, 5]
        project.refresh_from_db()
        assert project.next_task_number == 6

    def test_continues_after_existing_tasks(self):
        project = ProjectFactory()
        TaskFactory(project=project)
        TaskFactory(project=project)
        with transaction.atomic():
            nums = project.allocate_task_numbers(3)
        assert nums == [3, 4, 5]

    def test_rejects_non_positive_count(self):
        project = ProjectFactory()
        with pytest.raises(ValueError):
            with transaction.atomic():
                project.allocate_task_numbers(0)


@pytest.mark.django_db
class TestSlugComposition:
    """``Task.slug`` is ``{project.slug_prefix}-{number}``."""

    def test_slug_format(self):
        project = ProjectFactory(slug_prefix="HRW")
        task = TaskFactory(project=project)
        assert task.slug == f"HRW-{task.number}"
