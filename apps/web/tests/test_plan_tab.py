"""The project Plan tab — one project's work cut two levels deep.

The boards answer where the work is; the Plan answers how it sits inside
what the project has committed to. Both dimensions matter, so the cut is
a knob, and these tests pin what each cut produces and what the rollups
count. See docs/decisions/0037-milestones.md.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.milestones import services as milestone_services
from apps.milestones.tests.factories import MilestoneFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web import plan
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """A project with two milestones, an epic, and work across both."""
    workspace = WorkspaceFactory()
    user = UserFactory()
    WorkspaceMember.objects.create(user=user, workspace=workspace)
    project = ProjectFactory(workspace=workspace, slug_prefix="PLN")
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
    return workspace, user, project, soon, later, epic


def rows_for(tasks, cut):
    """Build the plan rows for a cut, reading the model directly."""
    return plan.build_plan_rows(list(tasks), cut)


@pytest.mark.django_db
class TestTheCut:
    """Which dimension leads is the knob; both readings stay honest."""

    def test_milestone_first_nests_epics_under_dates(self, setup):
        _, _, project, soon, _, epic = setup
        TaskFactory(project=project, milestone=soon, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        rows = rows_for(tasks, "me")
        groups = [(row["depth"], str(row["label"])) for row in rows if row["kind"] == "group"]

        assert groups == [(0, "Beta"), (1, "Search rework"), (1, "No epic")]

    def test_epic_first_nests_dates_under_efforts(self, setup):
        _, _, project, soon, later, epic = setup
        TaskFactory(project=project, milestone=soon, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=later, epic=epic, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        rows = rows_for(tasks, "em")
        groups = [(row["depth"], str(row["label"])) for row in rows if row["kind"] == "group"]

        assert groups == [(0, "Search rework"), (1, "Beta"), (1, "GA")]

    def test_dates_run_soonest_first_with_the_uncommitted_last(self, setup):
        _, _, project, soon, later, _ = setup
        TaskFactory(project=project, milestone=later, status=Task.STATUS_TODO)
        TaskFactory(project=project, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        labels = [str(row["label"]) for row in rows_for(tasks, "me") if row["kind"] == "group" and row["depth"] == 0]

        assert labels == ["Beta", "GA", "No milestone"]

    def test_flat_is_every_task_and_no_groups(self, setup):
        _, _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        TaskFactory(project=project, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        rows = rows_for(tasks, "flat")

        assert {row["kind"] for row in rows} == {"task"}
        assert len(rows) == 2


@pytest.mark.django_db
class TestTheRollups:
    """Counted the way the rest of the product counts."""

    def test_cancelled_and_shelved_work_leaves_the_count(self, setup):
        _, _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_DONE)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_CANCELLED)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO, archived_at=timezone.now())
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_DONE, archived_at=timezone.now())
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        group = next(row for row in rows_for(tasks, "me") if row["kind"] == "group")

        assert (group["done"], group["total"]) == (2, 3)
        assert group["percent"] == 67

    def test_work_due_after_its_own_date_counts_as_past_it(self, setup):
        _, _, project, soon, _, _ = setup
        late = TaskFactory(
            project=project,
            milestone=soon,
            status=Task.STATUS_TODO,
            due_date=soon.target_date + datetime.timedelta(days=5),
        )
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO, due_date=soon.target_date)
        TaskFactory(
            project=project,
            milestone=soon,
            status=Task.STATUS_DONE,
            due_date=soon.target_date + datetime.timedelta(days=9),
        )
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        rows = rows_for(tasks, "me")
        group = next(row for row in rows if row["kind"] == "group")
        late_row = next(row for row in rows if row["kind"] == "task" and row["task"].pk == late.pk)

        assert group["risk"] == 1
        assert late_row["late"] == 5

    def test_a_group_says_what_it_is_made_of(self, setup):
        _, _, project, soon, _, epic = setup
        TaskFactory(project=project, milestone=soon, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        group = next(row for row in rows_for(tasks, "me") if row["kind"] == "group")

        assert str(group["scope"]) == "1 epic · 1 direct"

    def test_a_task_hides_behind_every_group_above_it(self, setup):
        _, _, project, soon, _, epic = setup
        TaskFactory(project=project, milestone=soon, epic=epic, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        row = next(row for row in rows_for(tasks, "me") if row["kind"] == "task")

        assert row["ancestors"] == [f"ms-{soon.pk}", f"ms-{soon.pk}:ep-{epic.pk}"]


@pytest.mark.django_db
class TestThePage:
    """The tab, and the knob that refetches it."""

    def test_the_project_page_renders_the_plan(self, client, setup):
        workspace, user, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO, title="Ship the index")
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/projects/{project.slug_prefix}/?view=plan")
        body = resp.content.decode()

        assert resp.status_code == 200
        assert resp.context["view_mode"] == "plan"
        assert resp.context["plan_cut"] == "me"
        assert "Ship the index" in body
        assert soon.name in body

    def test_the_knob_picks_the_cut_and_the_panel_follows(self, client, setup):
        workspace, user, project, soon, _, epic = setup
        TaskFactory(project=project, milestone=soon, epic=epic, status=Task.STATUS_TODO)
        client.force_login(user)

        resp = client.get(
            f"/{workspace.slug}/projects/{project.slug_prefix}/?panel=plan&cut=em",
        )
        groups = [str(row["label"]) for row in resp.context["plan_rows"] if row["kind"] == "group"]

        assert resp.context["plan_cut"] == "em"
        assert groups[0] == epic.title

    def test_an_unknown_cut_falls_back_to_the_default(self, client, setup):
        workspace, user, project, _, _, _ = setup
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/projects/{project.slug_prefix}/?view=plan&cut=nonsense")

        assert resp.context["plan_cut"] == plan.DEFAULT_CUT

    def test_all_tasks_plans_the_whole_workspace(self, client, setup):
        workspace, user, project, soon, _, _ = setup
        other = ProjectFactory(workspace=workspace, slug_prefix="PLO")
        soon.projects.add(other)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        TaskFactory(project=other, milestone=soon, status=Task.STATUS_TODO)
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/tasks/?view=plan")
        group = next(row for row in resp.context["plan_rows"] if row["kind"] == "group")
        chips = {chip["project"].slug_prefix: chip for chip in group["chips"]}

        assert resp.context["view_mode"] == "plan"
        assert group["total"] == 2
        # Whose part is whose — the question only a cross-project plan
        # raises, so the chips carry a count each.
        assert set(chips) == {"PLN", "PLO"}
        assert chips["PLN"]["count"] == 1
        assert chips["PLO"]["count"] == 1
        assert resp.context["plan_shared_count"] == 1

    def test_a_project_in_scope_with_no_work_is_drawn_dashed(self, client, setup):
        workspace, user, project, soon, _, _ = setup
        quiet = ProjectFactory(workspace=workspace, slug_prefix="PLQ")
        soon.projects.add(quiet)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/tasks/?view=plan")
        group = next(row for row in resp.context["plan_rows"] if row["kind"] == "group")
        chips = {chip["project"].slug_prefix: chip for chip in group["chips"]}

        assert chips["PLQ"]["empty"] is True
        assert chips["PLN"]["empty"] is False

    def test_inside_one_project_the_chips_name_the_others(self, client, setup):
        workspace, user, project, soon, _, _ = setup
        other = ProjectFactory(workspace=workspace, slug_prefix="PLS")
        soon.projects.add(other)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        client.force_login(user)

        resp = client.get(f"/{workspace.slug}/projects/{project.slug_prefix}/?view=plan")
        group = next(row for row in resp.context["plan_rows"] if row["kind"] == "group")

        assert [chip["project"].slug_prefix for chip in group["chips"]] == ["PLS"]
        assert "plan_shared_count" not in resp.context


@pytest.mark.django_db
class TestTheNoteRow:
    """Work attached straight to a date is not an oversight."""

    def test_direct_work_under_a_milestone_says_what_it_is(self, setup):
        _, _, project, soon, _, epic = setup
        TaskFactory(project=project, milestone=soon, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        notes = [str(row["label"]) for row in rows_for(tasks, "me") if row["kind"] == "note"]

        assert notes == ["1 task without an epic — attached directly"]

    def test_the_other_cut_needs_no_note(self, setup):
        _, _, project, soon, _, epic = setup
        TaskFactory(project=project, milestone=soon, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        assert [row for row in rows_for(tasks, "em") if row["kind"] == "note"] == []


@pytest.mark.django_db
class TestTheFlatCut:
    """One list, so every row says which date it belongs to."""

    def test_every_row_names_its_milestone(self, setup):
        _, _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        TaskFactory(project=project, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        names = sorted(str(row["milestone_name"]) for row in rows_for(tasks, "flat") if row["kind"] == "task")

        assert names == [soon.name, "no milestone"]

    def test_a_long_list_stops_and_offers_the_rest(self, setup):
        _, _, project, soon, _, _ = setup
        for _index in range(plan.FLAT_LIMIT + 4):
            TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")

        rows = rows_for(tasks, "flat")
        folds = [str(row["label"]) for row in rows if row["kind"] == "fold"]

        assert folds == ["Show 4 more open"]
        assert sum(1 for row in rows if row["kind"] == "task") == plan.FLAT_LIMIT + 4


@pytest.mark.django_db
class TestTheSharedDateReportsTwice:
    """Inside a project a shared milestone carries two numbers.

    The plan is cut from one project's work, so its own rollup counts the
    local part. On a date several projects aim at, that part is not the
    commitment — "1/1 here" on a milestone that is 1/9 overall reads as
    finished when nothing is. See docs/decisions/0037-milestones.md.
    """

    def _shared(self, setup):
        """Put the Beta milestone in a second project and attach work there.

        Returns:
            ``(project, other, milestone)``.
        """
        workspace, _, project, soon, _, _ = setup
        other = ProjectFactory(workspace=workspace, slug_prefix="OTH")
        soon.projects.add(other)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_DONE)
        for _index in range(3):
            TaskFactory(project=other, milestone=soon, status=Task.STATUS_TODO)
        return project, other, soon

    def _milestone_row(self, project, milestone, **kwargs):
        """Build the plan and return the row for one milestone.

        Returns:
            The group row, or ``None`` when the milestone has no row.
        """
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")
        rows = plan.build_plan_rows(list(tasks), "milestone-epic", project=kwargs.get("scope_to", project))
        return next(
            (row for row in rows if row["kind"] == "group" and row["milestone"] == milestone),
            None,
        )

    def test_a_shared_date_carries_the_whole_count(self, setup):
        project, _, milestone = self._shared(setup)

        row = self._milestone_row(project, milestone)

        assert (row["done"], row["total"]) == (1, 1)
        assert (row["overall_done"], row["overall_total"]) == (1, 4)

    def test_a_date_this_project_owns_alone_says_nothing_extra(self, setup):
        _, _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        row = self._milestone_row(project, soon)

        assert "overall_total" not in row

    def test_across_the_workspace_there_is_no_second_number(self, setup):
        project, _, milestone = self._shared(setup)

        row = self._milestone_row(project, milestone, scope_to=None)

        assert "overall_total" not in row

    def test_the_whole_count_costs_one_query_however_many_dates(self, setup):
        """One aggregate for the page — a row each would be an N+1."""
        workspace, _, project, soon, later, _ = setup
        other = ProjectFactory(workspace=workspace, slug_prefix="OTH")
        for milestone in (soon, later):
            milestone.projects.add(other)
            TaskFactory(project=project, milestone=milestone, status=Task.STATUS_TODO)
            TaskFactory(project=other, milestone=milestone, status=Task.STATUS_TODO)
        tasks = list(Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic"))

        with CaptureQueriesContext(connection) as ctx:
            plan.build_plan_rows(tasks, "milestone-epic", project=project)

        # Two for the milestone scopes (the rows and their prefetched
        # projects), one for the counts across them — and none per row:
        # ``state()`` is handed the counts it would otherwise fetch twice
        # over, which is what made this an N+1 in the milestone count.
        assert len(ctx.captured_queries) == 3, [query["sql"] for query in ctx.captured_queries]


@pytest.mark.django_db
class TestTheLazyPanel:
    """``?panel=plan`` must return the panel, not the page around it.

    The Plan slot is lazy like the list and the timeline: ``acta.js``
    fetches it and swaps the response into the slot. With no template
    branch for it the fetch fell through to the full inner partial, so
    the whole panel wrapper — every slot, the Plan one empty — landed
    inside the Plan slot and the tab went black. A reload hid it, because
    that path renders the panel inline. The context was right the whole
    time, which is why asserting on ``resp.context`` alone missed it.
    """

    def test_the_project_panel_is_the_plan_alone(self, client, setup):
        workspace, user, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO, title="Ship the index")
        client.force_login(user)

        resp = client.get(
            f"/{workspace.slug}/projects/{project.slug_prefix}/?panel=plan",
            HTTP_HX_REQUEST="true",
        )
        body = resp.content.decode()

        assert "Ship the index" in body
        # The panel's own knob says ``closest [data-panel-slot]``; what must
        # not come back is a slot element, which is the wrapper in disguise.
        assert 'data-panel-slot="' not in body

    def test_the_workspace_panel_is_the_plan_alone(self, client, setup):
        workspace, user, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO, title="Ship the index")
        client.force_login(user)

        resp = client.get(
            f"/{workspace.slug}/tasks/?panel=plan",
            HTTP_HX_REQUEST="true",
        )
        body = resp.content.decode()

        assert "Ship the index" in body
        # The panel's own knob says ``closest [data-panel-slot]``; what must
        # not come back is a slot element, which is the wrapper in disguise.
        assert 'data-panel-slot="' not in body


@pytest.mark.django_db
class TestTheTreeSaysWhereTheDateStands:
    """A milestone row carries its state, not just its date.

    The tree knew the state all along — it drew every row the same and
    printed the word "target" under the date. Half of what a plan is for
    is seeing which commitments have gone past.
    """

    def _row(self, project, milestone):
        """Return the plan row for one milestone.

        Returns:
            The group row, or ``None``.
        """
        tasks = Task.objects.filter(project=project, kind=Task.KIND_TASK).select_related("milestone", "epic")
        rows = plan.build_plan_rows(list(tasks), "milestone-epic", project=project)
        return next(
            (row for row in rows if row["kind"] == "group" and row["milestone"] == milestone),
            None,
        )

    def test_a_date_that_has_gone_reads_as_overdue(self, setup):
        _, _, project, soon, _, _ = setup
        soon.target_date = timezone.localdate() - datetime.timedelta(days=3)
        soon.save(update_fields=["target_date"])
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        row = self._row(project, soon)

        assert row["state"] == "overdue"
        assert "3" in row["countdown"]

    def test_finished_work_reads_as_ready_to_close(self, setup):
        _, _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_DONE)

        row = self._row(project, soon)

        assert row["state"] == "complete"
        assert row["state_label"]

    def test_the_countdown_is_the_line_the_milestone_pages_print(self, setup):
        """One reading in one place — the tree must not grow its own wording."""
        _, _, project, soon, _, _ = setup
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        row = self._row(project, soon)

        assert row["countdown"] == milestone_services.countdown(soon, row["state"])
