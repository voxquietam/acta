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


class TestTheCost:
    """One read of the viewer's work answers every block."""

    def test_the_query_count_does_not_follow_the_task_count(self, desk):
        _workspace, me, them, project = desk
        today = timezone.localdate()
        theirs = TaskFactory(project=project, assignee=them, reporter=them, status=Task.STATUS_TODO)
        first = mine(project, me, due_date=today - datetime.timedelta(days=1))
        first.blocks.add(theirs)
        with CaptureQueriesContext(connection) as few:
            _read(me)
        for offset in range(40):
            task = mine(project, me, due_date=today - datetime.timedelta(days=offset + 1))
            task.blocks.add(theirs)

        with CaptureQueriesContext(connection) as many:
            _read(me)

        assert len(many.captured_queries) == len(few.captured_queries) == 3


def _read(user):
    """Run every focus reading off one load, the way the view does."""
    tasks = focus.focus_tasks(user)
    focus.do_first(tasks)
    focus.summary(tasks)
    focus.tidy_up(tasks)
