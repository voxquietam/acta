"""An epic is a task that collects other tasks.

Everything here is about the two things that make it one: the rules a
task has to satisfy to be collected, and the state an epic reads off the
tasks it collects. See docs/decisions/0036-epics.md.
"""

import datetime

from django.core.exceptions import ValidationError

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def workspace(db):
    """A workspace with epics turned on (the default)."""
    return WorkspaceFactory()


@pytest.fixture
def project(workspace):
    """A project in that workspace."""
    return ProjectFactory(workspace=workspace)


@pytest.fixture
def epic(project):
    """An empty epic."""
    return TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED)


def member(epic, project, status=Task.STATUS_TODO, **kwargs):
    """Create a task collected by ``epic``."""
    return TaskFactory(project=project, epic=epic, status=status, **kwargs)


@pytest.mark.django_db
class TestWhatMayBeCollected:

    def test_a_task_joins_an_epic(self, epic, project):
        task = member(epic, project)
        task.full_clean(exclude=["number"])
        assert list(epic.epic_tasks.all()) == [task]

    def test_an_epic_reaches_across_projects(self, epic, workspace):
        # The whole point: a parent must share its child's project, an
        # epic must not.
        elsewhere = ProjectFactory(workspace=workspace, slug_prefix="OTH")
        task = TaskFactory(project=elsewhere, epic=epic)
        task.full_clean(exclude=["number"])
        assert task.epic_id == epic.pk

    def test_an_epic_never_reaches_across_workspaces(self, epic):
        foreign = ProjectFactory(workspace=WorkspaceFactory(), slug_prefix="FGN")
        task = TaskFactory(project=foreign, epic=epic)
        with pytest.raises(ValidationError, match="same workspace"):
            task.full_clean(exclude=["number"])

    def test_a_subtask_keeps_its_parent_and_joins_an_epic(self, epic, project):
        # The reason epics got their own field: with one hierarchy field
        # this task could be a subtask or be collected, never both.
        parent = TaskFactory(project=project)
        task = TaskFactory(project=project, parent=parent, epic=epic)
        task.full_clean(exclude=["number"])
        assert task.parent_id == parent.pk
        assert task.epic_id == epic.pk

    def test_an_epic_cannot_belong_to_an_epic(self, epic, project):
        other = TaskFactory(project=project, kind=Task.KIND_EPIC, epic=epic, status=Task.STATUS_PLANNED)
        with pytest.raises(ValidationError, match="cannot belong to another epic"):
            other.full_clean(exclude=["number"])

    def test_a_plain_task_cannot_collect(self, project):
        not_an_epic = TaskFactory(project=project)
        task = TaskFactory(project=project, epic=not_an_epic)
        with pytest.raises(ValidationError, match="only be collected by an epic"):
            task.full_clean(exclude=["number"])

    def test_an_epic_is_never_a_subtask(self, project):
        parent = TaskFactory(project=project)
        bad = TaskFactory(project=project, kind=Task.KIND_EPIC, parent=parent, status=Task.STATUS_PLANNED)
        with pytest.raises(ValidationError, match="cannot be a subtask"):
            bad.full_clean(exclude=["number"])

    def test_an_unknown_kind_is_rejected(self, project):
        # Short enough to reach ``clean()`` — the column itself stops
        # anything longer first.
        bad = TaskFactory(project=project, kind="story")
        with pytest.raises(ValidationError, match="Unknown kind"):
            bad.full_clean(exclude=["number"])

    @pytest.mark.parametrize(
        "field,value",
        [
            ("due_date", datetime.date(2026, 10, 9)),
            ("size", 5),
        ],
    )
    def test_an_epic_carries_none_of_the_fields_it_derives(self, project, field, value):
        bad = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, **{field: value})
        with pytest.raises(ValidationError, match="from its tasks"):
            bad.full_clean(exclude=["number"])


@pytest.mark.django_db
class TestTheWorkspaceToggle:

    def test_off_refuses_a_new_epic(self, workspace, project):
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        bad = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED)
        with pytest.raises(ValidationError, match="turned off"):
            bad.full_clean(exclude=["number"])

    def test_off_refuses_collecting_into_an_existing_one(self, workspace, project, epic):
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        task = TaskFactory(project=project, epic=epic)
        with pytest.raises(ValidationError, match="turned off"):
            task.full_clean(exclude=["number"])

    def test_off_keeps_what_already_exists(self, workspace, project, epic):
        task = member(epic, project)
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        # Turning the feature off hides it; it does not unpick the work.
        assert list(epic.epic_members()) == [task]

    def test_on_by_default(self, workspace):
        assert workspace.epics_enabled is True


@pytest.mark.django_db
class TestWhatAnEpicReadsOffItsTasks:

    def test_an_empty_epic_is_planned_and_zero(self, epic):
        assert epic.epic_counts == (0, 0)
        assert epic.epic_status == Task.STATUS_PLANNED

    def test_counts_are_done_over_total(self, epic, project):
        member(epic, project, Task.STATUS_DONE)
        member(epic, project, Task.STATUS_DONE)
        member(epic, project, Task.STATUS_IN_PROGRESS)
        assert epic.epic_counts == (2, 3)

    def test_cancelled_and_archived_tasks_leave_the_count(self, epic, project):
        from django.utils import timezone

        member(epic, project, Task.STATUS_DONE)
        member(epic, project, Task.STATUS_CANCELLED)
        member(epic, project, archived_at=timezone.now())
        # Neither is work any more, and counting them would hold the
        # epic's progress down forever.
        assert epic.epic_counts == (1, 1)

    @pytest.mark.parametrize(
        "statuses,expected",
        [
            ([Task.STATUS_DONE, Task.STATUS_DONE], Task.STATUS_DONE),
            ([Task.STATUS_DONE, Task.STATUS_TODO], Task.STATUS_IN_PROGRESS),
            ([Task.STATUS_IN_REVIEW, Task.STATUS_PLANNED], Task.STATUS_IN_PROGRESS),
            ([Task.STATUS_TODO, Task.STATUS_PLANNED], Task.STATUS_TODO),
            ([Task.STATUS_PLANNED, Task.STATUS_READY], Task.STATUS_PLANNED),
        ],
    )
    def test_the_status_ladder(self, epic, project, statuses, expected):
        for status in statuses:
            member(epic, project, status)
        assert epic.epic_status == expected

    def test_the_span_is_the_widest_reach_of_its_tasks(self, epic, project):
        member(epic, project, start_date=datetime.date(2026, 9, 15), due_date=datetime.date(2026, 10, 1))
        member(epic, project, start_date=datetime.date(2026, 9, 20), due_date=datetime.date(2026, 11, 14))
        assert epic.epic_span == (datetime.date(2026, 9, 15), datetime.date(2026, 11, 14))

    def test_a_span_with_no_dates_is_empty(self, epic, project):
        member(epic, project)
        assert epic.epic_span == (None, None)

    def test_a_plain_task_collects_nothing(self, project):
        assert TaskFactory(project=project).epic_counts == (0, 0)

    def test_losing_the_epic_leaves_the_task_alone(self, epic, project):
        task = member(epic, project)
        epic.delete()
        task.refresh_from_db()
        # SET_NULL, not CASCADE: deleting the umbrella must not delete
        # the work it was over.
        assert task.epic_id is None
        assert Task.objects.filter(pk=task.pk).exists()


@pytest.mark.django_db
class TestTheApiEnforcesTheSameRules:
    """The REST layer repeats the model's rules rather than trusting it.

    ``Model.clean()`` is not called on ``save()``, so the serializer is
    where an API client actually meets them.
    """

    @pytest.fixture
    def api(self, workspace):
        """An authenticated API client whose user owns the workspace."""
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=workspace.owner)
        return client

    def post(self, api, project, **extra):
        """POST a task payload to the API."""
        payload = {"project": project.id, "title": "From the API"}
        payload.update(extra)
        return api.post("/api/v1/tasks/", payload, format="json")

    def test_a_task_can_be_collected(self, api, project, epic):
        resp = self.post(api, project, epic=epic.id)
        assert resp.status_code == 201, resp.data
        assert Task.objects.get(pk=resp.data["id"]).epic_id == epic.pk

    def test_an_epic_can_be_created(self, api, project):
        resp = self.post(api, project, kind=Task.KIND_EPIC)
        assert resp.status_code == 201, resp.data
        assert Task.objects.get(pk=resp.data["id"]).kind == Task.KIND_EPIC

    def test_a_plain_task_cannot_collect(self, api, project):
        resp = self.post(api, project, epic=TaskFactory(project=project).id)
        assert resp.status_code == 400
        assert "epic" in resp.data

    def test_an_epic_from_another_workspace_is_refused(self, api, project):
        foreign = ProjectFactory(workspace=WorkspaceFactory(), slug_prefix="FGN")
        foreign_epic = TaskFactory(project=foreign, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED)
        resp = self.post(api, project, epic=foreign_epic.id)
        assert resp.status_code == 400
        assert "epic" in resp.data

    def test_an_epic_cannot_be_given_a_deadline(self, api, project):
        resp = self.post(api, project, kind=Task.KIND_EPIC, due_date="2026-10-09")
        assert resp.status_code == 400
        assert "due_date" in resp.data

    def test_the_switch_is_honoured(self, api, workspace, project):
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        resp = self.post(api, project, kind=Task.KIND_EPIC)
        assert resp.status_code == 400
        assert "kind" in resp.data


@pytest.mark.django_db
class TestEpicsNeverJoinACycle:
    """The cadence policy writes without validating, so it has to know.

    ``set_task_status`` saves directly and the bulk path uses
    ``QuerySet.update()``; neither calls ``full_clean()``. Without a
    guard an epic would silently acquire a cycle and then count toward
    the burndown, the velocity and the cycle summary.
    """

    @pytest.fixture
    def cadence(self, workspace):
        """Turn the workspace's cadence on with an anchored start."""
        workspace.cycle_settings = {"enabled": True, "length_weeks": 2, "start_date": "2026-09-01"}
        workspace.save(update_fields=["cycle_settings"])
        return workspace

    def test_the_in_memory_policy_skips_an_epic(self, cadence, project, epic):
        from apps.cycles.services import apply_cycle_policy

        epic.status = Task.STATUS_IN_PROGRESS
        assert apply_cycle_policy(epic) is False
        assert epic.cycle_id is None

    def test_the_policy_still_applies_to_real_work(self, cadence, project):
        from apps.cycles.services import apply_cycle_policy

        task = TaskFactory(project=project, status=Task.STATUS_IN_PROGRESS)
        assert apply_cycle_policy(task) is True
        assert task.cycle_id is not None

    def test_the_bulk_path_skips_an_epic(self, cadence, project, epic):
        from apps.tasks.bulk import _bulk_apply_cycle_policy

        task = TaskFactory(project=project)
        _bulk_apply_cycle_policy([epic.pk, task.pk], Task.STATUS_TODO)
        epic.refresh_from_db()
        task.refresh_from_db()
        assert epic.cycle_id is None
        assert task.cycle_id is not None
