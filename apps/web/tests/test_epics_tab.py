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


def grid(client, workspace):
    """Return the epics the tab rendered, with their cells."""
    resp = client.get(f"/{workspace.slug}/epics/")
    assert resp.status_code == 200
    return resp.context["epics"], resp.content.decode()


@pytest.mark.django_db
class TestTheGrid:

    def test_the_tab_lists_the_workspace_epics(self, client, setup):
        workspace, _, user, epic = setup
        client.force_login(user)
        epics, body = grid(client, workspace)
        assert [e.pk for e in epics] == [epic.pk]
        assert epic.title in body

    def test_a_row_is_counts_per_status(self, client, setup):
        workspace, _, user, _ = setup
        client.force_login(user)
        epics, _ = grid(client, workspace)
        by_status = {cell["status"]: cell["n"] for cell in epics[0].cells}
        assert by_status[Task.STATUS_DONE] == 1
        assert by_status[Task.STATUS_IN_PROGRESS] == 1
        assert by_status[Task.STATUS_TODO] == 0

    def test_cancelled_has_no_column(self, client, setup):
        workspace, _, user, _ = setup
        client.force_login(user)
        epics, _ = grid(client, workspace)
        # Out for the same reason it is out of ``epic_members``: it is not
        # work any more, and a column of it would hold progress down.
        assert Task.STATUS_CANCELLED not in [cell["status"] for cell in epics[0].cells]

    def test_a_cell_shades_with_its_count(self, client, setup):
        workspace, project, user, epic = setup
        for _ in range(6):
            TaskFactory(project=project, epic=epic, status=Task.STATUS_TODO)
        client.force_login(user)
        epics, _ = grid(client, workspace)
        by_status = {cell["status"]: cell for cell in epics[0].cells}
        assert by_status[Task.STATUS_TODO]["shade"] > by_status[Task.STATUS_DONE]["shade"]
        assert by_status[Task.STATUS_READY]["shade"] == 0

    def test_the_done_share_is_over_the_live_tasks(self, client, setup):
        workspace, project, user, epic = setup
        TaskFactory(project=project, epic=epic, status=Task.STATUS_CANCELLED)
        client.force_login(user)
        epics, _ = grid(client, workspace)
        # One done of two live tasks; the cancelled one is not counted.
        assert epics[0].done_pct == 50

    def test_an_epic_with_no_tasks_has_no_share(self, client, setup):
        workspace, project, user, _ = setup
        TaskFactory(project=project, kind=Task.KIND_EPIC, status=Task.STATUS_PLANNED, title="Empty one")
        client.force_login(user)
        epics, _ = grid(client, workspace)
        empty = next(e for e in epics if e.title == "Empty one")
        assert empty.done_pct is None

    def test_tasks_never_appear_as_rows(self, client, setup):
        workspace, project, user, _ = setup
        TaskFactory(project=project, title="Just a task")
        client.force_login(user)
        epics, body = grid(client, workspace)
        assert all(e.kind == Task.KIND_EPIC for e in epics)
        assert "Just a task" not in body

    def test_the_switch_replaces_the_grid_with_an_explanation(self, client, setup):
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
