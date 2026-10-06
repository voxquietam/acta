"""The ranking My Work opens with.

A list says what is mine; these say which of it comes first, and why.
The order is the product here — so the tests pin the order and the
sentence next to each row, not just the fact that rows came back.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.cycles.tests.factories import CycleFactory
from apps.meetings.tests.factories import MeetingFactory
from apps.milestones.tests.factories import MilestoneFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web import focus
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def desk():
    """A viewer, a colleague and a project they share."""
    workspace = WorkspaceFactory()
    me = UserFactory(username="me")
    them = UserFactory(username="them", first_name="Dana", last_name="Pike")
    for user in (me, them):
        WorkspaceMember.objects.create(user=user, workspace=workspace)
    project = ProjectFactory(workspace=workspace, slug_prefix="FOC")
    return workspace, me, them, project


def mine(project, me, **kwargs):
    """Create an open task assigned to the viewer."""
    kwargs.setdefault("status", Task.STATUS_TODO)
    return TaskFactory(project=project, assignee=me, reporter=me, **kwargs)


class TestWhatComesFirst:
    """The order, and the sentence that justifies it."""

    def test_a_task_with_nothing_pressing_is_not_ranked(self, desk):
        """Not urgent is not a low rank — it is no rank at all."""
        _workspace, me, _them, project = desk
        mine(project, me, title="Someday", due_date=timezone.localdate() + datetime.timedelta(days=30))

        assert focus.do_first(focus.focus_tasks(me)) == []

    def test_overdue_outranks_a_deadline_that_has_not_passed(self, desk):
        _workspace, me, _them, project = desk
        today = timezone.localdate()
        soon = mine(project, me, title="Soon", due_date=today + datetime.timedelta(days=1))
        late = mine(project, me, title="Late", due_date=today - datetime.timedelta(days=1))

        rows = focus.do_first(focus.focus_tasks(me))

        assert [row["task"] for row in rows] == [late, soon]

    def test_being_in_someones_way_outranks_ones_own_deadline(self, desk):
        _workspace, me, them, project = desk
        today = timezone.localdate()
        theirs = TaskFactory(project=project, assignee=them, reporter=them, status=Task.STATUS_TODO)
        blocker = mine(project, me, title="Blocker")
        blocker.blocks.add(theirs)
        tomorrow = mine(project, me, title="Tomorrow", due_date=today + datetime.timedelta(days=1))

        rows = focus.do_first(focus.focus_tasks(me))

        assert [row["task"] for row in rows] == [blocker, tomorrow]
        assert rows[0]["reasons"][0]["text"] == f"blocks Dana Pike · {theirs.slug}"

    def test_a_link_to_ones_own_task_is_not_someone_waiting(self, desk):
        """A person waiting on themselves is not waiting."""
        _workspace, me, _them, project = desk
        other = mine(project, me, title="Also mine")
        blocker = mine(project, me, title="Blocker")
        blocker.blocks.add(other)

        assert focus.do_first(focus.focus_tasks(me)) == []

    def test_a_finished_task_on_the_far_end_stops_counting(self, desk):
        _workspace, me, them, project = desk
        theirs = TaskFactory(project=project, assignee=them, reporter=them, status=Task.STATUS_DONE)
        blocker = mine(project, me, title="Blocker")
        blocker.blocks.add(theirs)

        assert focus.do_first(focus.focus_tasks(me)) == []

    def test_the_overdue_sentence_counts_the_days(self, desk):
        _workspace, me, _them, project = desk
        mine(project, me, due_date=timezone.localdate() - datetime.timedelta(days=3))

        row = focus.do_first(focus.focus_tasks(me))[0]

        assert row["reasons"][0]["text"] == "overdue by 3 days"
        assert row["due_label"] == "3d overdue"
        assert row["due_tone"] == "text-rose-400"

    def test_work_that_runs_past_its_date_says_by_how_much(self, desk):
        workspace, me, _them, project = desk
        today = timezone.localdate()
        milestone = MilestoneFactory(
            workspace=workspace,
            name="Beta",
            target_date=today + datetime.timedelta(days=5),
            projects=[project],
        )
        mine(project, me, milestone=milestone, due_date=today + datetime.timedelta(days=9))

        row = focus.do_first(focus.focus_tasks(me))[0]

        assert [reason["text"] for reason in row["reasons"]] == ["4d past Beta"]

    def test_a_closed_date_cannot_be_run_past(self, desk):
        workspace, me, _them, project = desk
        today = timezone.localdate()
        milestone = MilestoneFactory(
            workspace=workspace,
            target_date=today + datetime.timedelta(days=5),
            projects=[project],
            closed_at=timezone.now(),
        )
        mine(project, me, milestone=milestone, due_date=today + datetime.timedelta(days=9))

        assert focus.do_first(focus.focus_tasks(me)) == []

    def test_every_reason_is_kept_not_only_the_heaviest(self, desk):
        """The row shows the top one and lists the rest underneath."""
        _workspace, me, _them, project = desk
        mine(
            project,
            me,
            due_date=timezone.localdate() - datetime.timedelta(days=2),
            priority=Task.URGENT,
        )

        row = focus.do_first(focus.focus_tasks(me))[0]

        assert [reason["kind"] for reason in row["reasons"]] == [
            "overdue",
            "urgent",
        ]


class TestWhatTheButtonSays:
    """The verb is what the person does next, not what the status is."""

    def test_work_in_someones_way_is_opened_whatever_its_status(self, desk):
        _workspace, me, them, project = desk
        theirs = TaskFactory(project=project, assignee=them, reporter=them, status=Task.STATUS_TODO)
        blocker = mine(project, me, status=Task.STATUS_IN_PROGRESS)
        blocker.blocks.add(theirs)

        assert focus.do_first(focus.focus_tasks(me))[0]["action"] == "Open"

    @pytest.mark.parametrize(
        ("status", "label"),
        [
            (Task.STATUS_IN_REVIEW, "Review"),
            (Task.STATUS_IN_PROGRESS, "Continue"),
            (Task.STATUS_TODO, "Start"),
        ],
    )
    def test_the_status_picks_the_verb(self, desk, status, label):
        _workspace, me, _them, project = desk
        mine(project, me, status=status, due_date=timezone.localdate() - datetime.timedelta(days=1))

        assert focus.do_first(focus.focus_tasks(me))[0]["action"] == label


class TestTheDayInOneLine:
    """Five counts, and none of them shown as a nought."""

    def test_a_count_of_zero_is_left_out(self, desk):
        _workspace, me, _them, project = desk
        mine(project, me, due_date=timezone.localdate() - datetime.timedelta(days=1))

        kinds = [row["kind"] for row in focus.summary(focus.focus_tasks(me))]

        assert kinds == ["overdue"]

    def test_each_kind_counts_its_own_pressure(self, desk):
        _workspace, me, them, project = desk
        today = timezone.localdate()
        mine(project, me, due_date=today - datetime.timedelta(days=1))
        mine(project, me, due_date=today + datetime.timedelta(days=1))
        theirs = TaskFactory(project=project, assignee=them, reporter=them, status=Task.STATUS_TODO)
        waiting = mine(project, me)
        waiting.blocks.add(theirs)
        blocked = mine(project, me)
        blocked.blocked_by.add(theirs)

        rows = {row["kind"]: row["count"] for row in focus.summary(focus.focus_tasks(me))}

        assert rows == {"overdue": 1, "soon": 1, "waiting": 1, "blocked": 1}

    def test_a_date_of_ones_own_making_counts_as_at_risk(self, desk):
        workspace, me, _them, project = desk
        today = timezone.localdate()
        passed = MilestoneFactory(
            workspace=workspace,
            name="Gone",
            target_date=today - datetime.timedelta(days=1),
            projects=[project],
        )
        safe = MilestoneFactory(
            workspace=workspace,
            name="Fine",
            target_date=today + datetime.timedelta(days=20),
            projects=[project],
        )
        mine(project, me, milestone=passed)
        mine(project, me, milestone=safe, due_date=today + datetime.timedelta(days=2))

        at_risk = focus.milestones_at_risk(focus.focus_tasks(me))

        assert [milestone.name for milestone in at_risk] == ["Gone"]


class TestTidyUp:
    """The three ways a list rots quietly."""

    def test_it_counts_what_has_no_date_and_no_priority(self, desk):
        _workspace, me, _them, project = desk
        mine(project, me, priority=Task.NO_PRIORITY)
        mine(project, me, priority=Task.HIGH)

        rows = {row["kind"]: row["count"] for row in focus.tidy_up(focus.focus_tasks(me))}

        assert rows == {"no-milestone": 2, "no-priority": 1}

    def test_work_that_has_sat_still_a_fortnight_shows_up(self, desk):
        _workspace, me, _them, project = desk
        stale = mine(project, me, status=Task.STATUS_IN_PROGRESS)
        Task.objects.filter(pk=stale.pk).update(created_at=timezone.now() - datetime.timedelta(days=20))

        rows = {row["kind"]: row["count"] for row in focus.tidy_up(focus.focus_tasks(me))}

        assert rows["sitting"] == 1

    def test_work_that_is_meant_to_be_waiting_never_counts_as_stale(self, desk):
        """Planned work sitting still is planned work, not rot."""
        _workspace, me, _them, project = desk
        waiting = mine(project, me, status=Task.STATUS_PLANNED)
        Task.objects.filter(pk=waiting.pk).update(created_at=timezone.now() - datetime.timedelta(days=60))

        rows = {row["kind"]: row["count"] for row in focus.tidy_up(focus.focus_tasks(me))}

        assert "sitting" not in rows


class TestPickNext:
    """What is free to start, once the urgent is dealt with."""

    def test_ranked_work_is_not_offered_twice(self, desk):
        """Ranked at all, not merely ranked onto the visible shortlist."""
        _workspace, me, _them, project = desk
        today = timezone.localdate()
        spare = mine(project, me, title="Spare")
        pressing = [mine(project, me, due_date=today - datetime.timedelta(days=i + 1)) for i in range(8)]
        tasks = focus.focus_tasks(me)
        ranked = focus.do_first(tasks)

        rows = focus.pick_next(tasks, ranked)

        assert len(ranked) == len(pressing)
        assert [row["task"] for row in rows] == [spare]

    def test_ready_behind_an_unfinished_blocker_is_not_ready(self, desk):
        """Including a blocker of one's own — the question is can it start."""
        _workspace, me, _them, project = desk
        blocker = mine(project, me, title="First")
        blocked = mine(project, me, title="Second")
        blocked.blocked_by.add(blocker)

        rows = focus.pick_next(focus.focus_tasks(me), [])

        assert [row["task"] for row in rows] == [blocker]

    def test_a_finished_blocker_lets_it_through(self, desk):
        _workspace, me, _them, project = desk
        blocker = mine(project, me, title="First", status=Task.STATUS_DONE)
        blocked = mine(project, me, title="Second")
        blocked.blocked_by.add(blocker)

        assert [row["task"] for row in focus.pick_next(focus.focus_tasks(me), [])] == [blocked]

    def test_priority_leads_and_no_priority_sorts_last(self, desk):
        _workspace, me, _them, project = desk
        none = mine(project, me, title="None", priority=Task.NO_PRIORITY)
        low = mine(project, me, title="Low", priority=Task.LOW)
        high = mine(project, me, title="High", priority=Task.HIGH)

        rows = focus.pick_next(focus.focus_tasks(me), [])

        assert [row["task"] for row in rows] == [high, low, none]

    def test_work_already_moving_is_not_something_to_pick(self, desk):
        _workspace, me, _them, project = desk
        mine(project, me, status=Task.STATUS_IN_PROGRESS)

        assert focus.pick_next(focus.focus_tasks(me), []) == []


class TestCommitments:
    """Two numbers per date, and never one standing in for the other."""

    @pytest.fixture
    def promised(self, desk):
        """A shared date the viewer owes two tasks to, one of them late."""
        workspace, me, them, project = desk
        today = timezone.localdate()
        milestone = MilestoneFactory(
            workspace=workspace,
            name="Beta",
            target_date=today + datetime.timedelta(days=10),
            projects=[project],
        )
        mine(project, me, title="Done bit", milestone=milestone, status=Task.STATUS_DONE)
        mine(project, me, title="On time", milestone=milestone, due_date=today + datetime.timedelta(days=2))
        mine(
            project,
            me,
            title="Spills over",
            milestone=milestone,
            due_date=today + datetime.timedelta(days=20),
        )
        TaskFactory(project=project, assignee=them, reporter=them, milestone=milestone, status=Task.STATUS_TODO)
        return workspace, me, project, milestone

    def test_yours_and_the_teams_are_different_numbers(self, promised):
        _workspace, me, _project, _milestone = promised

        card = focus.commitments(me, focus.focus_tasks(me))["hot"][0]

        assert (card["mine_done"], card["mine_total"]) == (1, 3)
        assert (card["team_done"], card["team_total"]) == (1, 4)

    def test_work_that_runs_past_the_date_makes_it_hot(self, promised):
        _workspace, me, _project, _milestone = promised

        card = focus.commitments(me, focus.focus_tasks(me))["hot"][0]

        assert card["hot"] is True
        assert card["line"]["text"] == "1 of yours ends after this date"

    def test_a_date_ones_own_part_fits_is_steady(self, desk):
        workspace, me, _them, project = desk
        today = timezone.localdate()
        milestone = MilestoneFactory(
            workspace=workspace,
            name="Calm",
            target_date=today + datetime.timedelta(days=10),
            projects=[project],
        )
        mine(project, me, milestone=milestone, due_date=today + datetime.timedelta(days=3))

        reading = focus.commitments(me, focus.focus_tasks(me))

        assert reading["hot"] == []
        assert reading["steady"][0]["line"]["text"] == "your part fits the date"

    def test_a_date_that_has_gone_says_what_is_still_open(self, desk):
        workspace, me, _them, project = desk
        today = timezone.localdate()
        milestone = MilestoneFactory(
            workspace=workspace,
            name="Gone",
            target_date=today - datetime.timedelta(days=2),
            projects=[project],
        )
        mine(project, me, milestone=milestone)
        mine(project, me, milestone=milestone)

        card = focus.commitments(me, focus.focus_tasks(me))["hot"][0]

        assert card["line"]["text"] == "date passed — 2 of yours still open"
        assert card["countdown"] == "overdue by 2 days"

    def test_the_work_is_grouped_by_epic_with_the_rest_last(self, promised):
        workspace, me, project, milestone = promised
        workspace.epics_enabled = True
        workspace.save(update_fields=["epics_enabled"])
        epic = TaskFactory(project=project, kind=Task.KIND_EPIC, title="Search")
        inside = mine(project, me, title="Inside", milestone=milestone, epic=epic)

        card = focus.commitments(me, focus.focus_tasks(me))["hot"][0]
        groups = card["groups"]

        assert [group["epic"] for group in groups] == [epic, None]
        assert [row["task"] for row in groups[0]["rows"]] == [inside]
        assert (groups[0]["team_done"], groups[0]["team_total"]) == (0, 1)

    def test_a_closed_date_is_no_longer_a_commitment(self, desk):
        workspace, me, _them, project = desk
        milestone = MilestoneFactory(
            workspace=workspace,
            target_date=timezone.localdate() - datetime.timedelta(days=5),
            projects=[project],
            closed_at=timezone.now(),
        )
        mine(project, me, milestone=milestone)

        assert focus.commitments(me, focus.focus_tasks(me)) == {"hot": [], "steady": []}


class TestTheFourNumbers:
    """Load, throughput, reliability, commitment."""

    def test_in_progress_is_measured_against_the_workspace_limit(self, desk):
        workspace, me, _them, project = desk
        workspace.wip_limits = {"mode": workspace.WIP_PERSONAL, "limits": {Task.STATUS_IN_PROGRESS: 2}}
        workspace.save(update_fields=["wip_limits"])
        for _ in range(3):
            mine(project, me, status=Task.STATUS_IN_PROGRESS)

        card = focus.kpi(me, workspace, focus.focus_tasks(me))[0]

        assert card["value"] == "3/2"
        assert card["tone"] == "text-rose-400"
        assert card["sub"] == "1 over — finish one before starting another"

    def test_under_the_limit_it_says_how_much_room_is_left(self, desk):
        workspace, me, _them, project = desk
        mine(project, me, status=Task.STATUS_IN_PROGRESS)

        card = focus.kpi(me, workspace, focus.focus_tasks(me))[0]

        assert (card["value"], card["sub"]) == ("1/3", "room for 2 more")

    def test_on_time_reads_the_day_the_work_was_closed(self, desk):
        """Not ``updated_at`` — an edit afterwards must not move it."""
        workspace, me, _them, project = desk
        today = timezone.localdate()
        late = mine(project, me, status=Task.STATUS_DONE, due_date=today - datetime.timedelta(days=4))
        punctual = mine(project, me, status=Task.STATUS_DONE, due_date=today)
        for task in (late, punctual):
            ActivityLog.objects.create(
                workspace=workspace,
                actor=me,
                event_type="task.status_changed",
                target_type=ActivityLog.TARGET_TASK,
                target_id=task.pk,
            )

        cards = {card["label"]: card for card in focus.kpi(me, workspace, focus.focus_tasks(me))}

        assert cards["Closed, 7 days"]["value"] == "2"
        assert cards["On time"]["value"] == "50%"
        assert cards["On time"]["sub"] == "1 of 2 closed"

    def test_nothing_closed_is_nothing_to_judge(self, desk):
        workspace, me, _them, project = desk
        mine(project, me)

        card = next(c for c in focus.kpi(me, workspace, focus.focus_tasks(me)) if c["label"] == "On time")

        assert card["value"] == "—"
        assert card["tone"] == ""

    def test_a_cycle_with_none_of_your_work_does_not_read_as_finished(self, desk):
        workspace, me, _them, _project = desk
        today = timezone.localdate()
        CycleFactory(
            workspace=workspace,
            start_date=today - datetime.timedelta(days=2),
            end_date=today + datetime.timedelta(days=5),
        )

        card = focus.kpi(me, workspace, focus.focus_tasks(me))[-1]

        assert card["value"] == "0/0"
        assert "nothing of yours in it" in card["sub"]


class TestAroundYou:
    """People, days and calls — the things that act on the work."""

    def test_a_person_is_listed_once_with_both_directions(self, desk):
        _workspace, me, them, project = desk
        theirs = TaskFactory(project=project, assignee=them, reporter=them, status=Task.STATUS_TODO)
        blocker = mine(project, me, title="Mine blocks theirs")
        blocker.blocks.add(theirs)
        waiting = mine(project, me, title="Mine waits on theirs")
        waiting.blocked_by.add(theirs)

        rows = focus.people(me, None, focus.focus_tasks(me))

        assert [row["person"] for row in rows] == [them]
        assert [chip["text"] for chip in rows[0]["chips"]] == ["you block 1", "blocks you 1"]

    def test_someone_with_nothing_pending_is_not_a_row(self, desk):
        _workspace, me, _them, project = desk
        mine(project, me)

        assert focus.people(me, None, focus.focus_tasks(me)) == []

    def test_holding_someone_up_outweighs_being_held_up(self, desk):
        workspace, me, them, project = desk
        third = UserFactory(username="third", first_name="Ada", last_name="Byrne")
        WorkspaceMember.objects.create(user=third, workspace=workspace)
        theirs = TaskFactory(project=project, assignee=them, reporter=them, status=Task.STATUS_TODO)
        blocker = mine(project, me)
        blocker.blocks.add(theirs)
        thirds = TaskFactory(project=project, assignee=third, reporter=third, status=Task.STATUS_TODO)
        waiting = mine(project, me)
        waiting.blocked_by.add(thirds)

        rows = focus.people(me, None, focus.focus_tasks(me))

        assert [row["person"] for row in rows] == [them, third]

    def test_the_week_skips_the_weekend(self, desk):
        _workspace, me, _them, _project = desk

        days = focus.week([], datetime.date(2026, 10, 9))

        assert [day["date"].isoformat() for day in days] == [
            "2026-10-09",
            "2026-10-12",
            "2026-10-13",
            "2026-10-14",
            "2026-10-15",
        ]
        assert days[0]["label"] == "Today"

    def test_a_day_carries_what_is_due_on_it(self, desk):
        _workspace, me, _them, project = desk
        today = timezone.localdate()
        task = mine(project, me, due_date=today)

        days = focus.week(focus.focus_tasks(me), today)

        assert days[0]["tasks"] == [task]
        assert days[0]["is_today"] is True

    def test_a_call_named_after_a_date_says_what_is_still_open(self, desk):
        workspace, me, _them, project = desk
        today = timezone.localdate()
        milestone = MilestoneFactory(
            workspace=workspace,
            name="Beta",
            target_date=today + datetime.timedelta(days=4),
            projects=[project],
        )
        mine(project, me, milestone=milestone, due_date=today + datetime.timedelta(days=9))
        meeting = MeetingFactory(project=project, title="Beta readiness", happened_at=timezone.now())

        rows = focus.calls([meeting], focus.focus_tasks(me), today)

        assert rows[0]["label"] == "Today"
        assert rows[0]["prep"]["text"] == "Beta · 1 of yours open, 1 late"
        assert rows[0]["prep"]["tone"] == "text-rose-400"

    def test_a_call_beyond_the_window_is_not_listed(self, desk):
        _workspace, me, _them, _project = desk
        far = MeetingFactory(
            project=_project,
            title="Later",
            happened_at=timezone.now() + datetime.timedelta(days=5),
        )

        assert focus.calls([far], focus.focus_tasks(me)) == []


class TestTheCost:
    """One read of the viewer's work answers every block."""

    def test_the_query_count_does_not_follow_the_task_count(self, desk):
        _workspace, me, them, project = desk
        today = timezone.localdate()
        milestone = MilestoneFactory(
            workspace=_workspace,
            target_date=today + datetime.timedelta(days=5),
            projects=[project],
        )
        theirs = TaskFactory(project=project, assignee=them, reporter=them, status=Task.STATUS_TODO)
        first = mine(project, me, due_date=today - datetime.timedelta(days=1), milestone=milestone)
        first.blocks.add(theirs)
        with CaptureQueriesContext(connection) as few:
            _read(me)
        for offset in range(40):
            task = mine(project, me, due_date=today - datetime.timedelta(days=offset + 1), milestone=milestone)
            task.blocks.add(theirs)

        with CaptureQueriesContext(connection) as many:
            _read(me)

        # Three for the load, two more for the commitment cards: the
        # team's numbers and the viewer's own, each one grouped query
        # over every date in view rather than one per date.
        assert len(many.captured_queries) == len(few.captured_queries) == 5


def _read(user):
    """Run every focus reading off one load, the way the view does."""
    tasks = focus.focus_tasks(user)
    ranked = focus.do_first(tasks)
    focus.summary(tasks)
    focus.tidy_up(tasks)
    focus.pick_next(tasks, ranked)
    focus.commitments(user, tasks)
