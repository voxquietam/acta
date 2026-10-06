"""Group-by helpers for the List view (:mod:`apps.web.grouping`)."""

import datetime

from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web.grouping import group_tasks
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def project(db):
    ws = WorkspaceFactory()
    return ProjectFactory(workspace=ws)


@pytest.mark.django_db
class TestGroupByDeadline:
    """Deadline axis buckets by due_date and ``done`` recency."""

    def test_buckets_distribute_correctly(self, project):
        user = UserFactory()
        today = timezone.localdate()
        tasks = [
            TaskFactory(
                project=project,
                reporter=user,
                title="ovd",
                status=Task.STATUS_TODO,
                due_date=today - datetime.timedelta(days=2),
            ),
            TaskFactory(project=project, reporter=user, title="tdy", status=Task.STATUS_TODO, due_date=today),
            TaskFactory(
                project=project,
                reporter=user,
                title="wk",
                status=Task.STATUS_TODO,
                due_date=today + datetime.timedelta(days=3),
            ),
            TaskFactory(
                project=project,
                reporter=user,
                title="lt",
                status=Task.STATUS_TODO,
                due_date=today + datetime.timedelta(days=30),
            ),
            TaskFactory(project=project, reporter=user, title="nd", status=Task.STATUS_TODO, due_date=None),
        ]
        sections = {s["key"]: [t.title for t in s["tasks"]] for s in group_tasks(tasks, "deadline")}
        assert sections["overdue"] == ["ovd"]
        assert sections["today"] == ["tdy"]
        assert sections["week"] == ["wk"]
        assert sections["later"] == ["lt"]
        assert sections["no_deadline"] == ["nd"]

    def test_recently_done_window(self, project):
        user = UserFactory()
        now = timezone.now()
        recent = TaskFactory(project=project, reporter=user, title="recent", status=Task.STATUS_DONE)
        old = TaskFactory(project=project, reporter=user, title="old", status=Task.STATUS_DONE)
        Task.objects.filter(pk=old.pk).update(updated_at=now - datetime.timedelta(days=10))
        Task.objects.filter(pk=recent.pk).update(updated_at=now - datetime.timedelta(days=1))
        tasks = list(Task.objects.filter(pk__in=[recent.pk, old.pk]))
        sections = {s["key"]: [t.title for t in s["tasks"]] for s in group_tasks(tasks, "deadline")}
        # Recent done shows; old done falls outside the 7d window and is dropped.
        assert "recent" in sections.get("recently_done", [])
        assert "old" not in sections.get("recently_done", [])

    def test_keep_empty_preserves_named_buckets(self, project):
        sections = group_tasks([], "deadline", keep_empty={"recently_done"})
        keys = [s["key"] for s in sections]
        assert "recently_done" in keys
        # Other empty buckets are dropped.
        assert "overdue" not in keys


@pytest.mark.django_db
class TestGroupByStatus:
    def test_sections_in_workflow_order(self, project):
        user = UserFactory()
        TaskFactory(project=project, reporter=user, title="t-todo", status=Task.STATUS_TODO)
        TaskFactory(project=project, reporter=user, title="t-done", status=Task.STATUS_DONE)
        sections = group_tasks(list(Task.objects.all()), "status")
        keys = [s["key"] for s in sections]
        # Planned and in-progress / in-review are empty and dropped.
        assert keys == [Task.STATUS_TODO, Task.STATUS_DONE]


@pytest.mark.django_db
class TestGroupByPriority:
    def test_urgent_before_low_and_noprio_last(self, project):
        user = UserFactory()
        TaskFactory(
            project=project, reporter=user, title="t-noprio", priority=Task.NO_PRIORITY, status=Task.STATUS_TODO
        )
        TaskFactory(project=project, reporter=user, title="t-urgent", priority=Task.URGENT, status=Task.STATUS_TODO)
        TaskFactory(project=project, reporter=user, title="t-low", priority=Task.LOW, status=Task.STATUS_TODO)
        sections = group_tasks(list(Task.objects.all()), "priority")
        # Order: urgent, low, no-priority. (High/medium empty → dropped.)
        keys = [s["key"] for s in sections]
        assert keys == [str(Task.URGENT), str(Task.LOW), str(Task.NO_PRIORITY)]


@pytest.mark.django_db
class TestGroupByAssignee:
    def test_unassigned_last(self, project):
        user = UserFactory()
        alice = UserFactory(username="alice", first_name="Alice")
        bob = UserFactory(username="bob", first_name="Bob")
        TaskFactory(project=project, reporter=user, title="t-bob", assignee=bob, status=Task.STATUS_TODO)
        TaskFactory(project=project, reporter=user, title="t-alice", assignee=alice, status=Task.STATUS_TODO)
        TaskFactory(project=project, reporter=user, title="t-none", assignee=None, status=Task.STATUS_TODO)
        sections = group_tasks(list(Task.objects.all()), "assignee", request_user=user)
        labels = [s["label"] for s in sections]
        # Alice before Bob alphabetically; Unassigned always last.
        assert labels[-1] == "Unassigned"
        assert labels.index("Alice") < labels.index("Bob")


@pytest.mark.django_db
class TestGroupByProject:
    def test_alphabetical_by_project_name(self, db):
        ws = WorkspaceFactory()
        p_a = ProjectFactory(workspace=ws, name="Aardvark")
        p_b = ProjectFactory(workspace=ws, name="Beaver")
        user = UserFactory()
        TaskFactory(project=p_b, reporter=user, title="t-b", status=Task.STATUS_TODO)
        TaskFactory(project=p_a, reporter=user, title="t-a", status=Task.STATUS_TODO)
        sections = group_tasks(list(Task.objects.all()), "project")
        names = [s["label"] for s in sections]
        assert names == ["Aardvark", "Beaver"]


@pytest.mark.django_db
class TestGroupByMilestone:
    """A milestone is a date, so the sections run in date order."""

    def _milestone(self, project, name, days):
        from apps.milestones.tests.factories import MilestoneFactory

        return MilestoneFactory(
            workspace=project.workspace,
            projects=[project],
            name=name,
            target_date=timezone.localdate() + datetime.timedelta(days=days),
        )

    def test_soonest_first_and_unassigned_last(self, project):
        later = self._milestone(project, "Later", 30)
        sooner = self._milestone(project, "Sooner", 3)
        TaskFactory(project=project, milestone=later, title="far")
        TaskFactory(project=project, milestone=sooner, title="near")
        TaskFactory(project=project, title="loose")

        sections = group_tasks(Task.objects.all(), "milestone", request_user=None)

        assert [s["label"] for s in sections] == ["Sooner", "Later", "No milestone"]

    def test_overdue_milestones_lead(self, project):
        """Date order puts a missed date at the top, where attention goes."""
        past = self._milestone(project, "Missed", -5)
        ahead = self._milestone(project, "Ahead", 10)
        TaskFactory(project=project, milestone=ahead)
        TaskFactory(project=project, milestone=past)

        sections = group_tasks(Task.objects.all(), "milestone", request_user=None)

        assert sections[0]["label"] == "Missed"


@pytest.mark.django_db
def test_every_axis_the_picker_offers_can_be_grouped():
    """The picker and the grouping must never drift apart.

    They did once: My Work built its sections from a hand-written list
    of four axes while the picker was built from ``_optional_axes``, so
    choosing "Group by: Epic" rendered an empty page. This asserts the
    two agree for every optional axis a workspace can switch on.
    """
    from apps.milestones.tests.factories import MilestoneFactory
    from apps.web.views import _optional_axes

    ws = WorkspaceFactory(epics_enabled=True)
    project = ProjectFactory(workspace=ws)
    MilestoneFactory(workspace=ws, projects=[project])
    user = UserFactory()
    TaskFactory(project=project, assignee=user)

    keys = _optional_axes(("deadline", "status", "priority", "assignee", "project"), ws)

    assert "milestone" in keys
    for key in keys:
        sections = group_tasks(Task.objects.all(), key, request_user=user)
        assert sections, f"axis {key!r} is offered by the picker but groups into nothing"


@pytest.mark.django_db
def test_a_milestone_section_says_its_date_and_what_runs_past_it():
    """A heading that reads "Beta · 12" hides the one fact worth acting on.

    The section is named after a date, so it carries that date and the
    count of work already running past it — the same rule the plan and
    the milestone pages count risk by (ADR 0037).
    """
    from apps.milestones.tests.factories import MilestoneFactory

    workspace = WorkspaceFactory()
    project = ProjectFactory(workspace=workspace)
    target = timezone.localdate() + datetime.timedelta(days=10)
    milestone = MilestoneFactory(workspace=workspace, name="Beta", target_date=target, projects=[project])
    TaskFactory(
        project=project,
        milestone=milestone,
        due_date=target + datetime.timedelta(days=4),
        status=Task.STATUS_TODO,
    )
    TaskFactory(
        project=project,
        milestone=milestone,
        due_date=target - datetime.timedelta(days=1),
        status=Task.STATUS_TODO,
    )
    # Done work is not at risk, however late its deadline sits.
    TaskFactory(
        project=project,
        milestone=milestone,
        due_date=target + datetime.timedelta(days=9),
        status=Task.STATUS_DONE,
    )

    sections = group_tasks(
        Task.objects.filter(project=project).select_related("milestone"),
        "milestone",
    )
    section = next(s for s in sections if s["label"] == "Beta")

    assert section["risk"] == 1
    assert section["sub"]
