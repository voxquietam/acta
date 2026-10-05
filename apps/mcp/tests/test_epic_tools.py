"""Epics through MCP: listing, filing, and the two conversions.

An epic is a task with ``kind="epic"`` (docs/decisions/0036-epics.md),
which is exactly why the tools need pinning: a caller that cannot see
the UI has only the tool contract to go on. The cases below are the
ones where a loose contract would mislead it — epics counted as work,
a conversion attempted through a field that does not exist, and a
destructive change applied without being asked for.
"""

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.mcp.tools import CALLABLES
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """A member, a project, an epic, and a task the epic collects."""
    user = UserFactory()
    ws = WorkspaceFactory()
    WorkspaceMember.objects.create(user=user, workspace=ws)
    project = ProjectFactory(workspace=ws, slug_prefix="EPC")
    epic = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, title="Billing")
    member = TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE, title="Dual-write")
    return user, ws, project, epic, member


@pytest.mark.django_db
class TestListing:
    """Epics are an umbrella over work, so they are not work by default."""

    def test_an_epic_is_left_out_of_a_plain_list(self, setup):
        user, _, _, epic, member = setup
        slugs = [row["slug"] for row in CALLABLES["acta_tasks_list"](user, {"project": "EPC"})]
        assert member.slug in slugs
        assert epic.slug not in slugs

    def test_kind_epic_lists_only_epics(self, setup):
        user, _, _, epic, _ = setup
        rows = CALLABLES["acta_tasks_list"](user, {"project": "EPC", "kind": "epic"})
        assert [r["slug"] for r in rows] == [epic.slug]

    def test_kind_all_lists_both(self, setup):
        user, _, _, epic, member = setup
        slugs = {r["slug"] for r in CALLABLES["acta_tasks_list"](user, {"project": "EPC", "kind": "all"})}
        assert {epic.slug, member.slug} <= slugs

    def test_the_epic_argument_lists_what_it_collects(self, setup):
        user, _, _, epic, member = setup
        rows = CALLABLES["acta_tasks_list"](user, {"epic": epic.slug})
        assert [r["slug"] for r in rows] == [member.slug]

    def test_an_epic_row_carries_computed_progress_and_status(self, setup):
        """Its own ``status`` column is meaningless — the tasks decide."""
        user, _, project, epic, _ = setup
        TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        row = CALLABLES["acta_tasks_list"](user, {"project": "EPC", "kind": "epic"})[0]
        assert row["progress"] == {"done": 1, "total": 2}
        assert row["status"] == Task.STATUS_IN_PROGRESS

    def test_get_returns_the_members_and_the_rollup(self, setup):
        user, _, _, epic, member = setup
        detail = CALLABLES["acta_task_get"](user, {"slug": epic.slug})
        assert detail["epic"]["total"] == 1
        assert [m["slug"] for m in detail["epic"]["tasks"]] == [member.slug]

    def test_a_task_names_the_epic_that_holds_it(self, setup):
        user, _, _, epic, member = setup
        assert CALLABLES["acta_task_get"](user, {"slug": member.slug})["epic_slug"] == epic.slug


@pytest.mark.django_db
class TestFiling:
    """``epic_slug`` is how a task joins or leaves an epic."""

    def test_create_can_make_an_epic(self, setup):
        user, _, _, _, _ = setup
        created = CALLABLES["acta_task_create"](user, {"project": "EPC", "title": "Search v2", "kind": "epic"})
        assert Task.objects.get(project__slug_prefix="EPC", title="Search v2").kind == Task.KIND_EPIC
        assert created["slug"].startswith("EPC-")

    def test_update_files_a_task_into_an_epic_and_out_again(self, setup):
        user, _, project, epic, _ = setup
        loose = TaskFactory(project=project)
        CALLABLES["acta_task_update"](user, {"slug": loose.slug, "epic_slug": epic.slug})
        loose.refresh_from_db()
        assert loose.epic_id == epic.id
        CALLABLES["acta_task_update"](user, {"slug": loose.slug, "epic_slug": None})
        loose.refresh_from_db()
        assert loose.epic_id is None

    def test_bulk_update_collects_a_batch(self, setup):
        user, _, project, epic, _ = setup
        tasks = [TaskFactory(project=project) for _ in range(2)]
        CALLABLES["acta_tasks_bulk_update"](
            user,
            {"updates": [{"slug": t.slug, "epic_slug": epic.slug} for t in tasks]},
        )
        for task in tasks:
            task.refresh_from_db()
            assert task.epic_id == epic.id

    def test_an_epic_cannot_be_filed_into_an_epic(self, setup):
        user, _, project, epic, _ = setup
        other = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED)
        with pytest.raises(Exception):
            CALLABLES["acta_task_update"](user, {"slug": other.slug, "epic_slug": epic.slug})

    def test_an_epic_refuses_a_deadline(self, setup):
        """It takes its dates from its tasks; accepting one would be a lie."""
        user, _, _, epic, _ = setup
        with pytest.raises(Exception):
            CALLABLES["acta_task_update"](user, {"slug": epic.slug, "due_date": "2026-07-01"})


@pytest.mark.django_db
class TestTheConversions:
    """Destructive, so they are a dry run until asked to apply."""

    def test_a_dry_run_changes_nothing_and_says_what_it_would_do(self, setup):
        user, _, project, _, _ = setup
        task = TaskFactory(project=project, status=Task.STATUS_TODO, size=5)
        child = TaskFactory(project=project, parent=task)
        answer = CALLABLES["acta_task_turn_into_epic"](user, {"slug": task.slug})
        task.refresh_from_db()
        assert task.kind == Task.KIND_TASK
        assert answer["would"]["tasks_from_subtasks"] == [child.slug]
        assert "size" in answer["would"]["dropped"]

    def test_confirm_applies_it(self, setup):
        user, _, project, _, _ = setup
        task = TaskFactory(project=project, status=Task.STATUS_TODO, size=5)
        child = TaskFactory(project=project, parent=task)
        CALLABLES["acta_task_turn_into_epic"](user, {"slug": task.slug, "confirm": True})
        task.refresh_from_db()
        child.refresh_from_db()
        assert (task.kind, task.size, task.status) == (Task.KIND_EPIC, None, Task.STATUS_PLANNED)
        assert (child.parent_id, child.epic_id) == (None, task.pk)
        assert ActivityLog.objects.filter(event_type="task.turned_into_epic", actor=user).exists()

    def test_a_subtask_is_refused(self, setup):
        user, _, project, _, _ = setup
        parent = TaskFactory(project=project)
        child = TaskFactory(project=project, parent=parent)
        with pytest.raises(ValueError):
            CALLABLES["acta_task_turn_into_epic"](user, {"slug": child.slug, "confirm": True})

    def test_the_way_back_is_a_dry_run_too(self, setup):
        user, _, _, epic, member = setup
        answer = CALLABLES["acta_task_turn_into_task"](user, {"slug": epic.slug})
        epic.refresh_from_db()
        assert epic.kind == Task.KIND_EPIC
        assert answer["would"]["subtasks_from_tasks"] == [member.slug]

    def test_the_way_back_applies_with_confirm(self, setup):
        user, _, _, epic, member = setup
        CALLABLES["acta_task_turn_into_task"](user, {"slug": epic.slug, "confirm": True})
        epic.refresh_from_db()
        member.refresh_from_db()
        assert epic.kind == Task.KIND_TASK
        assert (member.parent_id, member.epic_id) == (epic.pk, None)

    def test_work_from_another_project_blocks_the_way_back(self, setup):
        user, ws, _, epic, _ = setup
        TaskFactory(project=ProjectFactory(workspace=ws, slug_prefix="FAR"), epic=epic)
        with pytest.raises(ValueError):
            CALLABLES["acta_task_turn_into_task"](user, {"slug": epic.slug, "confirm": True})
