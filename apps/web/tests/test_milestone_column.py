"""The table's optional Milestone column.

The other half of "where does this sit in the plan", next to the Epic
column and on the same terms: off until asked for in Display, offered
only where dates are planned, and sorted by the date rather than the
name — a column of commitments ordered alphabetically answers nothing.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.milestones.tests.factories import MilestoneFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """A project whose work aims at two dates, and one task that aims at none."""
    workspace = WorkspaceFactory()
    project = ProjectFactory(workspace=workspace, slug_prefix="COL")
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
    return workspace, project, soon, later


@pytest.mark.django_db
class TestTheColumn:
    """What the table shows, and only when asked."""

    def test_off_by_default_but_rendered(self, client, setup):
        """Hidden by a class, not omitted — the toggle runs client-side.

        A server-gated column only appeared after a reload, because the
        dock's toggles cancel their own request. So the cells are always
        there and the class decides.
        """
        workspace, project, soon, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/tasks/?view=table")
        body = resp.content.decode()

        assert resp.context["show_milestone"] is False
        assert "is-hide-milestone" in body
        assert 'data-col="milestone"' in body

    def test_shown_on_request_and_names_the_date(self, client, setup):
        workspace, project, soon, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/tasks/?view=table&show_milestone=1")
        body = resp.content.decode()

        assert resp.context["show_milestone"] is True
        assert "is-hide-milestone" not in body
        assert soon.name in body

    def test_a_workspace_that_plans_no_dates_is_not_offered_it(self, client, db):
        """Having a milestone is the switch — the same rule the axis follows."""
        workspace = WorkspaceFactory()
        project = ProjectFactory(workspace=workspace)
        TaskFactory(project=project, status=Task.STATUS_TODO)
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/tasks/?view=table&show_milestone=1")

        assert resp.context["show_milestone"] is False
        assert resp.context["milestones_enabled"] is False
        # Not merely hidden — never emitted, so there is no column to show.
        assert 'data-col="milestone"' not in resp.content.decode()


@pytest.mark.django_db
class TestTheOrder:
    """Sorted by the date it commits to, not by what it is called."""

    def test_soonest_first_with_the_uncommitted_last(self, client, setup):
        workspace, project, soon, later = setup
        # Named so that an alphabetical sort would invert them: "GA" is the
        # later date and sorts first by name.
        TaskFactory(project=project, milestone=later, title="aims later", status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, title="aims soon", status=Task.STATUS_TODO)
        TaskFactory(project=project, title="aims at nothing", status=Task.STATUS_TODO)
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/tasks/?view=table&order=milestone")
        titles = [task.title for task in resp.context["tasks"]]

        assert titles == ["aims soon", "aims later", "aims at nothing"]

    def test_the_other_way_round_still_sinks_the_uncommitted(self, client, setup):
        workspace, project, soon, later = setup
        TaskFactory(project=project, milestone=later, title="aims later", status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, title="aims soon", status=Task.STATUS_TODO)
        TaskFactory(project=project, title="aims at nothing", status=Task.STATUS_TODO)
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/tasks/?view=table&order=-milestone")
        titles = [task.title for task in resp.context["tasks"]]

        assert titles == ["aims later", "aims soon", "aims at nothing"]


@pytest.mark.django_db
class TestTheCost:
    """A column that reads a relation is a column that can explode."""

    def test_the_column_costs_nothing(self, client, setup):
        """Measured against the same page without it, not against a number.

        The row reads ``task.milestone.name``, so without the join this is
        a query per row. A fixed bound would drift with everything else on
        the page; the difference is the thing being claimed.
        """
        workspace, project, soon, _ = setup
        for index in range(30):
            TaskFactory(project=project, milestone=soon, title=f"t{index}", status=Task.STATUS_TODO)
        client.force_login(workspace.owner)
        url = reverse("web:all_tasks") + "?view=table"

        with CaptureQueriesContext(connection) as without:
            assert client.get(url).status_code == 200
        with CaptureQueriesContext(connection) as with_column:
            assert client.get(url + "&show_milestone=1").status_code == 200

        added = len(with_column.captured_queries) - len(without.captured_queries)
        # Not an exact equality: the two renders differ by a handful of
        # session / cookie reads either way. What must never happen is a
        # query per row.
        assert added <= 1, f"The column added {added} queries over 30 rows — the queryset must preload the milestone."


@pytest.mark.django_db
class TestTheToggleReachesEverySurface:
    """One switch per dimension, not one per surface.

    The Display entry used to govern the table column alone, which left it
    dead on the kanban — a checkbox that ticks and changes nothing. It now
    governs the chip on list rows and kanban cards as well, and the
    subtree it acts on is marked with ``data-plan-scope``.
    """

    def test_the_board_carries_the_scope_and_the_chip(self, client, setup):
        workspace, project, soon, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/tasks/?view=kanban")
        body = resp.content.decode()

        assert "data-plan-scope" in body
        assert "is-hide-milestone" in body
        assert 'data-col="milestone"' in body

    def test_asking_for_it_lifts_the_class(self, client, setup):
        workspace, project, soon, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/tasks/?view=kanban&show_milestone=1")

        assert "is-hide-milestone" not in resp.content.decode()

    def test_a_page_that_never_asks_hides_nothing(self, client, setup):
        """My Work computes no column flags — its chips must stay visible."""
        workspace, project, soon, _ = setup
        TaskFactory(
            project=project,
            milestone=soon,
            assignee=workspace.owner,
            status=Task.STATUS_TODO,
        )
        client.force_login(workspace.owner)

        resp = client.get(f"/{workspace.slug}/my-work/")
        body = resp.content.decode()

        assert "is-hide-milestone" not in body
        assert soon.name in body
