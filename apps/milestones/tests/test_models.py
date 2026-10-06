import datetime

from django.core.exceptions import ValidationError
from django.utils import timezone

import pytest

from apps.milestones.models import Milestone
from apps.milestones.tests.factories import MilestoneFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope():
    """A milestone covering one project of its own workspace."""
    project = ProjectFactory()
    milestone = MilestoneFactory(workspace=project.workspace, projects=[project])
    return project, milestone


def test_counts_ignore_cancelled_and_shelved_work(scope):
    """Cancelled is not work; archived-done counts, archived-open drops out."""
    project, milestone = scope
    TaskFactory(project=project, milestone=milestone, status=Task.STATUS_DONE)
    TaskFactory(project=project, milestone=milestone, status=Task.STATUS_TODO)
    TaskFactory(project=project, milestone=milestone, status=Task.STATUS_CANCELLED)
    TaskFactory(
        project=project,
        milestone=milestone,
        status=Task.STATUS_DONE,
        archived_at=timezone.now(),
    )
    TaskFactory(
        project=project,
        milestone=milestone,
        status=Task.STATUS_TODO,
        archived_at=timezone.now(),
    )

    assert milestone.counts() == (2, 3)


def test_rollup_annotation_agrees_with_the_per_row_counts(scope):
    """The aggregate and the single-row walk must never disagree.

    They are two readings of one rule, and the bug this guards against
    is a container showing 1/3 in a list and 0/2 on its own page.
    """
    project, milestone = scope
    TaskFactory(project=project, milestone=milestone, status=Task.STATUS_DONE)
    TaskFactory(project=project, milestone=milestone, status=Task.STATUS_TODO)
    TaskFactory(project=project, milestone=milestone, status=Task.STATUS_CANCELLED)
    TaskFactory(
        project=project,
        milestone=milestone,
        status=Task.STATUS_DONE,
        archived_at=timezone.now(),
    )

    row = Milestone.objects.with_rollup().get(pk=milestone.pk)
    assert (row.counted_done, row.counted_total) == milestone.counts()


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (7, Milestone.STATE_OPEN),
        (0, Milestone.STATE_TODAY),
        (-1, Milestone.STATE_OVERDUE),
    ],
)
def test_state_follows_the_date_while_work_is_open(scope, days, expected):
    """With work still open the date alone decides the state."""
    project, milestone = scope
    milestone.target_date = timezone.localdate() + datetime.timedelta(days=days)
    TaskFactory(project=project, milestone=milestone, status=Task.STATUS_TODO)

    assert milestone.state() == expected


def test_state_is_complete_when_every_counted_task_is_done(scope):
    """All work done reads as complete — but not as closed."""
    project, milestone = scope
    TaskFactory(project=project, milestone=milestone, status=Task.STATUS_DONE)
    TaskFactory(project=project, milestone=milestone, status=Task.STATUS_CANCELLED)

    assert milestone.state() == Milestone.STATE_COMPLETE
    assert milestone.is_closed is False


def test_state_is_closed_once_a_person_closes_it(scope):
    """Closing is the one stored fact and it outranks the derived ones."""
    _, milestone = scope
    milestone.closed_at = timezone.now()

    assert milestone.state() == Milestone.STATE_CLOSED


def test_empty_milestone_is_never_complete(scope):
    """Nothing done out of nothing is not an achievement."""
    _, milestone = scope

    assert milestone.state() == Milestone.STATE_OPEN


def test_task_cannot_join_a_milestone_that_misses_its_project(scope):
    """Scope is the whole reason a milestone can be shared."""
    project, milestone = scope
    other = ProjectFactory(workspace=project.workspace)
    task = TaskFactory(project=other, milestone=milestone)

    with pytest.raises(ValidationError) as excinfo:
        task.full_clean()

    assert "milestone" in excinfo.value.error_dict


def test_task_joins_a_milestone_covering_its_project(scope):
    """The in-scope case passes validation."""
    project, milestone = scope
    task = TaskFactory(project=project, milestone=milestone)

    task.full_clean()


def test_an_epic_stores_no_milestone(scope):
    """An epic derives its milestones from its tasks, like its dates."""
    project, milestone = scope
    epic = TaskFactory(project=project, kind=Task.KIND_EPIC, milestone=milestone)

    with pytest.raises(ValidationError) as excinfo:
        epic.full_clean()

    assert "milestone" in excinfo.value.error_dict


def test_scope_cannot_reach_another_workspace(scope):
    """A milestone's projects all live in its own workspace."""
    _, milestone = scope
    foreign = ProjectFactory()
    milestone.projects.add(foreign)

    with pytest.raises(ValidationError) as excinfo:
        milestone.full_clean()

    assert "projects" in excinfo.value.error_dict
