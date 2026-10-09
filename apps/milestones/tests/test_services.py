"""The readings the milestone pages are built from.

Progress, state, the slices, risk and the burndown are all derived — the
model stores a date, a scope and the fact that someone closed it. These
tests pin the derivations, because the pages and the MCP tools both read
them and a drift between the two is exactly what ADR 0037 set out to
prevent.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.milestones import services
from apps.milestones.models import Milestone
from apps.milestones.tests.factories import MilestoneFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def scope():
    """A milestone two weeks out, covering two projects of its workspace."""
    backend = ProjectFactory(slug_prefix="BCK")
    infra = ProjectFactory(workspace=backend.workspace, slug_prefix="INF")
    milestone = MilestoneFactory(
        workspace=backend.workspace,
        name="Search beta",
        projects=[
            backend,
            infra,
        ],
    )
    return backend, infra, milestone


def status_event(task, to_status, when):
    """Record a status change in the activity log at a given moment.

    Args:
        task: The task that moved.
        to_status: The status it moved to.
        when: When it moved.

    Returns:
        The written :class:`ActivityLog` row.
    """
    event = ActivityLog.objects.create(
        workspace=task.project.workspace,
        project=task.project,
        target_type=ActivityLog.TARGET_TASK,
        target_id=task.id,
        event_type="task.status_changed",
        payload={
            "from": Task.STATUS_TODO,
            "to": to_status,
        },
    )
    ActivityLog.objects.filter(pk=event.pk).update(created_at=when)
    return event


def milestone_event(task, milestone, when, *, leaving=False):
    """Record a task joining or leaving a milestone at a given moment.

    Args:
        task: The task that moved.
        milestone: The milestone it joined or left.
        when: When it moved.
        leaving: ``True`` to record a departure instead of a join.

    Returns:
        The written :class:`ActivityLog` row.
    """
    payload = (
        {"from_milestone_id": milestone.id, "to_milestone_id": None}
        if leaving
        else {"from_milestone_id": None, "to_milestone_id": milestone.id}
    )
    event = ActivityLog.objects.create(
        workspace=task.project.workspace,
        project=task.project,
        target_type=ActivityLog.TARGET_TASK,
        target_id=task.id,
        event_type="task.milestone_changed",
        payload=payload,
    )
    ActivityLog.objects.filter(pk=event.pk).update(created_at=when)
    return event


class TestRows:
    """One row per milestone, with the numbers the list draws."""

    def test_the_row_counts_only_what_counts_as_work(self, scope):
        backend, _, milestone = scope
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_CANCELLED)
        TaskFactory(
            project=backend,
            milestone=milestone,
            status=Task.STATUS_DONE,
            archived_at=timezone.now(),
        )
        TaskFactory(
            project=backend,
            milestone=milestone,
            status=Task.STATUS_TODO,
            archived_at=timezone.now(),
        )

        row = services.workspace_rows(milestone.workspace)[0]

        assert (row["done"], row["total"], row["open"]) == (2, 3, 1)
        assert row["percent"] == 67

    def test_a_project_in_scope_with_no_work_keeps_its_chip(self, scope):
        backend, infra, milestone = scope
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)

        row = services.workspace_rows(milestone.workspace)[0]
        chips = {chip["project"].slug_prefix: chip for chip in row["projects"]}

        assert set(chips) == {"BCK", "INF"}
        assert chips["INF"]["total"] == 0

    def test_a_shared_day_is_named_on_both_rows(self, scope):
        backend, _, milestone = scope
        twin = MilestoneFactory(
            workspace=milestone.workspace,
            name="Onboarding v2 live",
            target_date=milestone.target_date,
            projects=[backend],
        )

        rows = {row["milestone"].name: row for row in services.workspace_rows(milestone.workspace)}

        assert rows[milestone.name]["same_day"] == [twin.name]
        assert rows[twin.name]["same_day"] == [milestone.name]

    def test_the_list_costs_a_fixed_number_of_queries(self, scope, django_assert_num_queries):
        backend, infra, milestone = scope
        for _index in range(6):
            TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        for _index in range(4):
            MilestoneFactory(
                workspace=milestone.workspace,
                projects=[
                    backend,
                    infra,
                ],
            )

        # Milestones, their scope, and one pass over the attached work —
        # whatever the number of rows.
        with django_assert_num_queries(3):
            services.workspace_rows(milestone.workspace)


class TestState:
    """Closed is stored; everything else is read off the date and the work."""

    def test_all_done_reads_as_ready_to_close_even_past_the_date(self, scope):
        backend, _, milestone = scope
        milestone.target_date = timezone.localdate() - datetime.timedelta(days=3)
        milestone.save()
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)

        row = services.workspace_rows(milestone.workspace)[0]

        assert row["state"] == Milestone.STATE_COMPLETE

    def test_an_empty_milestone_is_open_not_complete(self, scope):
        _, _, milestone = scope

        row = services.workspace_rows(milestone.workspace)[0]

        assert row["state"] == Milestone.STATE_OPEN
        assert row["total"] == 0

    def test_closing_outranks_the_work(self, scope):
        backend, _, milestone = scope
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        milestone.closed_at = timezone.now()
        milestone.save()

        row = services.workspace_rows(milestone.workspace)[0]

        assert row["state"] == Milestone.STATE_CLOSED


class TestRisk:
    """Two sides, and the second is the one people forget."""

    def test_work_due_after_the_date_is_at_risk(self, scope):
        backend, _, milestone = scope
        TaskFactory(
            project=backend,
            milestone=milestone,
            status=Task.STATUS_TODO,
            due_date=milestone.target_date + datetime.timedelta(days=4),
        )
        TaskFactory(
            project=backend,
            milestone=milestone,
            status=Task.STATUS_TODO,
            due_date=milestone.target_date,
        )

        rows = services.at_risk(milestone)

        assert [row[1] for row in rows] == [4]
        assert rows[0][2] == "due after the milestone"

    def test_once_the_date_has_passed_everything_open_is_at_risk(self, scope):
        backend, _, milestone = scope
        milestone.target_date = timezone.localdate() - datetime.timedelta(days=2)
        milestone.save()
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO, due_date=None)
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)

        rows = services.at_risk(milestone)

        assert [row[2] for row in rows] == ["still open"]
        assert rows[0][1] == 2

    def test_the_slice_carries_its_own_risk(self, scope):
        backend, infra, milestone = scope
        TaskFactory(
            project=backend,
            milestone=milestone,
            status=Task.STATUS_TODO,
            due_date=milestone.target_date + datetime.timedelta(days=1),
        )
        TaskFactory(project=infra, milestone=milestone, status=Task.STATUS_TODO)

        row = services.workspace_rows(milestone.workspace)[0]
        chips = {chip["project"].slug_prefix: chip for chip in row["projects"]}

        assert (chips["BCK"]["risk"], chips["INF"]["risk"]) == (1, 0)
        assert row["risk"] == 1


class TestGrouping:
    """Past folds away, the rest runs by month — and the fold still talks."""

    def test_past_comes_first_and_says_what_is_wrong_behind_it(self, scope):
        backend, _, milestone = scope
        milestone.target_date = timezone.localdate() - datetime.timedelta(days=5)
        milestone.save()
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)

        groups = services.group_rows(services.workspace_rows(milestone.workspace))

        assert groups[0]["key"] == "past"
        assert groups[0]["collapsed"] is True
        assert "overdue by 5 days" in groups[0]["summary"][0]["text"]
        assert groups[0]["summary"][0]["tone"] == "risk"

    def test_a_closed_past_milestone_says_so_quietly(self, scope):
        _, _, milestone = scope
        milestone.target_date = timezone.localdate() - datetime.timedelta(days=5)
        milestone.closed_at = timezone.now()
        milestone.save()

        groups = services.group_rows(services.workspace_rows(milestone.workspace))

        assert groups[0]["summary"] == [{"text": "all closed", "tone": "muted"}]

    def test_upcoming_dates_group_by_month(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=3)
        milestone.save()
        later = MilestoneFactory(
            workspace=milestone.workspace,
            target_date=today + datetime.timedelta(days=70),
            projects=[backend],
        )

        groups = services.group_rows(services.workspace_rows(milestone.workspace))
        labels = [group["label"] for group in groups]

        assert len(groups) == 2
        assert labels[0] == f"{milestone.target_date:%B} {milestone.target_date.year}"
        assert labels[1] == f"{later.target_date:%B} {later.target_date.year}"


class TestDetail:
    """The page's own readings: slices, groups, and what is not counted."""

    def test_the_epic_slice_is_read_off_the_tasks(self, scope):
        backend, infra, milestone = scope
        epic = TaskFactory(project=backend, kind=Task.KIND_EPIC, title="Ranking")
        TaskFactory(project=backend, milestone=milestone, epic=epic, status=Task.STATUS_DONE)
        TaskFactory(project=infra, milestone=milestone, epic=epic, status=Task.STATUS_TODO)
        TaskFactory(project=infra, milestone=milestone, status=Task.STATUS_TODO)

        context = services.detail_context(milestone)
        rows = {row["label"]: row for row in context["by_epic"]}

        assert rows["Ranking"]["total"] == 2
        assert rows["Ranking"]["done"] == 1
        assert rows["No epic"]["total"] == 1

    def test_what_the_counting_rule_leaves_out_is_still_listed(self, scope):
        backend, _, milestone = scope
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_CANCELLED)
        TaskFactory(
            project=backend,
            milestone=milestone,
            status=Task.STATUS_TODO,
            archived_at=timezone.now(),
        )
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)

        context = services.detail_context(milestone)
        labels = [group["label"] for group in context["groups"]]
        excluded = context["groups"][-1]

        assert context["total"] == 1
        assert context["attached_count"] == 3
        assert str(labels[-1]) == "Not counted or archived"
        assert excluded["count"] == 2
        assert "cancelled not counted" in context["note"]
        assert "archived unfinished dropped" in context["note"]

    def test_done_work_gets_its_own_group(self, scope):
        backend, _, milestone = scope
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_IN_PROGRESS)

        context = services.detail_context(milestone)
        statuses = [group["status"] for group in context["groups"]]

        assert statuses == [Task.STATUS_IN_PROGRESS, Task.STATUS_DONE]


class TestBurndown:
    """Read off the activity log, never from due dates."""

    def test_nothing_attached_draws_no_chart(self, scope):
        _, _, milestone = scope

        assert services.burndown(milestone) is None

    def test_remaining_falls_on_the_day_the_work_was_finished(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=3)
        milestone.save()
        joined = timezone.now() - datetime.timedelta(days=4)
        done = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        open_task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        milestone_event(done, milestone, joined)
        milestone_event(open_task, milestone, joined)
        status_event(done, Task.STATUS_DONE, timezone.now() - datetime.timedelta(days=2))

        chart = services.burndown(milestone, today=today)

        assert chart["labels"][0] == (today - datetime.timedelta(days=4)).isoformat()
        assert chart["remaining"][0] == 2
        assert chart["remaining"][1] == 2
        # The day it was finished is the day the line drops.
        assert chart["remaining"][2] == 1
        assert chart["scope"][0] == 2
        assert chart["total"] == 2
        assert chart["open"] == 1

    def test_work_that_joined_later_shows_as_a_scope_move(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        first = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        late = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        milestone_event(first, milestone, timezone.now() - datetime.timedelta(days=5))
        milestone_event(late, milestone, timezone.now() - datetime.timedelta(days=1))

        chart = services.burndown(milestone, today=today)

        assert chart["scope"][0] == 1
        assert chart["scope"][5] == 2
        assert "scope moved on 1 day" in chart["scope_note"]

    def test_work_that_left_drops_out_of_the_scope_line(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        stayed = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        left = TaskFactory(project=backend, status=Task.STATUS_TODO)
        milestone_event(stayed, milestone, timezone.now() - datetime.timedelta(days=5))
        milestone_event(left, milestone, timezone.now() - datetime.timedelta(days=5))
        milestone_event(left, milestone, timezone.now() - datetime.timedelta(days=2), leaving=True)

        chart = services.burndown(milestone, today=today)

        assert chart["scope"][0] == 2
        assert chart["scope"][-1] is None
        assert chart["scope"][3] == 1

    def _replayable(self, backend, milestone, *, closed, open_tasks, over_days=24):
        """Seed enough closes for the forecast to answer at all.

        Below ten closes across three weeks it refuses, which is the
        point of the refusal — so a test about the verdict has to clear
        that bar first. The closes are spread evenly from ``over_days``
        ago to today, because the span they cover is the history the
        forecast measures.
        """
        joined = timezone.now() - datetime.timedelta(days=over_days + 2)
        for index in range(closed):
            task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
            milestone_event(task, milestone, joined)
            ago = over_days - index * over_days // max(1, closed - 1)
            status_event(task, Task.STATUS_DONE, timezone.now() - datetime.timedelta(days=ago))
        for _index in range(open_tasks):
            task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
            milestone_event(task, milestone, joined)

    def test_a_date_the_pace_cannot_reach_reads_as_unlikely(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=1)
        milestone.save()
        self._replayable(backend, milestone, closed=12, open_tasks=40)

        chart = services.burndown(milestone, today=today)

        assert chart["forecast"]["state"] == "ready"
        assert chart["reading"] == "unlikely"
        assert chart["forecast"]["chance"] <= 30

    def test_a_date_it_clears_easily_reads_as_likely(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=120)
        milestone.save()
        self._replayable(backend, milestone, closed=12, open_tasks=3)

        chart = services.burndown(milestone, today=today)

        assert chart["reading"] == "likely"
        assert chart["forecast"]["chance"] >= 70

    def test_too_little_history_gets_no_forecast_and_no_lines(self, scope):
        """What the 0.05-a-day floor used to answer with 865 days."""
        backend, _, milestone = scope
        today = timezone.localdate()
        joined = timezone.now() - datetime.timedelta(days=10)
        for _index in range(6):
            task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
            milestone_event(task, milestone, joined)

        chart = services.burndown(milestone, today=today)

        assert chart["reading"] == "thin"
        assert set(chart["p50_line"]) == {None}
        assert set(chart["p85_line"]) == {None}

    def test_the_two_lines_run_from_today_to_their_own_landing(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=30)
        milestone.save()
        self._replayable(backend, milestone, closed=12, open_tasks=20)

        chart = services.burndown(milestone, today=today)
        here = chart["today_index"]
        drawn = lambda line: [i for i, v in enumerate(line) if v is not None]  # noqa: E731

        assert chart["p50_line"][here] == chart["open"]
        assert chart["p85_line"][here] == chart["open"]
        # The slower line lands no earlier than the faster one.
        assert max(drawn(chart["p85_line"])) >= max(drawn(chart["p50_line"]))

    def test_the_day_it_was_filled_is_not_a_day_work_arrived(self, scope):
        """A fortnight-old milestone must not read as one that never ends.

        ``_replayable`` attaches everything inside the window, so if the
        first fill were drawn as an arrival the replay would expect forty
        more tasks on a typical day and no pace would ever converge. The
        work is already the remainder; its day is not evidence of growth.
        """
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=60)
        milestone.save()
        self._replayable(backend, milestone, closed=12, open_tasks=20)

        chart = services.burndown(milestone, today=today)

        assert chart["reading"] != "never"
        assert chart["forecast"]["p50"] is not None
        assert chart["forecast"]["arrive_per_day"] == 0

    def test_work_poured_in_after_the_start_pushes_the_dates_out(self, scope):
        """The replay draws the top-ups the scope line already shows."""
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=60)
        milestone.save()
        self._replayable(backend, milestone, closed=12, open_tasks=20)
        steady = services.burndown(milestone, today=today)

        for index in range(10):
            late = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
            milestone_event(late, milestone, timezone.now() - datetime.timedelta(days=index + 1))

        grown = services.burndown(milestone, today=today)

        assert grown["forecast"]["arrive_per_day"] > 0
        assert grown["forecast"]["p50"] > steady["forecast"]["p50"]

    def test_a_milestone_filling_faster_than_it_closes_gets_no_date(self, scope):
        """The sixth reading, and the one the first cut could not reach."""
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=30)
        milestone.save()
        self._replayable(backend, milestone, closed=12, open_tasks=10)
        for index in range(40):
            late = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
            milestone_event(late, milestone, timezone.now() - datetime.timedelta(days=index % 20 + 1))

        chart = services.burndown(milestone, today=today)

        assert chart["reading"] == "never"
        assert chart["forecast"]["diverges"] is True
        assert chart["forecast"]["p50"] is None
        # No line to zero, because there is no zero.
        assert set(chart["p50_line"]) == {None}
        assert set(chart["p85_line"]) == {None}
        # The two paces are what the page says instead of a date.
        assert chart["forecast"]["arrive_per_day"] > chart["forecast"]["per_day"]

    def test_a_milestone_filled_over_two_days_is_not_told_it_never_ends(self, scope):
        """The defect that reached production, as the page met it.

        A milestone opened on Monday and filled across Monday and
        Tuesday. Excluding only the first day left the second to be
        divided by the four-week window, which read as a team taking in
        more than it closes — so a milestone three days old, over work
        that was mostly finished before it existed, was told it would
        never converge.
        """
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=60)
        milestone.save()
        Milestone.objects.filter(pk=milestone.pk).update(created_at=timezone.now() - datetime.timedelta(days=3))
        # Day one of the fill: the work that was already finished.
        for index in range(14):
            task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
            milestone_event(task, milestone, timezone.now() - datetime.timedelta(days=3))
            status_event(task, Task.STATUS_DONE, timezone.now() - datetime.timedelta(days=24 - index))
        # Day two: the rest of the scope, carried in the next afternoon.
        for _index in range(20):
            task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
            milestone_event(task, milestone, timezone.now() - datetime.timedelta(days=2))

        chart = services.burndown(milestone, today=today)

        assert chart["reading"] != "never"
        assert chart["forecast"]["p50"] is not None
        # Nothing was replayed, because three days of a container's life
        # is not evidence about inflow either way.
        assert chart["forecast"]["arrive_per_day"] is None

    def test_work_already_done_when_it_was_swept_in_is_not_an_arrival(self, scope):
        """Both sides of the sum have to be measured the same way.

        A milestone filed in over finished work collects tasks whose
        closing never reached the log — imported history, or work closed
        before the log existed. They are not in the remainder, so they
        cannot be arrivals into it; counting them invents a backlog out
        of a filing decision.
        """
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=60)
        milestone.save()
        self._replayable(backend, milestone, closed=12, open_tasks=10)
        # Swept in well after the fill, and finished long before it —
        # with no closing event to say when.
        for _index in range(30):
            archived = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
            milestone_event(archived, milestone, timezone.now() - datetime.timedelta(days=4))

        chart = services.burndown(milestone, today=today)

        assert chart["forecast"]["arrive_per_day"] == 0
        assert chart["reading"] != "never"

    def test_a_milestone_filed_in_late_still_has_the_pace_it_ran_at(self, scope):
        """The container was opened this morning; the work was not.

        Closing dates come off the activity log, so work that was
        finished before it was attached still says when it was finished.
        Refusing to read that because the milestone row is a day old
        would be refusing the only history there is.
        """
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=40)
        milestone.save()
        for _index in range(4):
            open_task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
            milestone_event(open_task, milestone, timezone.now())
        for index in range(12):
            old = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
            status_event(old, Task.STATUS_DONE, timezone.now() - datetime.timedelta(days=index * 2 + 2))
            milestone_event(old, milestone, timezone.now())

        chart = services.burndown(milestone, today=today)

        assert chart["forecast"]["state"] == "ready"
        assert chart["forecast"]["closed"] == 12
        assert chart["total"] == 16
        assert chart["open"] == 4
        # Counted as done from the day they joined, not re-burned today.
        assert chart["remaining"][chart["today_index"]] == 4

    def test_closes_too_bunched_up_are_still_refused(self, scope):
        """Ten closes in two days says nothing about the next month."""
        backend, _, milestone = scope
        today = timezone.localdate()
        for _index in range(4):
            open_task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
            milestone_event(open_task, milestone, timezone.now() - datetime.timedelta(days=30))
        for index in range(12):
            burst = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
            status_event(burst, Task.STATUS_DONE, timezone.now() - datetime.timedelta(days=index % 2))
            milestone_event(burst, milestone, timezone.now() - datetime.timedelta(days=30))

        chart = services.burndown(milestone, today=today)

        assert chart["reading"] == "thin"
        assert chart["forecast"]["closed"] == 12

    def test_everything_done_says_so(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        milestone_event(task, milestone, timezone.now() - datetime.timedelta(days=2))
        status_event(task, Task.STATUS_DONE, timezone.now() - datetime.timedelta(days=1))

        chart = services.burndown(milestone, today=today)

        assert chart["open"] == 0
        assert chart["reading"] == "done"
        assert set(chart["p50_line"]) == {None}


class TestFilling:
    """The reverse picker: standing on the date, pick work for it."""

    def test_only_in_scope_unfinished_work_is_offered(self, scope):
        backend, infra, milestone = scope
        outside = ProjectFactory(workspace=milestone.workspace, slug_prefix="WEB")
        wanted = TaskFactory(project=backend, status=Task.STATUS_TODO)
        TaskFactory(project=backend, status=Task.STATUS_DONE)
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        TaskFactory(project=outside, status=Task.STATUS_TODO)
        TaskFactory(project=infra, kind=Task.KIND_EPIC, title="An epic is not work")

        offered = [row["task"].id for row in services.fill_candidates(milestone)]

        assert offered == [wanted.id]

    def test_work_committed_elsewhere_is_offered_and_says_so(self, scope):
        backend, _, milestone = scope
        other = MilestoneFactory(workspace=milestone.workspace, name="Later", projects=[backend])
        task = TaskFactory(project=backend, milestone=other, status=Task.STATUS_TODO)

        rows = services.fill_candidates(milestone)

        assert [row["task"].id for row in rows] == [task.id]
        assert rows[0]["other"].id == other.id

    def test_search_matches_a_title_or_a_slug(self, scope):
        backend, _, milestone = scope
        hit = TaskFactory(project=backend, status=Task.STATUS_TODO, title="Shard the search index")
        TaskFactory(project=backend, status=Task.STATUS_TODO, title="Rotate the signing keys")

        by_title = services.fill_candidates(milestone, "shard")
        by_slug = services.fill_candidates(milestone, hit.slug)

        assert [row["task"].id for row in by_title] == [hit.id]
        assert [row["task"].id for row in by_slug] == [hit.id]

    def test_a_match_outside_the_scope_is_named_not_hidden(self, scope):
        _, _, milestone = scope
        outside = ProjectFactory(workspace=milestone.workspace, slug_prefix="WEB", name="Web")
        task = TaskFactory(project=outside, status=Task.STATUS_TODO, title="Search results page")

        rows = services.out_of_scope_matches(milestone, "search")

        assert [row["task"].id for row in rows] == [task.id]
        assert rows[0]["project"].name == "Web"

    def test_nothing_is_listed_outside_scope_without_a_search(self, scope):
        _, _, milestone = scope
        outside = ProjectFactory(workspace=milestone.workspace, slug_prefix="WEB")
        TaskFactory(project=outside, status=Task.STATUS_TODO)

        assert services.out_of_scope_matches(milestone, "") == []


class TestMembershipReports:
    """Two questions, and neither of them joins anything by itself."""

    def test_near_lists_in_scope_work_due_before_the_date(self, scope):
        backend, _, milestone = scope
        near = TaskFactory(
            project=backend,
            status=Task.STATUS_TODO,
            due_date=milestone.target_date - datetime.timedelta(days=2),
        )
        TaskFactory(
            project=backend,
            status=Task.STATUS_TODO,
            due_date=milestone.target_date + datetime.timedelta(days=2),
        )
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO, due_date=milestone.target_date)
        TaskFactory(project=backend, status=Task.STATUS_DONE, due_date=milestone.target_date)

        rows = services.membership_reports(milestone)["near"]

        assert [row["task"].id for row in rows] == [near.id]

    def test_a_blocker_from_outside_is_reported_with_its_chain(self, scope):
        backend, infra, milestone = scope
        inside = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        blocker = TaskFactory(project=infra, status=Task.STATUS_TODO, title="Size the cluster")
        blocker.blocks.add(inside)

        rows = services.membership_reports(milestone)["blocks"]

        assert [row["task"].id for row in rows] == [blocker.id]
        assert rows[0]["chain"] == [blocker.id, inside.id]
        assert rows[0]["in_scope"] is True
        # A blocker in scope and in no milestone gets no reason line: the
        # row already offers an Add button, which says the same thing in
        # the space of nothing.
        assert rows[0]["why"] == ""

    def test_a_blocker_scheduled_after_the_work_it_blocks_says_so(self, scope):
        backend, _, milestone = scope
        later = MilestoneFactory(
            workspace=milestone.workspace,
            name="Cluster cut over",
            target_date=milestone.target_date + datetime.timedelta(days=12),
            projects=[backend],
        )
        inside = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        blocker = TaskFactory(project=backend, milestone=later, status=Task.STATUS_TODO)
        blocker.blocks.add(inside)

        row = services.membership_reports(milestone)["blocks"][0]

        assert "12 days after this date" in str(row["why"])

    def test_a_blocker_outside_the_scope_names_the_project(self, scope):
        backend, _, milestone = scope
        outside = ProjectFactory(workspace=milestone.workspace, slug_prefix="WEB", name="Web")
        inside = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        blocker = TaskFactory(project=outside, status=Task.STATUS_TODO)
        blocker.blocks.add(inside)

        row = services.membership_reports(milestone)["blocks"][0]

        assert row["in_scope"] is False
        assert "Web is outside this milestone's scope" in str(row["why"])

    def test_the_walk_reaches_two_hops_and_stops(self, scope):
        backend, _, milestone = scope
        inside = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        first = TaskFactory(project=backend, status=Task.STATUS_TODO)
        second = TaskFactory(project=backend, status=Task.STATUS_TODO)
        third = TaskFactory(project=backend, status=Task.STATUS_TODO)
        first.blocks.add(inside)
        second.blocks.add(first)
        third.blocks.add(second)

        reported = {row["task"].id for row in services.membership_reports(milestone)["blocks"]}

        assert reported == {first.id, second.id}

    def test_finished_blockers_and_the_milestone_s_own_work_stay_out(self, scope):
        backend, _, milestone = scope
        inside = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        sibling = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        done = TaskFactory(project=backend, status=Task.STATUS_DONE)
        sibling.blocks.add(inside)
        done.blocks.add(inside)

        assert services.membership_reports(milestone)["blocks"] == []


@pytest.mark.django_db
class TestWhereAnEpicsWorkSits:
    """An epic derives its dates from its tasks; it derives this too.

    The row has to carry two different readings and not confuse them:
    this epic's share of a date, and the state of the milestone itself.
    An epic finishing its three tasks does not make the milestone
    complete, and a row that said so would be lying.
    """

    def _epic_with_work(self):
        """An epic whose work sits in two dates and partly in none.

        Returns:
            ``(epic, soon, later, counted)``.
        """
        workspace = WorkspaceFactory(epics_enabled=True)
        project = ProjectFactory(workspace=workspace)
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
        TaskFactory(project=project, epic=epic, milestone=later, status=Task.STATUS_TODO)
        TaskFactory(project=project, epic=epic, milestone=soon, status=Task.STATUS_DONE)
        TaskFactory(
            project=project,
            epic=epic,
            milestone=soon,
            status=Task.STATUS_TODO,
            due_date=soon.target_date + datetime.timedelta(days=3),
        )
        TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        counted = list(Task.objects.filter(epic=epic).select_related("milestone"))
        return epic, soon, later, counted

    def test_rows_run_soonest_first_with_the_unattached_last(self):
        _, soon, later, counted = self._epic_with_work()

        rows = services.epic_milestone_rows(counted)

        assert [row["milestone"] for row in rows] == [soon, later, None]

    def test_a_row_counts_this_epics_share(self):
        _, soon, _, counted = self._epic_with_work()

        row = next(r for r in services.epic_milestone_rows(counted) if r["milestone"] == soon)

        assert (row["done"], row["count"], row["percent"]) == (1, 2, 50)

    def test_a_row_counts_what_runs_past_the_date(self):
        _, soon, later, counted = self._epic_with_work()
        rows = services.epic_milestone_rows(counted)

        assert next(r for r in rows if r["milestone"] == soon)["late"] == 1
        assert next(r for r in rows if r["milestone"] == later)["late"] == 0

    def test_the_state_is_the_milestones_own_not_the_epics_share(self):
        """This epic's whole share of Beta is done; Beta itself is not."""
        workspace = WorkspaceFactory(epics_enabled=True)
        project = ProjectFactory(workspace=workspace)
        soon = MilestoneFactory(
            workspace=workspace,
            name="Beta",
            target_date=timezone.localdate() + datetime.timedelta(days=10),
            projects=[project],
        )
        epic = TaskFactory(project=project, kind=Task.KIND_EPIC, title="Search rework")
        TaskFactory(project=project, epic=epic, milestone=soon, status=Task.STATUS_DONE)
        # Someone else's work, same date, still open.
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_TODO)

        rows = services.epic_milestone_rows(list(Task.objects.filter(epic=epic).select_related("milestone")))
        row = next(r for r in rows if r["milestone"] == soon)

        assert (row["done"], row["count"]) == (1, 1)
        assert row["state"] != "complete"

    def test_the_unattached_row_carries_no_state(self):
        _, _, _, counted = self._epic_with_work()

        row = next(r for r in services.epic_milestone_rows(counted) if r["milestone"] is None)

        assert "state" not in row
        assert row["count"] == 1


class TestTheProjectOverview:
    """The milestone block of a project's Overview tab.

    Two readings of one set, and the tests keep them apart: a card
    reports this project's slice of a date while its state comes from
    the date's whole scope, and the decision list reports the dates
    waiting on a person rather than on work.
    """

    def _board(self):
        """A project aiming at five dates, one of each kind.

        Returns:
            ``(project, other, milestones)`` where ``milestones`` is a
            dict keyed by the kind each one stands for.
        """
        today = timezone.localdate()
        project = ProjectFactory(slug_prefix="BCK")
        other = ProjectFactory(workspace=project.workspace, slug_prefix="INF")
        missed = MilestoneFactory(
            workspace=project.workspace,
            name="Beta",
            target_date=today - datetime.timedelta(days=3),
            projects=[project],
        )
        soon = MilestoneFactory(
            workspace=project.workspace,
            name="RC",
            target_date=today + datetime.timedelta(days=5),
            projects=[
                project,
                other,
            ],
        )
        later = MilestoneFactory(
            workspace=project.workspace,
            name="GA",
            target_date=today + datetime.timedelta(days=30),
            projects=[project],
        )
        empty = MilestoneFactory(
            workspace=project.workspace,
            name="Audit",
            target_date=today + datetime.timedelta(days=40),
            projects=[project],
        )
        closeable = MilestoneFactory(
            workspace=project.workspace,
            name="Docs",
            target_date=today + datetime.timedelta(days=50),
            projects=[project],
        )
        TaskFactory(project=project, milestone=missed, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=soon, status=Task.STATUS_DONE)
        TaskFactory(
            project=project,
            milestone=soon,
            status=Task.STATUS_TODO,
            due_date=soon.target_date + datetime.timedelta(days=2),
        )
        TaskFactory(project=other, milestone=soon, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=later, status=Task.STATUS_TODO)
        TaskFactory(project=project, milestone=closeable, status=Task.STATUS_DONE)
        return (
            project,
            other,
            {
                "missed": missed,
                "soon": soon,
                "later": later,
                "empty": empty,
                "closeable": closeable,
            },
        )

    def test_the_missed_date_comes_first_and_the_finished_one_not_at_all(self):
        """A card is something still to aim at; a finished date is a decision."""
        project, _other, ms = self._board()

        cards = services.project_overview(project)["cards"]

        assert [card["milestone"] for card in cards] == [
            ms["missed"],
            ms["soon"],
            ms["later"],
            ms["empty"],
        ]

    def test_the_card_count_stops_at_the_limit(self):
        project, _other, _ms = self._board()
        today = timezone.localdate()
        for offset in (60, 70, 80):
            MilestoneFactory(
                workspace=project.workspace,
                name=f"Extra {offset}",
                target_date=today + datetime.timedelta(days=offset),
                projects=[project],
            )

        cards = services.project_overview(project)["cards"]

        assert len(cards) == services.OVERVIEW_CARD_LIMIT

    def test_a_card_counts_this_projects_slice_not_the_whole_scope(self):
        """``RC`` holds three tasks; two of them are this project's."""
        project, _other, ms = self._board()

        card = next(c for c in services.project_overview(project)["cards"] if c["milestone"] == ms["soon"])

        assert (card["done"], card["total"], card["open"], card["percent"]) == (1, 2, 1, 50)
        assert card["scope_total"] == 3

    def test_a_shared_date_names_every_project_aiming_at_it(self):
        project, _other, ms = self._board()
        overview = services.project_overview(project)

        shared = next(c for c in overview["cards"] if c["milestone"] == ms["soon"])
        local = next(c for c in overview["cards"] if c["milestone"] == ms["later"])

        assert shared["shared"] is True
        assert shared["scope"] == [
            "BCK",
            "INF",
        ]
        assert local["shared"] is False
        assert overview["shared_count"] == 1

    def test_risk_is_read_off_this_projects_slice(self):
        """The task due after ``RC`` is ours; ``INF``'s open one is not."""
        project, _other, ms = self._board()

        card = next(c for c in services.project_overview(project)["cards"] if c["milestone"] == ms["soon"])

        assert card["risk"] == 1

    def test_a_date_this_project_has_no_work_in_still_shows(self):
        project, _other, ms = self._board()

        card = next(c for c in services.project_overview(project)["cards"] if c["milestone"] == ms["empty"])

        assert (card["total"], card["percent"]) == (0, 0)

    def test_the_decisions_run_closeable_then_missed_then_empty_then_the_rest(self):
        project, _other, ms = self._board()
        TaskFactory(project=project, status=Task.STATUS_TODO)

        decisions = services.project_overview(project)["decisions"]

        assert [row["kind"] for row in decisions] == [
            "closeable",
            "missed",
            "empty",
            "unattached",
        ]
        assert [row["milestone"] for row in decisions[:3]] == [
            ms["closeable"],
            ms["missed"],
            ms["empty"],
        ]
        assert decisions[-1]["milestone"] is None

    def test_the_missed_decision_says_how_late_and_how_much_is_open_here(self):
        project, _other, _ms = self._board()

        row = next(r for r in services.project_overview(project)["decisions"] if r["kind"] == "missed")

        assert row["text"] == "Beta — overdue by 3 days, 1 open here"

    def test_the_remainder_counts_only_this_projects_open_work(self):
        """Done, cancelled, shelved and epics are not open work in no date."""
        project, other, _ms = self._board()
        workspace = project.workspace
        workspace.epics_enabled = True
        workspace.save(update_fields=["epics_enabled"])
        TaskFactory(project=project, status=Task.STATUS_TODO)
        TaskFactory(project=project, status=Task.STATUS_DONE)
        TaskFactory(project=project, status=Task.STATUS_CANCELLED)
        TaskFactory(project=project, status=Task.STATUS_TODO, archived_at=timezone.now())
        TaskFactory(project=project, kind=Task.KIND_EPIC)
        TaskFactory(project=other, status=Task.STATUS_TODO)

        assert services.project_overview(project)["unattached"] == 1

    def test_a_closed_date_leaves_both_readings(self):
        project, _other, ms = self._board()
        ms["later"].closed_at = timezone.now()
        ms["later"].save(update_fields=["closed_at"])

        overview = services.project_overview(project)

        assert ms["later"] not in [card["milestone"] for card in overview["cards"]]
        assert ms["later"] not in [row["milestone"] for row in overview["decisions"]]
        assert overview["open_count"] == 4

    def test_the_query_count_does_not_follow_the_number_of_dates(self):
        """Four queries for the block, however many dates it reads."""
        project, _other, _ms = self._board()
        today = timezone.localdate()
        with CaptureQueriesContext(connection) as small:
            services.project_overview(project)
        for offset in range(60, 90):
            milestone = MilestoneFactory(
                workspace=project.workspace,
                name=f"Extra {offset}",
                target_date=today + datetime.timedelta(days=offset),
                projects=[project],
            )
            TaskFactory(project=project, milestone=milestone, status=Task.STATUS_TODO)

        with CaptureQueriesContext(connection) as large:
            services.project_overview(project)

        assert len(large.captured_queries) == len(small.captured_queries) == 4


class TestNarrowingTheList:
    """The Milestones tab's filters, and the counts on their chips."""

    @pytest.fixture
    def shelf(self):
        """Four dates covering every state the list can filter on."""
        today = timezone.localdate()
        owner = UserFactory()
        backend = ProjectFactory(slug_prefix="FBK")
        infra = ProjectFactory(workspace=backend.workspace, slug_prefix="FIN")
        workspace = backend.workspace

        def date(name, offset, projects, **kwargs):
            return MilestoneFactory(
                workspace=workspace,
                name=name,
                target_date=today + datetime.timedelta(days=offset),
                projects=projects,
                **kwargs,
            )

        upcoming = date("Upcoming", 20, [backend], owner=owner)
        TaskFactory(project=backend, milestone=upcoming, status=Task.STATUS_TODO)
        missed = date("Missed", -5, [backend, infra])
        TaskFactory(project=infra, milestone=missed, status=Task.STATUS_TODO)
        ready = date("Ready", 10, [infra])
        TaskFactory(project=infra, milestone=ready, status=Task.STATUS_DONE)
        shut = date("Shut", 30, [backend], closed_at=timezone.now())
        TaskFactory(project=backend, milestone=shut, status=Task.STATUS_TODO)
        return workspace, backend, infra, owner

    def names(self, rows):
        """The names of the rows, in order."""
        return [row["milestone"].name for row in rows]

    def test_no_filter_keeps_everything(self, shelf):
        workspace, _backend, _infra, _owner = shelf
        rows = services.workspace_rows(workspace)

        assert len(services.filter_rows(rows)) == 4

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            ("open", ["Upcoming"]),
            ("overdue", ["Missed"]),
            ("complete", ["Ready"]),
            ("closed", ["Shut"]),
        ],
    )
    def test_each_state_keeps_its_own(self, shelf, state, expected):
        workspace, _backend, _infra, _owner = shelf
        rows = services.workspace_rows(workspace)

        assert self.names(services.filter_rows(rows, state=state)) == expected

    def test_a_date_due_today_counts_as_open(self, shelf):
        """It has not been missed, and a chip for one day would be noise."""
        workspace, backend, _infra, _owner = shelf
        today = MilestoneFactory(
            workspace=workspace, name="Today", target_date=timezone.localdate(), projects=[backend]
        )
        TaskFactory(project=backend, milestone=today, status=Task.STATUS_TODO)
        rows = services.workspace_rows(workspace)

        assert "Today" in self.names(services.filter_rows(rows, state="open"))

    def test_an_unknown_state_is_ignored_rather_than_emptying_the_page(self, shelf):
        workspace, _backend, _infra, _owner = shelf
        rows = services.workspace_rows(workspace)

        assert len(services.filter_rows(rows, state="nonsense")) == 4

    def test_a_project_keeps_the_dates_that_cover_it(self, shelf):
        workspace, _backend, infra, _owner = shelf
        rows = services.workspace_rows(workspace)

        kept = services.filter_rows(rows, project_id=infra.id)

        assert sorted(self.names(kept)) == ["Missed", "Ready"]

    def test_an_owner_keeps_what_they_answer_for(self, shelf):
        workspace, _backend, _infra, owner = shelf
        rows = services.workspace_rows(workspace)

        assert self.names(services.filter_rows(rows, owner_id=owner.id)) == ["Upcoming"]

    def test_at_risk_keeps_only_what_will_miss(self, shelf):
        workspace, _backend, _infra, _owner = shelf
        rows = services.workspace_rows(workspace)

        assert self.names(services.filter_rows(rows, at_risk=True)) == ["Missed"]

    def test_filters_narrow_together(self, shelf):
        """Missed covers both projects; Upcoming is open but only on FBK."""
        workspace, _backend, infra, _owner = shelf
        rows = services.workspace_rows(workspace)

        assert self.names(services.filter_rows(rows, state="overdue", project_id=infra.id)) == ["Missed"]
        assert services.filter_rows(rows, state="open", project_id=infra.id) == []

    def test_the_chips_count_against_the_whole_list(self, shelf):
        """So the numbers hold still while someone clicks through them."""
        workspace, _backend, infra, owner = shelf
        rows = services.workspace_rows(workspace)

        facets = services.list_facets(rows)
        states = {cell["key"]: cell["count"] for cell in facets["states"]}
        projects = {cell["project"].slug_prefix: cell["count"] for cell in facets["projects"]}

        assert states == {"open": 1, "overdue": 1, "complete": 1, "closed": 1}
        assert projects == {"FBK": 3, "FIN": 2}
        assert [cell["owner"] for cell in facets["owners"]] == [owner]
        assert facets["at_risk"] == 1


class TestCountingInPoints:
    """When the estimates are there, the replay stops counting heads.

    The milestone that reads as unbelievable is the one whose easy work
    went first: ten tasks closed and ten tasks left are not the same
    thirty days. Points say so; counts cannot.
    """

    def _sized(self, backend, milestone, *, closed, open_tasks, closed_size, open_size, source=None):
        """Seed a replayable milestone whose work carries estimates."""
        source = source or Task.SIZE_BY_HUMAN
        joined = timezone.now() - datetime.timedelta(days=30)
        for index in range(closed):
            task = TaskFactory(
                project=backend,
                milestone=milestone,
                status=Task.STATUS_DONE,
                size=closed_size,
                size_source=source,
            )
            milestone_event(task, milestone, joined)
            ago = 24 - index * 24 // max(1, closed - 1)
            status_event(task, Task.STATUS_DONE, timezone.now() - datetime.timedelta(days=ago))
        for _index in range(open_tasks):
            task = TaskFactory(
                project=backend,
                milestone=milestone,
                status=Task.STATUS_TODO,
                size=open_size,
                size_source=source,
            )
            milestone_event(task, milestone, joined)

    def test_a_heavy_remainder_is_not_a_light_one(self, scope):
        """Same task counts, different work — and the dates differ."""
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=90)
        milestone.save()
        self._sized(backend, milestone, closed=12, open_tasks=12, closed_size=1, open_size=13)

        chart = services.burndown(milestone, today=today)

        assert chart["forecast"]["unit"] == "points"
        # Twelve ones closed, twelve thirteens left: counting heads would
        # call that done in a fortnight.
        assert (chart["forecast"]["p50"] - today).days > 60

    def test_the_refusal_still_counts_tasks_not_points(self, scope):
        """Ten points could be one task, and one task is not a rhythm."""
        backend, _, milestone = scope
        today = timezone.localdate()
        self._sized(backend, milestone, closed=3, open_tasks=5, closed_size=13, open_size=13)

        chart = services.burndown(milestone, today=today)

        assert chart["reading"] == "thin"
        assert chart["forecast"]["closed"] == 3

    def test_too_few_estimates_falls_back_to_counting(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=30)
        milestone.save()
        self._sized(backend, milestone, closed=12, open_tasks=6, closed_size=None, open_size=None)

        chart = services.burndown(milestone, today=today)

        assert chart["forecast"]["unit"] == "tasks"
        assert "agent_share" not in chart["forecast"]

    def test_the_page_is_told_how_much_of_it_a_machine_made_up(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=60)
        milestone.save()
        self._sized(
            backend,
            milestone,
            closed=12,
            open_tasks=8,
            closed_size=3,
            open_size=3,
            source=Task.SIZE_BY_AGENT,
        )

        chart = services.burndown(milestone, today=today)

        assert chart["forecast"]["unit"] == "points"
        assert chart["forecast"]["agent_share"] == 100
