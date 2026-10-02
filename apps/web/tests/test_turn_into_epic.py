"""Turning a task that outgrew itself into an epic.

The endpoint asks before it acts because three things change at once:
the subtasks become the epic's first tasks, the fields an epic derives
from its tasks are dropped, and the task leaves the board for the Epics
tab. The tests below pin each of the three, and the cases where the
change is refused instead. See docs/decisions/0036-epics.md.
"""

import pytest

from apps.activity.models import ActivityLog
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """A member, a project, and a task with two subtasks."""
    workspace = WorkspaceFactory()
    user = workspace.owner
    WorkspaceMember.objects.get_or_create(user=user, workspace=workspace)
    project = ProjectFactory(workspace=workspace, slug_prefix="TIE")
    task = TaskFactory(project=project, reporter=user, title="Billing migration")
    children = [TaskFactory(project=project, parent=task, reporter=user) for _ in range(2)]
    return workspace, project, user, task, children


def url(task):
    """Return the turn-into-epic endpoint for ``task``."""
    return f"/projects/{task.project.slug_prefix}/{task.number}/turn-into-epic/"


@pytest.mark.django_db
class TestTheConfirmation:
    """GET names what is kept, what moves and what is dropped."""

    def test_it_lists_the_subtasks_that_become_tasks(self, client, setup):
        _, _, user, task, children = setup
        client.force_login(user)
        resp = client.get(url(task))
        assert resp.status_code == 200
        assert [t.pk for t in resp.context["subtasks"]] == [c.pk for c in children]
        assert not resp.context["blockers"]

    def test_it_counts_the_comments_and_files_it_keeps(self, client, setup):
        from apps.comments.tests.factories import CommentFactory

        _, _, user, task, _ = setup
        CommentFactory(task=task, author=user)
        client.force_login(user)
        resp = client.get(url(task))
        assert resp.context["comment_count"] == 1

    def test_the_confirm_button_carries_a_csrf_token(self, client, setup):
        """The POST is a form submit, not an ``hx-post`` on a bare button.

        Django's test client does not enforce CSRF, so nothing else here
        would notice the token going missing — and the browser answers a
        tokenless POST with a 403.
        """
        _, _, user, task, _ = setup
        client.force_login(user)
        body = client.get(url(task)).content.decode()
        assert "csrfmiddlewaretoken" in body

    def test_an_epic_cannot_become_one_again(self, client, setup):
        _, project, user, _, _ = setup
        epic = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, reporter=user)
        client.force_login(user)
        resp = client.get(url(epic))
        assert resp.status_code == 200
        assert resp.context["blockers"]

    def test_a_subtask_has_to_be_promoted_first(self, client, setup):
        _, _, user, _, children = setup
        client.force_login(user)
        resp = client.get(url(children[0]))
        assert resp.context["blockers"]

    def test_a_workspace_with_epics_off_says_so(self, client, setup):
        workspace, _, user, task, _ = setup
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        client.force_login(user)
        resp = client.get(url(task))
        assert resp.context["blockers"]


@pytest.mark.django_db
class TestTheChange:
    """POST converts the task and re-parents the work it spawned."""

    def test_it_becomes_an_epic_and_redirects_to_its_page(self, client, setup):
        _, _, user, task, _ = setup
        client.force_login(user)
        resp = client.post(url(task))
        assert resp.status_code == 204
        assert resp["HX-Redirect"].endswith(f"/{task.number}/")
        task.refresh_from_db()
        assert task.kind == Task.KIND_EPIC
        assert task.status == Task.STATUS_PLANNED

    def test_the_derived_fields_are_dropped(self, client, setup):
        import datetime

        from apps.cycles.tests.factories import CycleFactory

        workspace, project, user, task, _ = setup
        task.due_date = datetime.date(2026, 7, 1)
        task.size = 5
        task.cycle = CycleFactory(workspace=workspace)
        task.save(update_fields=["due_date", "size", "cycle"])
        client.force_login(user)
        client.post(url(task))
        task.refresh_from_db()
        assert (task.due_date, task.size, task.cycle_id) == (None, None, None)

    def test_it_leaves_the_epic_it_belonged_to(self, client, setup):
        _, project, user, task, _ = setup
        host = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, reporter=user)
        Task.objects.filter(pk=task.pk).update(epic=host)
        client.force_login(user)
        client.post(url(task))
        task.refresh_from_db()
        assert task.epic_id is None

    def test_the_subtasks_become_its_first_tasks(self, client, setup):
        _, _, user, task, children = setup
        client.force_login(user)
        client.post(url(task))
        for child in children:
            child.refresh_from_db()
            assert child.parent_id is None
            assert child.epic_id == task.pk
        task.refresh_from_db()
        assert task.epic_counts == (0, 2)

    def test_the_conversion_is_logged(self, client, setup):
        _, _, user, task, children = setup
        client.force_login(user)
        client.post(url(task))
        event = ActivityLog.objects.get(event_type="task.turned_into_epic")
        assert event.actor_id == user.id
        assert event.payload["tasks"] == len(children)

    def test_a_blocked_task_is_refused(self, client, setup):
        _, _, user, _, children = setup
        client.force_login(user)
        resp = client.post(url(children[0]))
        assert resp.status_code == 400
        children[0].refresh_from_db()
        assert children[0].kind == Task.KIND_TASK

    def test_it_disappears_from_the_board(self, client, setup):
        """The headline consequence: an epic is not a card any more."""
        _, project, user, task, _ = setup
        client.force_login(user)
        client.post(url(task))
        assert task.pk not in set(Task.objects.work().values_list("pk", flat=True))
        assert task.pk in set(Task.objects.epics().values_list("pk", flat=True))


def back_url(task):
    """Return the turn-back-into-a-task endpoint for ``task``."""
    return f"/projects/{task.project.slug_prefix}/{task.number}/turn-into-task/"


@pytest.mark.django_db
class TestTheWayBack:
    """An epic can go back to being a task while there is a way back.

    The rule is the design's — "reversible while the epic has no tasks
    from other projects" — stated as what the code can check: its tasks
    become subtasks again, so each has to be able to be one.
    """

    def test_it_becomes_a_task_again_and_takes_its_tasks_as_subtasks(self, client, setup):
        _, _, user, task, children = setup
        client.force_login(user)
        client.post(url(task))
        resp = client.post(back_url(task))
        assert resp.status_code == 204
        task.refresh_from_db()
        assert task.kind == Task.KIND_TASK
        for child in children:
            child.refresh_from_db()
            assert (child.parent_id, child.epic_id) == (task.pk, None)

    def test_it_lands_on_the_status_its_work_added_up_to(self, client, setup):
        _, _, user, task, children = setup
        client.force_login(user)
        client.post(url(task))
        Task.objects.filter(pk=children[0].pk).update(status=Task.STATUS_IN_PROGRESS)
        task.refresh_from_db()
        expected = task.epic_status
        client.post(back_url(task))
        task.refresh_from_db()
        assert task.status == expected == Task.STATUS_IN_PROGRESS

    def test_work_from_another_project_blocks_the_way_back(self, client, setup):
        """A subtask lives in its parent's project — that work would be orphaned."""
        _, project, user, task, _ = setup
        client.force_login(user)
        client.post(url(task))
        elsewhere = ProjectFactory(workspace=project.workspace, slug_prefix="FAR")
        TaskFactory(project=elsewhere, epic=task, reporter=user)
        resp = client.get(back_url(task))
        assert resp.context["blockers"]
        assert client.post(back_url(task)).status_code == 400
        task.refresh_from_db()
        assert task.kind == Task.KIND_EPIC

    def test_a_member_with_its_own_subtasks_blocks_the_way_back(self, client, setup):
        """The depth-1 rule: a subtask cannot have a subtask."""
        _, project, user, task, children = setup
        client.force_login(user)
        client.post(url(task))
        TaskFactory(project=project, parent=children[0], reporter=user)
        resp = client.get(back_url(task))
        assert resp.context["blockers"]
        assert client.post(back_url(task)).status_code == 400

    def test_a_plain_task_is_refused(self, client, setup):
        _, _, user, task, _ = setup
        client.force_login(user)
        assert client.get(back_url(task)).context["blockers"]
        assert client.post(back_url(task)).status_code == 400

    def test_the_dialog_says_what_does_not_come_back(self, client, setup):
        """It is a conversion back, not an undo — the dropped fields are gone."""
        _, _, user, task, _ = setup
        client.force_login(user)
        client.post(url(task))
        body = client.get(back_url(task)).content.decode()
        assert "Not restored:" in body
        assert "csrfmiddlewaretoken" in body

    def test_the_round_trip_is_logged(self, client, setup):
        _, _, user, task, _ = setup
        client.force_login(user)
        client.post(url(task))
        client.post(back_url(task))
        assert ActivityLog.objects.filter(event_type="task.turned_into_task").count() == 1
