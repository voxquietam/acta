"""The Epics tab, and making an epic from it.

The tab is a grid rather than a list because an epic's own state is read
off its tasks: the question worth answering when you look at all of them
is *where the work has collected*. See docs/decisions/0036-epics.md.
"""

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """A workspace with a member, a project and an epic over two tasks."""
    workspace = WorkspaceFactory()
    user = UserFactory()
    WorkspaceMember.objects.create(user=user, workspace=workspace)
    project = ProjectFactory(workspace=workspace, slug_prefix="EPT")
    epic = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, title="Billing migration")
    TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE)
    TaskFactory(project=project, epic=epic, status=Task.STATUS_IN_PROGRESS)
    return workspace, project, user, epic


def table(client, workspace):
    """Return the epics the tab rendered, with their rollups."""
    resp = client.get(f"/{workspace.slug}/epics/")
    assert resp.status_code == 200
    return resp.context["epics"], resp.content.decode()


@pytest.mark.django_db
class TestTheTable:
    """One row per epic: who carries it, how far along, and how quiet."""

    def test_the_tab_lists_the_workspace_epics(self, client, setup):
        workspace, _, user, epic = setup
        client.force_login(user)
        epics, body = table(client, workspace)
        assert [e.pk for e in epics] == [epic.pk]
        assert epic.title in body

    def test_a_row_carries_the_rollup(self, client, setup):
        workspace, _, user, _ = setup
        client.force_login(user)
        epics, _ = table(client, workspace)
        assert (epics[0].member_done, epics[0].member_total, epics[0].done_pct) == (1, 2, 50)

    def test_cancelled_and_archived_tasks_leave_the_count(self, client, setup):
        from django.utils import timezone

        workspace, project, user, epic = setup
        TaskFactory(project=project, epic=epic, status=Task.STATUS_CANCELLED)
        TaskFactory(project=project, epic=epic, archived_at=timezone.now())
        client.force_login(user)
        epics, _ = table(client, workspace)
        assert epics[0].member_total == 2

    def test_a_row_names_every_project_carrying_it(self, client, setup):
        workspace, _, user, epic = setup
        other = ProjectFactory(workspace=workspace, slug_prefix="OTH")
        TaskFactory(project=other, epic=epic, status=Task.STATUS_TODO)
        client.force_login(user)
        epics, body = table(client, workspace)
        # The answer every other view splits apart.
        assert [p.slug_prefix for p in epics[0].projects] == ["EPT", "OTH"]
        assert "OTH" in body

    def test_a_row_counts_what_is_blocked(self, client, setup):
        workspace, project, user, epic = setup
        blocker = TaskFactory(project=project, status=Task.STATUS_TODO)
        stuck = TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        stuck.blocked_by.add(blocker)
        client.force_login(user)
        epics, _ = table(client, workspace)
        assert epics[0].blocked == 1

    def test_an_epic_that_closed_nothing_recently_reads_as_paused(self, client, setup):
        import datetime

        from django.utils import timezone

        workspace, project, user, epic = setup
        old = TaskFactory(project=project, epic=epic, status=Task.STATUS_DONE)
        Task.objects.filter(pk=old.pk).update(completed_at=timezone.now() - datetime.timedelta(days=40))
        Task.objects.filter(epic=epic, completed_at__isnull=False).exclude(pk=old.pk).update(completed_at=None)
        client.force_login(user)
        epics, _ = table(client, workspace)
        # "5 of 22 done" says nothing about whether an epic is moving.
        assert epics[0].paused is True
        assert epics[0].quiet_days >= 40

    def test_a_busy_epic_is_not_paused(self, client, setup):
        workspace, _, user, _ = setup
        client.force_login(user)
        epics, _ = table(client, workspace)
        assert epics[0].paused is False

    def test_the_quietest_epic_sorts_first(self, client, setup):
        workspace, project, user, epic = setup
        silent = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, title="Never moved")
        TaskFactory(project=project, epic=silent, status=Task.STATUS_TODO)
        client.force_login(user)
        epics, _ = table(client, workspace)
        # An epic that has never closed anything is the one worth opening.
        assert epics[0].pk == silent.pk

    def test_an_epic_with_no_tasks_has_no_share(self, client, setup):
        workspace, project, user, _ = setup
        TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, title="Empty one")
        client.force_login(user)
        epics, _ = table(client, workspace)
        empty = next(e for e in epics if e.title == "Empty one")
        assert empty.done_pct is None
        assert empty.member_total == 0

    def test_tasks_never_appear_as_rows(self, client, setup):
        workspace, project, user, _ = setup
        TaskFactory(project=project, title="Just a task")
        client.force_login(user)
        epics, body = table(client, workspace)
        assert all(e.kind == Task.KIND_EPIC for e in epics)
        assert "Just a task" not in body

    def test_the_switch_replaces_the_table_with_an_explanation(self, client, setup):
        workspace, _, user, _ = setup
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        client.force_login(user)
        resp = client.get(f"/{workspace.slug}/epics/")
        # A stale link must not 404 on someone who just flipped it off.
        assert resp.status_code == 200
        assert resp.context["epics"] == []
        assert "Epics are off for this workspace" in resp.content.decode()

    def test_the_sidebar_link_follows_the_switch(self, client, setup):
        workspace, _, user, _ = setup
        client.force_login(user)
        assert f"/{workspace.slug}/epics/" in client.get(f"/{workspace.slug}/tasks/").content.decode()
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        assert f"/{workspace.slug}/epics/" not in client.get(f"/{workspace.slug}/tasks/").content.decode()


@pytest.mark.django_db
class TestMakingAnEpicFromTheTab:

    def test_the_dialog_opens_in_epic_mode(self, client, setup):
        _, project, user, _ = setup
        client.force_login(user)
        body = client.get("/tasks/new/", {"kind": "epic", "project": project.slug_prefix}).content.decode()
        assert "New epic" in body
        assert 'name="kind" value="epic"' in body

    def test_epic_mode_drops_the_rows_it_would_derive(self, client, setup):
        import json
        import re

        _, project, user, _ = setup
        client.force_login(user)
        body = client.get("/tasks/new/", {"kind": "epic", "project": project.slug_prefix}).content.decode()
        payload = json.loads(
            re.search(r'<script id="create-task-data" type="application/json">(.*?)</script>', body, re.S).group(1),
        )
        keys = {f["key"] for f in payload["fields"]}
        # Dates, size and cycle come from the tasks; parent and epic are
        # relationships an epic cannot have.
        assert keys.isdisjoint({"due", "size", "cycle", "parent", "epic", "repeat"})
        assert {"status", "priority", "assignee", "labels"} <= keys

    def test_posting_creates_an_epic(self, client, setup):
        _, project, user, _ = setup
        client.force_login(user)
        resp = client.post(
            "/tasks/new/",
            {"project": project.slug_prefix, "title": "Access audit", "kind": "epic"},
        )
        assert resp.status_code == 204
        assert Task.objects.get(title="Access audit").kind == Task.KIND_EPIC

    def test_a_stale_form_cannot_smuggle_derived_fields(self, client, setup):
        _, project, user, _ = setup
        client.force_login(user)
        client.post(
            "/tasks/new/",
            {
                "project": project.slug_prefix,
                "title": "Access audit",
                "kind": "epic",
                "due_date": "2026-12-01",
                "size": "5",
            },
        )
        epic = Task.objects.get(title="Access audit")
        # Dropped rather than rejected: the dialog does not offer these
        # rows, so a value here came from a stale form, not a person.
        assert epic.due_date is None
        assert epic.size is None

    def test_the_switch_refuses_the_create(self, client, setup):
        workspace, project, user, _ = setup
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        client.force_login(user)
        resp = client.post(
            "/tasks/new/",
            {"project": project.slug_prefix, "title": "Access audit", "kind": "epic"},
        )
        assert resp.status_code == 400


@pytest.mark.django_db
class TestTheEpicPage:
    """An epic's page is its board, not the task page with a locked rail."""

    def board(self, client, epic):
        """Fetch the epic page and return its context + body."""
        resp = client.get(f"/projects/{epic.project.slug_prefix}/{epic.number}/")
        assert resp.status_code == 200
        return resp.context, resp.content.decode()

    def test_an_epic_renders_its_own_template(self, client, setup):
        _, _, user, epic = setup
        client.force_login(user)
        resp = client.get(f"/projects/{epic.project.slug_prefix}/{epic.number}/")
        assert "web/projects/epic_detail.html" in [t.name for t in resp.templates]

    def test_a_plain_task_still_renders_the_task_page(self, client, setup):
        _, project, user, _ = setup
        task = TaskFactory(project=project)
        client.force_login(user)
        resp = client.get(f"/projects/{project.slug_prefix}/{task.number}/")
        assert "web/projects/task_detail.html" in [t.name for t in resp.templates]

    def test_it_uses_the_app_kanban(self, client, setup):
        _, _, user, epic = setup
        client.force_login(user)
        resp = client.get(f"/projects/{epic.project.slug_prefix}/{epic.number}/")
        # Not a second board: the same template, so drag-to-change-status,
        # collapsible columns and the live insert come with it.
        assert "web/projects/_kanban.html" in [t.name for t in resp.templates]

    def test_the_columns_are_the_working_statuses(self, client, setup):
        _, _, user, epic = setup
        client.force_login(user)
        ctx, _ = self.board(client, epic)
        assert [c["key"] for c in ctx["columns"]] == list(Task.KANBAN_STATUS_VALUES)

    def test_a_task_lands_in_its_own_column(self, client, setup):
        _, _, user, epic = setup
        client.force_login(user)
        ctx, _ = self.board(client, epic)
        by_status = {c["key"]: c for c in ctx["columns"]}
        assert len(by_status[Task.STATUS_DONE]["tasks"]) == 1
        assert len(by_status[Task.STATUS_IN_PROGRESS]["tasks"]) == 1

    def test_a_blocked_task_keeps_its_column_and_rises(self, client, setup):
        _, project, user, epic = setup
        blocker = TaskFactory(project=project, status=Task.STATUS_TODO)
        loose = TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO, title="Fine")
        stuck = TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO, title="Stuck")
        stuck.blocked_by.add(blocker)
        client.force_login(user)
        ctx, _ = self.board(client, epic)
        todo = next(c for c in ctx["columns"] if c["key"] == Task.STATUS_TODO)
        # Top of its own status column — moving it to a Blocked column
        # would trade "how far along is this" for "what is stuck".
        assert [t.pk for t in todo["tasks"]] == [stuck.pk, loose.pk]
        assert ctx["epic_blocked_total"] == 1

    def test_the_cards_say_which_project_they_came_from(self, client, setup):
        _, _, user, epic = setup
        client.force_login(user)
        ctx, _ = self.board(client, epic)
        # An epic's board is the one kanban whose cards are not all from
        # the same project.
        assert ctx["show_project"] is True

    def test_the_page_says_which_projects_carry_it(self, client, setup):
        workspace, _, user, epic = setup
        other = ProjectFactory(workspace=workspace, slug_prefix="OTH")
        TaskFactory(project=other, epic=epic, status=Task.STATUS_TODO)
        client.force_login(user)
        ctx, _ = self.board(client, epic)
        # The point of an epic is that the answer spans projects.
        assert {row["project"].slug_prefix for row in ctx["epic_projects"]} == {"EPT", "OTH"}

    def test_the_rollup_is_on_the_page(self, client, setup):
        _, _, user, epic = setup
        client.force_login(user)
        ctx, _ = self.board(client, epic)
        assert (ctx["epic_done"], ctx["epic_total"], ctx["epic_pct"]) == (1, 2, 50)

    def test_the_rail_locks_what_an_epic_derives(self, client, setup):
        _, _, user, epic = setup
        client.force_login(user)
        _, body = self.board(client, epic)
        # Size, cycle and the three dates are gone; progress and the span
        # take their place, read-only.
        assert "Set size" not in body
        assert "Set deadline" not in body
        assert "Progress" in body
        assert "Make recurring" not in body

    def test_the_board_carries_the_filter_dock(self, client, setup):
        _, _, user, epic = setup
        client.force_login(user)
        ctx, body = self.board(client, epic)
        # Same dock as every other board; its filters are client-side, so
        # the cards carry the attributes it matches on.
        assert "acta-dock-wrap" in body
        assert "filter-dock-data" in body
        assert ctx["filter_htmx_target"] == "#epic-board"

    def test_the_dock_offers_the_project_axis_here(self, client, setup):
        import json
        import re

        workspace, _, user, epic = setup
        other = ProjectFactory(workspace=workspace, slug_prefix="OTH")
        TaskFactory(project=other, epic=epic, status=Task.STATUS_TODO)
        client.force_login(user)
        _, body = self.board(client, epic)
        payload = json.loads(
            re.search(r'<script id="filter-dock-data" type="application/json">(.*?)</script>', body, re.S).group(1),
        )
        keys = [f["key"] for f in payload["fields"]]
        # An epic is the one board whose cards come from several
        # projects, so that axis matters more here than anywhere.
        assert "project" in keys
        # Status is the columns, as on any kanban.
        assert "status" not in keys

    def test_an_empty_epic_explains_itself(self, client, setup):
        workspace, project, user, _ = setup
        empty = TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, title="Nothing in me")
        client.force_login(user)
        _, body = self.board(client, empty)
        assert "This epic has no tasks yet" in body


@pytest.mark.django_db
class TestTheWorkspaceSwitch:
    """The toggle in workspace settings, and what it does not do."""

    def test_the_settings_page_offers_it(self, client, setup):
        workspace, _, _, _ = setup
        client.force_login(workspace.owner)
        body = client.get(f"/workspaces/{workspace.slug}/settings/").content.decode()
        assert 'name="epics_enabled"' in body

    def test_saving_without_it_turns_epics_off(self, client, setup):
        workspace, _, _, _ = setup
        client.force_login(workspace.owner)
        client.post(
            f"/workspaces/{workspace.slug}/general/",
            {"name": workspace.name},
        )
        workspace.refresh_from_db()
        assert workspace.epics_enabled is False

    def test_turning_it_off_keeps_the_epic_and_its_tasks(self, client, setup):
        workspace, _, _, epic = setup
        client.force_login(workspace.owner)
        client.post(
            f"/workspaces/{workspace.slug}/general/",
            {"name": workspace.name},
        )
        epic.refresh_from_db()
        # Hiding the feature must not scatter an effort someone spent a
        # quarter assembling.
        assert epic.kind == Task.KIND_EPIC
        assert epic.epic_counts == (1, 2)

    def test_turning_it_back_on_restores_the_tab(self, client, setup):
        workspace, _, user, epic = setup
        workspace.epics_enabled = False
        workspace.save(update_fields=["epics_enabled"])
        client.force_login(workspace.owner)
        client.post(
            f"/workspaces/{workspace.slug}/general/",
            {"name": workspace.name, "epics_enabled": "on"},
        )
        client.force_login(user)
        resp = client.get(f"/{workspace.slug}/epics/")
        assert [e.pk for e in resp.context["epics"]] == [epic.pk]
