"""The kanban board sliced into lanes.

The columns say what state the work is in; a lane says whose part of the
plan it is. A lane is a cut across the same columns, so the cell at the
crossing is "this status, in this milestone" — and the header of the row
reports what the milestone is as a whole, which the board's own filtered
rows cannot tell it.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import pytest

from apps.milestones.tests.factories import MilestoneFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web import kanban
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """A project aiming at two dates, with an epic across them."""
    workspace = WorkspaceFactory(epics_enabled=True)
    project = ProjectFactory(workspace=workspace, slug_prefix="LAN")
    soon = MilestoneFactory(
        workspace=workspace,
        name="Beta",
        target_date=timezone.localdate() + datetime.timedelta(days=10),
        projects=[project],
    )
    later = MilestoneFactory(
        workspace=workspace,
        name="GA",
        target_date=timezone.localdate() + datetime.timedelta(days=40),
        projects=[project],
    )
    epic = TaskFactory(project=project, kind=Task.KIND_EPIC, title="Search rework")
    return workspace, project, soon, later, epic


def lanes_for(project, axis):
    """Build lanes from the project's tasks, as the board does."""
    tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")
    return kanban.build_lanes(list(tasks), axis, project=project)


@pytest.mark.django_db
class TestTheSlice:
    """Which rows a board breaks into, and in what order."""

    def test_no_axis_leaves_the_board_whole(self, setup):
        _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        assert lanes_for(project, "none") == []

    def test_dates_run_soonest_first_with_the_uncommitted_last(self, setup):
        _, project, soon, later, _ = setup
        TaskFactory(project=project, milestone=later, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        TaskFactory(project=project, status=Task.STATUS_TODO)

        labels = [str(lane["label"]) for lane in lanes_for(project, "milestone")]

        assert labels == [soon.name, later.name, "No milestone"]

    def test_the_uncommitted_row_is_dropped_when_it_is_empty(self, setup):
        _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        labels = [str(lane["label"]) for lane in lanes_for(project, "milestone")]

        assert labels == [soon.name]

    def test_epics_run_by_name_with_the_ungrouped_last(self, setup):
        _, project, _, _, epic = setup
        other = TaskFactory(project=project, kind=Task.KIND_EPIC, title="Auth rework")
        TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=project, epic=other, status=Task.STATUS_TODO)
        TaskFactory(project=project, status=Task.STATUS_TODO)

        labels = [str(lane["label"]) for lane in lanes_for(project, "epic")]

        assert labels == [other.title, epic.title, "No epic"]


@pytest.mark.django_db
class TestTheCells:
    """A lane holds one cell per column, and the work lands in the right one."""

    def test_every_status_gets_a_cell_even_when_empty(self, setup):
        _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        lane = lanes_for(project, "milestone")[0]

        assert [cell["key"] for cell in lane["cells"]] == list(Task.KANBAN_STATUS_VALUES)

    def test_work_lands_under_its_own_status(self, setup):
        _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, title="doing", status=Task.STATUS_IN_PROGRESS)
        TaskFactory(project=project, milestone=soon, title="waiting", status=Task.STATUS_TODO)

        lane = lanes_for(project, "milestone")[0]
        placed = {cell["key"]: [task.title for task in cell["tasks"]] for cell in lane["cells"]}

        assert placed[Task.STATUS_IN_PROGRESS] == ["doing"]
        assert placed[Task.STATUS_TODO] == ["waiting"]


@pytest.mark.django_db
class TestTheHeader:
    """What a lane reports about itself."""

    def test_it_counts_what_is_done_and_what_runs_past_the_date(self, setup):
        _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_DONE)
        TaskFactory(
            project=project,
            milestone=soon,
            status=Task.STATUS_TODO,
            due_date=soon.target_date + datetime.timedelta(days=5),
        )

        lane = lanes_for(project, "milestone")[0]

        assert (lane["done"], lane["total"], lane["percent"]) == (1, 2, 50)
        assert lane["risk"] == 1

    def test_it_says_where_the_date_stands(self, setup):
        _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        lane = lanes_for(project, "milestone")[0]

        assert lane["state"] == "open"
        assert lane["state_label"]
        assert lane["countdown"]

    def test_a_settled_date_starts_folded(self, setup):
        """Nobody opened the board to look at what is already finished."""
        _, project, soon, _, _ = setup
        soon.closed_at = timezone.now()
        soon.save(update_fields=["closed_at"])
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        lane = lanes_for(project, "milestone")[0]

        assert lane["state"] == "closed"
        assert lane["collapsed"] is True

    def test_an_open_date_starts_unfolded(self, setup):
        _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        assert lanes_for(project, "milestone")[0]["collapsed"] is False


@pytest.mark.django_db
class TestTheCost:
    """One batch for the board, not one query per lane."""

    def test_more_lanes_do_not_mean_more_queries(self, setup):
        workspace, project, soon, later, _ = setup
        for milestone in (soon, later):
            TaskFactory(project=project, milestone=milestone, status=Task.STATUS_TODO)
        tasks = list(Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic"))

        with CaptureQueriesContext(connection) as two:
            kanban.build_lanes(tasks, "milestone", project=project)

        for index in range(4):
            extra = MilestoneFactory(
                workspace=workspace,
                name=f"M{index}",
                target_date=timezone.localdate() + datetime.timedelta(days=60 + index),
                projects=[project],
            )
            TaskFactory(project=project, milestone=extra, status=Task.STATUS_TODO)
        tasks = list(Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic"))

        with CaptureQueriesContext(connection) as six:
            lanes = kanban.build_lanes(tasks, "milestone", project=project)

        assert len(lanes) == 6
        assert len(six.captured_queries) == len(two.captured_queries) == 1


@pytest.mark.django_db
class TestTheBoardKeepsItsOwnRules:
    """A sliced board is still the board, and obeys the same toggles.

    The backlog toggle hides the ``planned`` / ``ready`` columns
    client-side, keyed on ``data-kanban-column``. Without that marker the
    lanes showed those columns when the plain board did not — and a card
    dragged into ``ready`` then vanished on the next filter pass, because
    the row filter hid it while its column stayed open.
    """

    def test_every_cell_is_a_kanban_column(self, client, setup):
        workspace, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/tasks/?view=kanban&lanes=milestone")
        body = resp.content.decode()

        assert 'data-kanban-column="ready"' in body
        assert 'data-lane="ms-' in body

    def test_the_knob_offers_every_axis(self, client, setup):
        workspace, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/tasks/?view=kanban")

        assert [option["key"] for option in resp.context["lanes_options"]] == ["none", "milestone", "epic"]
        assert resp.context["lanes_axis"] == "none"
        assert resp.context["lanes"] == []
