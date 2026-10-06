"""The readings the milestone pages are built from.

Progress, state, the slices, risk and the burndown are all derived — the
model stores a date, a scope and the fact that someone closed it. These
tests pin the derivations, because the pages and the MCP tools both read
them and a drift between the two is exactly what ADR 0037 set out to
prevent.
"""

import datetime

from django.utils import timezone

import pytest

from apps.activity.models import ActivityLog
from apps.milestones import services
from apps.milestones.models import Milestone
from apps.milestones.tests.factories import MilestoneFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory

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

    def test_the_verdict_names_the_day_the_pace_lands_on(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        milestone.target_date = today + datetime.timedelta(days=1)
        milestone.save()
        joined = timezone.now() - datetime.timedelta(days=10)
        for _index in range(6):
            task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
            milestone_event(task, milestone, joined)
        finished = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        milestone_event(finished, milestone, joined)
        status_event(finished, Task.STATUS_DONE, timezone.now() - datetime.timedelta(days=1))

        chart = services.burndown(milestone, today=today)

        assert chart["behind"] is True
        assert chart["slip"] > 0
        assert "Behind" in chart["verdict"]

    def test_everything_done_says_so(self, scope):
        backend, _, milestone = scope
        today = timezone.localdate()
        task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        milestone_event(task, milestone, timezone.now() - datetime.timedelta(days=2))
        status_event(task, Task.STATUS_DONE, timezone.now() - datetime.timedelta(days=1))

        chart = services.burndown(milestone, today=today)

        assert chart["open"] == 0
        assert chart["verdict"] == "all done"
        assert chart["behind"] is False
