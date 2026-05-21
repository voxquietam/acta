"""Tests for the All Tasks page (:class:`apps.web.views.AllTasksView`)."""

import re

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.mark.django_db
class TestHtmxBoostTemplateSelection:
    """``HX-Boosted`` swaps need the full template, not the inner partial.

    Sidebar / topbar navigation links carry ``hx-boost="true"`` plus
    ``hx-select="#app-content"`` — for that select to find anything
    the response must extend ``base_app.html``. Plain ``HX-Request``
    (filter form / panel refresh) keeps the inner-only partial.
    """

    def test_plain_htmx_returns_inner_partial(self, client):
        ws = WorkspaceFactory()
        ProjectFactory(workspace=ws)
        client.force_login(ws.owner)
        resp = client.get(reverse("web:all_tasks"), HTTP_HX_REQUEST="true")
        body = resp.content.decode()
        # Inner partial has no ``<!DOCTYPE`` / ``<html>`` shell.
        assert "<!DOCTYPE" not in body
        assert "<html" not in body

    def test_htmx_boost_returns_full_shell(self, client):
        ws = WorkspaceFactory()
        ProjectFactory(workspace=ws)
        client.force_login(ws.owner)
        resp = client.get(
            reverse("web:all_tasks"),
            HTTP_HX_REQUEST="true",
            HTTP_HX_BOOSTED="true",
        )
        body = resp.content.decode()
        # Full template carries the shell so ``hx-select="#app-content"``
        # can extract the target fragment.
        assert 'id="app-content"' in body
        assert "<html" in body

    def test_history_restore_returns_full_shell(self, client):
        """Back/forward history restore must rebuild the whole page.

        On a history-cache miss HTMX re-fetches the URL with
        ``HX-History-Restore-Request: true`` to repopulate ``<body>``.
        Serving the inner partial there leaves the page as the bare
        panel with no sidebar / topbar (the "timeline goes fullscreen
        on Back" bug), so the response must be the full shell.
        """
        ws = WorkspaceFactory()
        ProjectFactory(workspace=ws)
        client.force_login(ws.owner)
        resp = client.get(
            reverse("web:all_tasks"),
            HTTP_HX_REQUEST="true",
            HTTP_HX_HISTORY_RESTORE_REQUEST="true",
        )
        body = resp.content.decode()
        assert "<html" in body
        assert 'id="app-content"' in body


def _table_body(html):
    """Return the substring covering the table body so ordering asserts
    don't pick up duplicate task titles rendered in the kanban body.

    Both kanban and table bodies live in the DOM (Alpine ``x-show``
    selects which is visible). Ordering tests need to inspect the
    table body in isolation since kanban has its own status-grouped
    order.
    """
    match = re.search(r"<table\b[\s\S]*?</table>", html)
    return match.group(0) if match else html


@pytest.fixture
def setup(db):
    """Two workspaces both owned by the same user + one project each.

    ``ws1`` is the user's active workspace, so All Tasks (scoped to the
    active workspace) shows ``p1`` tasks and hides ``p2`` until switched.
    """
    user = UserFactory()
    ws1 = WorkspaceFactory(owner=user)
    ws2 = WorkspaceFactory(owner=user)
    WorkspaceMemberFactory(workspace=ws2, user=user)
    p1 = ProjectFactory(workspace=ws1)
    p2 = ProjectFactory(workspace=ws2)
    user.active_workspace = ws1
    user.save(update_fields=["active_workspace"])
    return user, ws1, ws2, p1, p2


@pytest.mark.django_db
class TestAllTasksScope:
    """All Tasks is scoped to the user's active workspace."""

    def test_lists_only_active_workspace_tasks(self, client, setup):
        """Tasks in the active workspace show; another workspace's tasks
        are hidden until the user switches into it."""
        user, ws1, ws2, p1, p2 = setup  # active workspace is ws1
        TaskFactory(project=p1, reporter=user, title="From WS1", status=Task.STATUS_TODO)
        TaskFactory(project=p2, reporter=user, title="From WS2", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks"))
        body = resp.content.decode()
        assert "From WS1" in body
        assert "From WS2" not in body

    def test_switching_workspace_changes_scope(self, client, setup):
        """After switching to ws2, All Tasks shows ws2's tasks, not ws1's."""
        user, ws1, ws2, p1, p2 = setup
        TaskFactory(project=p1, reporter=user, title="From WS1", status=Task.STATUS_TODO)
        TaskFactory(project=p2, reporter=user, title="From WS2", status=Task.STATUS_TODO)
        user.active_workspace = ws2
        user.save(update_fields=["active_workspace"])
        client.force_login(user)
        body = client.get(reverse("web:all_tasks")).content.decode()
        assert "From WS2" in body
        assert "From WS1" not in body

    def test_foreign_workspace_tasks_excluded(self, client, setup):
        user, _, _, _, _ = setup
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        TaskFactory(project=foreign_project, reporter=foreign_ws.owner, title="Foreign")
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks"))
        assert "Foreign" not in resp.content.decode()


@pytest.mark.django_db
class TestAllTasksFilters:
    """Querystring filters narrow the result set."""

    def test_status_filter(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-todo", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-prog", status=Task.STATUS_IN_PROGRESS)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?status=to-do")
        body = resp.content.decode()
        assert "t-todo" in body
        assert "t-prog" not in body

    def test_done_visible_by_default(self, client, setup):
        """Done tasks are shown by default — the ``default_show_done``
        seam stays True until a per-user setting overrides it."""
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-done", status=Task.STATUS_DONE)
        TaskFactory(project=p1, reporter=user, title="t-todo", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks"))
        body = resp.content.decode()
        assert "t-todo" in body
        assert "t-done" in body

    def test_status_filter_excludes_done(self, client, setup):
        """An explicit status filter scopes the list to those statuses
        and drops everything else, done included."""
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-done", status=Task.STATUS_DONE)
        TaskFactory(project=p1, reporter=user, title="t-todo", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?status=to-do")
        body = resp.content.decode()
        assert "t-todo" in body
        assert "t-done" not in body

    def test_status_done_param_includes_done(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-done", status=Task.STATUS_DONE)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?status=done")
        assert "t-done" in resp.content.decode()

    def test_assignee_me(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, assignee=user, title="mine", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, assignee=None, title="nobody", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?assignee=me")
        body = resp.content.decode()
        assert "mine" in body
        assert "nobody" not in body

    def test_project_filter(self, client, setup):
        user, _, _, p1, p2 = setup
        TaskFactory(project=p1, reporter=user, title="in-p1", status=Task.STATUS_TODO)
        TaskFactory(project=p2, reporter=user, title="in-p2", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?project={p1.id}")
        body = resp.content.decode()
        assert "in-p1" in body
        assert "in-p2" not in body

    def test_search_query(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="Refactor auth", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="Wire up SSE", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?q=refactor")
        body = resp.content.decode()
        assert "Refactor auth" in body
        assert "Wire up SSE" not in body

    def test_assignee_by_user_id(self, client, setup):
        """``?assignee=<id>`` filters to that specific user's tasks."""
        user, ws1, _, p1, _ = setup
        other = UserFactory()
        WorkspaceMemberFactory(workspace=ws1, user=other)
        TaskFactory(project=p1, reporter=user, assignee=user, title="mine", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, assignee=other, title="theirs", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?assignee={other.id}")
        body = resp.content.decode()
        assert "theirs" in body
        assert "mine" not in body

    def test_assignee_multi_value(self, client, setup):
        """Multiple ``?assignee=`` values combine as OR (incl. ``unassigned``)."""
        user, ws1, _, p1, _ = setup
        other = UserFactory()
        WorkspaceMemberFactory(workspace=ws1, user=other)
        TaskFactory(project=p1, reporter=user, assignee=other, title="for-other", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, assignee=None, title="nobody", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, assignee=user, title="mine", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?assignee={other.id}&assignee=unassigned")
        body = resp.content.decode()
        assert "for-other" in body
        assert "nobody" in body
        assert "mine" not in body

    def test_label_filter(self, client, setup):
        """``?label=<id>`` keeps only tasks tagged with that label."""
        user, _, _, p1, _ = setup
        keep_label = LabelFactory(workspace=p1.workspace, name="keep")
        drop_label = LabelFactory(workspace=p1.workspace, name="drop")
        t_keep = TaskFactory(project=p1, reporter=user, title="has-keep", status=Task.STATUS_TODO)
        t_keep.labels.add(keep_label)
        t_drop = TaskFactory(project=p1, reporter=user, title="has-drop", status=Task.STATUS_TODO)
        t_drop.labels.add(drop_label)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?label={keep_label.id}")
        body = resp.content.decode()
        assert "has-keep" in body
        assert "has-drop" not in body

    def test_workspace_filter(self, client, setup):
        """``?workspace=<id>`` restricts to tasks in that workspace."""
        user, ws1, ws2, p1, p2 = setup
        TaskFactory(project=p1, reporter=user, title="in-ws1", status=Task.STATUS_TODO)
        TaskFactory(project=p2, reporter=user, title="in-ws2", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + f"?workspace={ws1.id}")
        body = resp.content.decode()
        assert "in-ws1" in body
        assert "in-ws2" not in body


@pytest.mark.django_db
class TestAllTasksExcludeFilters:
    """Right-click exclude flow — querystring uses ``x<field>=<value>``."""

    def test_xstatus_drops_matching_status(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-todo", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-done", status=Task.STATUS_DONE)
        client.force_login(user)
        body = client.get(reverse("web:all_tasks") + "?xstatus=done").content.decode()
        assert "t-todo" in body
        assert "t-done" not in body

    def test_xpriority_drops_matching_priority(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-urgent", priority=Task.URGENT, status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-low", priority=Task.LOW, status=Task.STATUS_TODO)
        client.force_login(user)
        body = client.get(reverse("web:all_tasks") + "?xpriority=1").content.decode()
        assert "t-low" in body
        assert "t-urgent" not in body

    def test_xlabel_drops_tasks_carrying_label(self, client, setup):
        user, _, _, p1, _ = setup
        kill_label = LabelFactory(workspace=p1.workspace, name="kill")
        t_kill = TaskFactory(project=p1, reporter=user, title="has-kill", status=Task.STATUS_TODO)
        t_kill.labels.add(kill_label)
        TaskFactory(project=p1, reporter=user, title="clean", status=Task.STATUS_TODO)
        client.force_login(user)
        body = client.get(reverse("web:all_tasks") + f"?xlabel={kill_label.id}").content.decode()
        assert "clean" in body
        assert "has-kill" not in body

    def test_xassignee_drops_own_tasks(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, assignee=user, title="mine", status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, assignee=None, title="nobody", status=Task.STATUS_TODO)
        client.force_login(user)
        body = client.get(reverse("web:all_tasks") + "?xassignee=me").content.decode()
        assert "nobody" in body
        assert "mine" not in body

    def test_include_and_exclude_combine(self, client, setup):
        """``?status=todo&xpriority=1`` keeps todo but drops urgent ones."""
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-todo-urgent", priority=Task.URGENT, status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-todo-low", priority=Task.LOW, status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-prog-low", priority=Task.LOW, status=Task.STATUS_IN_PROGRESS)
        client.force_login(user)
        body = client.get(reverse("web:all_tasks") + "?status=to-do&xpriority=1").content.decode()
        assert "t-todo-low" in body
        assert "t-todo-urgent" not in body
        assert "t-prog-low" not in body


@pytest.mark.django_db
class TestAllTasksOrdering:
    """``?order=`` applies the smart per-column sort."""

    def test_priority_asc_urgent_before_low_then_no_priority(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-low", priority=Task.LOW, status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-urgent", priority=Task.URGENT, status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-noprio", priority=Task.NO_PRIORITY, status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?order=priority")
        body = _table_body(resp.content.decode())
        assert body.index("t-urgent") < body.index("t-low") < body.index("t-noprio")

    def test_priority_desc_keeps_no_priority_last(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-low", priority=Task.LOW, status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-urgent", priority=Task.URGENT, status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-noprio", priority=Task.NO_PRIORITY, status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?order=-priority")
        body = _table_body(resp.content.decode())
        assert body.index("t-low") < body.index("t-urgent") < body.index("t-noprio")

    def test_status_uses_logical_order(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-review", status=Task.STATUS_IN_REVIEW)
        TaskFactory(project=p1, reporter=user, title="t-planned", status=Task.STATUS_PLANNED)
        TaskFactory(project=p1, reporter=user, title="t-todo", status=Task.STATUS_TODO)
        client.force_login(user)
        # Status filter keeps done out by default, all three picked statuses are open.
        resp = client.get(
            reverse("web:all_tasks") + "?status=planned&status=to-do&status=in-review&order=status",
        )
        body = _table_body(resp.content.decode())
        assert body.index("t-planned") < body.index("t-todo") < body.index("t-review")

    def test_assignee_unassigned_sinks_in_both_directions(self, client, setup):
        user, _, _, p1, _ = setup
        alice = UserFactory(username="alice", first_name="Alice")
        WorkspaceMemberFactory(workspace=p1.workspace, user=alice)
        TaskFactory(project=p1, reporter=user, title="t-alice", assignee=alice, status=Task.STATUS_TODO)
        TaskFactory(project=p1, reporter=user, title="t-unassigned", assignee=None, status=Task.STATUS_TODO)
        client.force_login(user)
        body_asc = _table_body(client.get(reverse("web:all_tasks") + "?order=assignee").content.decode())
        body_desc = _table_body(client.get(reverse("web:all_tasks") + "?order=-assignee").content.decode())
        assert body_asc.index("t-alice") < body_asc.index("t-unassigned")
        assert body_desc.index("t-alice") < body_desc.index("t-unassigned")

    def test_id_sort_groups_by_project_then_number(self, client, setup):
        """``?order=id`` groups cross-project rows by project slug and
        sorts numerically within each group."""
        user, ws1, ws2, _, _ = setup  # active workspace is ws1
        # Explicit slug_prefixes so the order is deterministic regardless
        # of factory sequence numbers. Both in the active workspace (ws1)
        # — All Tasks is workspace-scoped, so cross-project ordering is
        # tested within one workspace.
        p_a = ProjectFactory(workspace=ws1, slug_prefix="AAA")
        p_b = ProjectFactory(workspace=ws1, slug_prefix="BBB")
        TaskFactory(project=p_a, reporter=user, title="t-AAA-2", number=2, status=Task.STATUS_TODO)
        TaskFactory(project=p_a, reporter=user, title="t-AAA-1", number=1, status=Task.STATUS_TODO)
        TaskFactory(project=p_b, reporter=user, title="t-BBB-1", number=1, status=Task.STATUS_TODO)
        client.force_login(user)
        body = _table_body(client.get(reverse("web:all_tasks") + "?order=id").content.decode())
        # AAA's slug_prefix sorts before BBB; within AAA numbers go asc.
        assert body.index("t-AAA-1") < body.index("t-AAA-2") < body.index("t-BBB-1")

    def test_unknown_order_falls_back_to_default(self, client, setup):
        user, _, _, p1, _ = setup
        TaskFactory(project=p1, reporter=user, title="t-a", status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(reverse("web:all_tasks") + "?order=mystery")
        assert resp.status_code == 200
        assert "t-a" in resp.content.decode()


@pytest.mark.django_db
class TestAllTasksQueryCount:
    """N+1 audit — large filtered list stays bounded."""

    def test_no_n_plus_one(self, client, setup):
        user, _, _, p1, _ = setup
        label = LabelFactory(workspace=p1.workspace)
        for i in range(30):
            t = TaskFactory(project=p1, reporter=user, title=f"t{i}", status=Task.STATUS_TODO)
            t.labels.add(label)
        client.force_login(user)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(reverse("web:all_tasks"))
            assert resp.status_code == 200
        assert len(ctx.captured_queries) < 30, f"Got {len(ctx.captured_queries)} queries for 30 tasks — N+1 regression."
