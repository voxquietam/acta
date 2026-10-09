"""Milestones through MCP: scope, membership, and closing with work open.

A milestone is a point with a scope (docs/decisions/0037-milestones.md),
and both halves are easy for a caller that cannot see the UI to get
wrong. The cases below pin the ones where a loose contract would let it
believe something happened that did not: a task joining a milestone that
does not cover its project, an epic joining at all, a scope narrowed
without saying what it dropped, and a delete going through unasked.
"""

import datetime

from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.mcp.tools import CALLABLES, reject_unknown_arguments
from apps.milestones.models import Milestone
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """A member, two projects in one workspace, and a milestone over one."""
    user = UserFactory()
    ws = WorkspaceFactory()
    WorkspaceMember.objects.create(user=user, workspace=ws)
    backend = ProjectFactory(workspace=ws, slug_prefix="BCK")
    web = ProjectFactory(workspace=ws, slug_prefix="WEB")
    milestone = Milestone.objects.create(
        workspace=ws,
        name="Search GA",
        target_date=timezone.localdate() + datetime.timedelta(days=30),
    )
    milestone.projects.set([backend])
    return user, ws, backend, web, milestone


@pytest.mark.django_db
class TestCommittingAtCreation:
    """A task can arrive already committed, in one call.

    Membership used to need a second tool, and a second tool is a second
    chance to forget: the agent that files the task is the one that knows
    which effort it belongs to.
    """

    def test_a_task_can_name_its_milestone_as_it_is_created(self, setup):
        user, _ws, backend, _web, milestone = setup

        result = CALLABLES["acta_task_create"](
            user,
            {
                "project": backend.slug_prefix,
                "title": "Ship the ranking eval",
                "milestone_id": milestone.id,
            },
        )

        assert result["milestone_id"] == milestone.id
        assert result["milestone_name"] == milestone.name
        assert Task.objects.get(title="Ship the ranking eval").milestone_id == milestone.id

    def test_joining_is_logged_the_way_every_other_surface_logs_it(self, setup):
        """The burndown is replayed from these events, so a membership
        that arrives without one is a task the chart cannot see."""
        from apps.activity.models import ActivityLog

        user, _ws, backend, _web, milestone = setup

        result = CALLABLES["acta_task_create"](
            user,
            {"project": backend.slug_prefix, "title": "Logged join", "milestone_id": milestone.id},
        )
        task = Task.objects.get(title="Logged join")
        kinds = set(
            ActivityLog.objects.filter(
                target_type=ActivityLog.TARGET_TASK,
                target_id=task.id,
            ).values_list("event_type", flat=True),
        )

        assert result["slug"] == task.slug
        assert {"task.created", "task.milestone_changed"} <= kinds

    def test_a_milestone_that_does_not_cover_the_project_creates_nothing(self, setup):
        """Not "creates it unattached" — the whole call fails.

        Leaving the task behind would hand back a half-done instruction
        that reads, from the agent's side, exactly like success.
        """
        user, _ws, _backend, web, milestone = setup
        before = Task.objects.count()

        with pytest.raises(Exception):
            CALLABLES["acta_task_create"](
                user,
                {"project": web.slug_prefix, "title": "Out of scope", "milestone_id": milestone.id},
            )

        assert Task.objects.count() == before

    def test_an_unreachable_milestone_fails_before_anything_is_written(self, setup):
        user, _ws, backend, _web, _milestone = setup
        before = Task.objects.count()

        with pytest.raises(Exception):
            CALLABLES["acta_task_create"](
                user,
                {"project": backend.slug_prefix, "title": "Bad id", "milestone_id": 10_000_000},
            )

        assert Task.objects.count() == before

    def test_a_batch_can_commit_as_it_creates(self, setup):
        user, _ws, backend, _web, milestone = setup

        result = CALLABLES["acta_tasks_bulk_create"](
            user,
            {
                "tasks": [
                    {"project": backend.slug_prefix, "title": "Batch one", "milestone_id": milestone.id},
                    {"project": backend.slug_prefix, "title": "Batch two", "milestone_id": milestone.id},
                ],
            },
        )

        assert result["count"] == 2
        assert {row["milestone_id"] for row in result["created"]} == {milestone.id}

    def test_a_task_with_no_milestone_says_so_rather_than_omitting_it(self, setup):
        user, _ws, backend, _web, _milestone = setup

        result = CALLABLES["acta_task_create"](user, {"project": backend.slug_prefix, "title": "Loose"})

        assert result["milestone_id"] is None
        assert result["milestone_name"] is None


@pytest.mark.django_db
class TestReading:
    def test_list_reports_progress_and_risk(self, setup):
        """A row carries the counts and how much will miss the date."""
        user, _, backend, _, milestone = setup
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)
        TaskFactory(
            project=backend,
            milestone=milestone,
            status=Task.STATUS_TODO,
            due_date=milestone.target_date + datetime.timedelta(days=5),
        )

        rows = CALLABLES["acta_milestones_list"](user, {})

        assert len(rows) == 1
        assert (rows[0]["done"], rows[0]["total"]) == (1, 2)
        assert rows[0]["at_risk"] == 1
        assert rows[0]["projects"] == ["BCK"]

    def test_get_explains_why_each_task_is_at_risk(self, setup):
        """Two different reasons, and the payload names them."""
        user, _, backend, _, milestone = setup
        TaskFactory(
            project=backend,
            milestone=milestone,
            status=Task.STATUS_TODO,
            due_date=milestone.target_date + datetime.timedelta(days=3),
        )

        payload = CALLABLES["acta_milestone_get"](user, {"milestone_id": milestone.id})

        assert payload["at_risk_tasks"][0]["days_over"] == 3
        assert payload["at_risk_tasks"][0]["why"] == "due after the milestone"

    def test_a_passed_date_puts_every_open_task_at_risk(self, setup):
        """The half people forget: overdue is risk regardless of due dates."""
        user, _, backend, _, milestone = setup
        milestone.target_date = timezone.localdate() - datetime.timedelta(days=2)
        milestone.save()
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO, due_date=None)

        payload = CALLABLES["acta_milestone_get"](user, {"milestone_id": milestone.id})

        assert payload["state"] == Milestone.STATE_OVERDUE
        assert payload["at_risk_tasks"][0]["why"] == "still open"


@pytest.mark.django_db
class TestWriting:
    def test_create_sets_the_scope(self, setup):
        """Two projects aiming at one date is the shared-commitment case."""
        user, _, _, _, _ = setup

        payload = CALLABLES["acta_milestone_create"](
            user,
            {
                "name": "Public API",
                "target_date": "2027-01-15",
                "projects": ["BCK", "WEB"],
                "goal": "Tokens and rate limits are live.",
            },
        )

        assert payload["projects"] == ["BCK", "WEB"]
        assert payload["goal"] == "Tokens and rate limits are live."

    def test_narrowing_the_scope_detaches_the_work_it_drops(self, setup):
        """Destructive, so the reply has to say how much it dropped."""
        user, _, backend, web, milestone = setup
        milestone.projects.add(web)
        kept = TaskFactory(project=backend, milestone=milestone)
        dropped = TaskFactory(project=web, milestone=milestone)

        payload = CALLABLES["acta_milestone_update"](
            user,
            {"milestone_id": milestone.id, "projects": ["BCK"]},
        )

        assert payload["detached_tasks"] == 1
        dropped.refresh_from_db()
        kept.refresh_from_db()
        assert dropped.milestone_id is None
        assert kept.milestone_id == milestone.id

    def test_a_task_cannot_join_a_milestone_that_misses_its_project(self, setup):
        """Scope is the whole reason a milestone can be shared."""
        user, _, _, web, milestone = setup
        outsider = TaskFactory(project=web)

        with pytest.raises(Exception):  # noqa: B017 — DRF ValidationError
            CALLABLES["acta_tasks_set_milestone"](
                user,
                {"tasks": [outsider.slug], "milestone_id": milestone.id},
            )

        outsider.refresh_from_db()
        assert outsider.milestone_id is None

    def test_an_epic_cannot_join(self, setup):
        """An epic derives its milestones from its tasks."""
        user, _, backend, _, milestone = setup
        epic = TaskFactory(project=backend, kind=Task.KIND_EPIC)

        with pytest.raises(Exception):  # noqa: B017 — DRF ValidationError
            CALLABLES["acta_tasks_set_milestone"](
                user,
                {"tasks": [epic.slug], "milestone_id": milestone.id},
            )

        epic.refresh_from_db()
        assert epic.milestone_id is None

    def test_close_moves_the_open_work_when_asked(self, setup):
        """Closing with work open is where the feature is careful or sloppy."""
        user, ws, backend, _, milestone = setup
        nxt = Milestone.objects.create(
            workspace=ws,
            name="Next",
            target_date=timezone.localdate() + datetime.timedelta(days=60),
        )
        nxt.projects.set([backend])
        open_task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)
        TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_DONE)

        payload = CALLABLES["acta_milestone_close"](
            user,
            {"milestone_id": milestone.id, "move_open_to": nxt.id},
        )

        open_task.refresh_from_db()
        assert open_task.milestone_id == nxt.id
        assert payload["open_work_handled"] == 1
        assert payload["state"] == Milestone.STATE_CLOSED

    def test_close_leaves_open_work_alone_by_default(self, setup):
        """No instruction means no surprise move; the reply says how many."""
        user, _, backend, _, milestone = setup
        open_task = TaskFactory(project=backend, milestone=milestone, status=Task.STATUS_TODO)

        payload = CALLABLES["acta_milestone_close"](user, {"milestone_id": milestone.id})

        open_task.refresh_from_db()
        assert open_task.milestone_id == milestone.id
        assert payload["open_work_left_attached"] == 1

    def test_delete_needs_confirmation_and_keeps_the_tasks(self, setup):
        """A destructive call answers with what it would do, then does it."""
        user, _, backend, _, milestone = setup
        task = TaskFactory(project=backend, milestone=milestone)

        with pytest.raises(ValueError, match="confirm=true"):
            CALLABLES["acta_milestone_delete"](user, {"milestone_id": milestone.id})

        payload = CALLABLES["acta_milestone_delete"](
            user,
            {"milestone_id": milestone.id, "confirm": True},
        )

        task.refresh_from_db()
        assert payload["detached_tasks"] == 1
        assert task.milestone_id is None
        assert task.pk is not None


@pytest.mark.django_db
def test_every_milestone_tool_closes_its_schema():
    """An unknown argument must be refused, not ignored into a false success."""
    for name in (
        "acta_milestones_list",
        "acta_milestone_get",
        "acta_milestone_create",
        "acta_milestone_update",
        "acta_milestone_close",
        "acta_milestone_reopen",
        "acta_milestone_delete",
        "acta_tasks_set_milestone",
    ):
        with pytest.raises(ValueError, match="does not take"):
            reject_unknown_arguments(name, {"nonsense": 1})
