"""The relationship graph's payload: what it draws and what it refuses to.

The interesting cases are the ones where naive serialisation would lie:
a symmetrical link counted twice, a blocker in a workspace the viewer
cannot see, and a query count that grows with the project.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web.url_scoping import project_path
from apps.web.views import _graph_context
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


def _scope(project):
    """Return the live-task queryset a project board is built from."""
    return Task.objects.filter(project=project, archived_at__isnull=True)


def _context(project, user):
    """Build the graph context the way the project view does."""
    return _graph_context(_scope(project), user=user, workspace=project.workspace)


def _payload(project, user):
    """Return the graph payload for ``project``."""
    return _context(project, user)["graph_data"]


def _edge_kinds(payload):
    """Return the edge list as ``(kind, source_slug, target_slug)`` tuples."""
    by_id = {n["id"]: n["slug"] for n in payload["nodes"]}
    return sorted((e["kind"], by_id[e["source"]], by_id[e["target"]]) for e in payload["edges"])


@pytest.fixture
def setup(db):
    """A workspace, its owner, and a project to hang tasks on."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws, slug_prefix="GRA")
    return ws, project, ws.owner


@pytest.mark.django_db
class TestGraphPayload:

    def test_every_task_is_sent_with_a_connected_flag(self, setup):
        _, project, user = setup
        a = TaskFactory(project=project)
        b = TaskFactory(project=project)
        lonely = TaskFactory(project=project)
        a.blocks.add(b)
        payload = _payload(project, user)
        flags = {n["slug"]: n["connected"] for n in payload["nodes"]}
        assert flags[a.slug] is True
        assert flags[b.slug] is True
        # Isolated tasks still ship — the client hides them by default and
        # reveals them without another request.
        assert flags[lonely.slug] is False

    def test_blocks_edge_keeps_its_direction(self, setup):
        _, project, user = setup
        a = TaskFactory(project=project)
        b = TaskFactory(project=project)
        a.blocks.add(b)
        assert _edge_kinds(_payload(project, user)) == [("blocks", a.slug, b.slug)]

    def test_symmetrical_link_collapses_to_one_edge(self, setup):
        _, project, user = setup
        a = TaskFactory(project=project)
        b = TaskFactory(project=project)
        a.related.add(b)
        # Django stores both directions for a symmetrical M2M; drawing both
        # would double every related line on the board.
        assert len(_payload(project, user)["edges"]) == 1

    def test_parent_edge_points_from_parent_to_subtask(self, setup):
        _, project, user = setup
        parent = TaskFactory(project=project)
        child = TaskFactory(project=project, parent=parent)
        assert _edge_kinds(_payload(project, user)) == [("parent", parent.slug, child.slug)]

    def test_blocker_in_a_sibling_project_is_drawn_as_external(self, setup):
        ws, project, user = setup
        other = ProjectFactory(workspace=ws, slug_prefix="SIB")
        here = TaskFactory(project=project)
        there = TaskFactory(project=other)
        there.blocks.add(here)
        payload = _payload(project, user)
        external = {n["slug"]: n["external"] for n in payload["nodes"]}
        assert external[there.slug] is True
        assert external[here.slug] is False
        assert _edge_kinds(payload) == [("blocks", there.slug, here.slug)]

    def test_task_from_a_foreign_workspace_is_dropped_with_its_edge(self, setup):
        _, project, user = setup
        here = TaskFactory(project=project)
        stranger = TaskFactory()  # its own workspace, our user is not a member
        stranger.blocks.add(here)
        payload = _payload(project, user)
        assert stranger.slug not in {n["slug"] for n in payload["nodes"]}
        # The edge would otherwise dangle, pointing at a node that is not there.
        assert payload["edges"] == []

    def test_archived_tasks_stay_out(self, setup):
        from django.utils import timezone

        _, project, user = setup
        a = TaskFactory(project=project)
        gone = TaskFactory(project=project, archived_at=timezone.now())
        a.blocks.add(gone)
        payload = _payload(project, user)
        assert gone.slug not in {n["slug"] for n in payload["nodes"]}
        assert payload["edges"] == []

    def test_member_sees_the_graph_too(self, setup):
        ws, project, user = setup
        member = UserFactory()
        WorkspaceMemberFactory(user=member, workspace=ws, role=WorkspaceMember.MEMBER)
        a = TaskFactory(project=project)
        b = TaskFactory(project=project)
        a.related.add(b)
        assert len(_payload(project, member)["nodes"]) == 2

    def test_panel_fetch_renders_the_canvas_host_and_its_data(self, client, setup):
        _, project, user = setup
        a = TaskFactory(project=project)
        b = TaskFactory(project=project)
        a.blocks.add(b)
        client.force_login(user)
        resp = client.get(project_path(project), {"panel": "graph"})
        body = resp.content.decode()
        assert resp.status_code == 200
        # The marker is what triggers the lazy load of the canvas library.
        assert "data-task-graph" in body
        assert 'id="task-graph-data"' in body
        assert a.slug in body

    def test_panel_is_closed_to_non_members(self, client, setup):
        _, project, _ = setup
        client.force_login(UserFactory())
        assert client.get(project_path(project), {"panel": "graph"}).status_code == 404

    def test_query_count_does_not_grow_with_the_project(self, setup):
        _, project, user = setup
        first = TaskFactory(project=project)
        second = TaskFactory(project=project)
        first.blocks.add(second)
        with CaptureQueriesContext(connection) as small:
            _context(project, user)

        bulk = [TaskFactory(project=project) for _ in range(30)]
        for task in bulk:
            first.related.add(task)
        with CaptureQueriesContext(connection) as large:
            payload = _payload(project, user)

        assert len(payload["nodes"]) == 32
        assert len(large.captured_queries) == len(small.captured_queries)


@pytest.mark.django_db
class TestGraphScope:
    """The board is built for a scope — one project, or a whole workspace."""

    def test_workspace_scope_spans_projects(self, setup):
        ws, project, user = setup
        other = ProjectFactory(workspace=ws, slug_prefix="OTH")
        here = TaskFactory(project=project)
        there = TaskFactory(project=other)
        there.blocks.add(here)
        scope = Task.objects.filter(project__workspace=ws, archived_at__isnull=True)
        payload = _graph_context(scope, user=user, workspace=ws)["graph_data"]
        slugs = {n["slug"]: n["external"] for n in payload["nodes"]}
        # Both ends belong to the workspace, so neither is foreign context.
        assert slugs[here.slug] is False
        assert slugs[there.slug] is False
        assert len(payload["edges"]) == 1

    def test_big_scope_sends_only_connected_tasks(self, setup, monkeypatch):
        _, project, user = setup
        monkeypatch.setattr("apps.web.views.GRAPH_FULL_PAYLOAD_LIMIT", 2)
        a = TaskFactory(project=project)
        b = TaskFactory(project=project)
        TaskFactory(project=project)  # isolated — the one that should be left out
        a.blocks.add(b)
        payload = _context(project, user)["graph_data"]
        assert payload["truncated"] is True
        assert {n["slug"] for n in payload["nodes"]} == {a.slug, b.slug}

    def test_the_client_can_ask_for_everything(self, setup, monkeypatch):
        _, project, user = setup
        monkeypatch.setattr("apps.web.views.GRAPH_FULL_PAYLOAD_LIMIT", 2)
        a = TaskFactory(project=project)
        b = TaskFactory(project=project)
        lonely = TaskFactory(project=project)
        a.blocks.add(b)
        payload = _graph_context(
            _scope(project),
            user=user,
            workspace=project.workspace,
            include_all=True,
        )["graph_data"]
        assert payload["truncated"] is False
        assert lonely.slug in {n["slug"] for n in payload["nodes"]}

    def test_all_tasks_serves_the_graph_panel(self, client, setup):
        ws, project, user = setup
        a = TaskFactory(project=project)
        b = TaskFactory(project=project)
        a.related.add(b)
        client.force_login(user)
        resp = client.get(f"/{ws.slug}/tasks/", {"panel": "graph"})
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "data-task-graph" in body
        assert a.slug in body


@pytest.mark.django_db
class TestGraphLink:
    """Links made by dropping a row from the Unlinked list onto a card.

    The board speaks ids and wants the new edge back as data, so this is
    its own endpoint — but the writing, the validation and the activity
    event are the task page's.
    """

    url = "/graph/link/"

    def test_blocks_comes_back_as_the_edge_the_board_draws(self, client, setup):
        _, project, user = setup
        source = TaskFactory(project=project)
        target = TaskFactory(project=project)
        client.force_login(user)
        resp = client.post(self.url, {"kind": "blocks", "source": source.pk, "target": target.pk})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "edge": {"source": source.pk, "target": target.pk, "kind": "blocks"}}
        assert source.blocks.filter(pk=target.pk).exists()

    def test_blocked_by_is_flipped_before_it_is_drawn(self, client, setup):
        _, project, user = setup
        source = TaskFactory(project=project)
        target = TaskFactory(project=project)
        client.force_login(user)
        resp = client.post(self.url, {"kind": "blocked_by", "source": source.pk, "target": target.pk})
        # Down means later on this board, so the blocker is the source.
        assert resp.json()["edge"] == {"source": target.pk, "target": source.pk, "kind": "blocks"}
        assert target.blocks.filter(pk=source.pk).exists()

    def test_a_circular_block_is_refused(self, client, setup):
        _, project, user = setup
        source = TaskFactory(project=project)
        target = TaskFactory(project=project)
        target.blocks.add(source)
        client.force_login(user)
        resp = client.post(self.url, {"kind": "blocks", "source": source.pk, "target": target.pk})
        assert resp.status_code == 400
        assert resp.json()["ok"] is False
        assert not source.blocks.filter(pk=target.pk).exists()

    def test_a_task_from_a_foreign_workspace_is_not_reachable(self, client, setup):
        _, project, user = setup
        source = TaskFactory(project=project)
        stranger = TaskFactory(project=ProjectFactory())
        client.force_login(user)
        resp = client.post(self.url, {"kind": "related", "source": source.pk, "target": stranger.pk})
        assert resp.status_code == 400
        assert not source.related.exists()

    def test_an_unknown_kind_is_refused(self, client, setup):
        _, project, user = setup
        source = TaskFactory(project=project)
        target = TaskFactory(project=project)
        client.force_login(user)
        resp = client.post(self.url, {"kind": "parent", "source": source.pk, "target": target.pk})
        assert resp.status_code == 400

    def test_anonymous_callers_are_sent_to_the_login_page(self, client, setup):
        _, project, _ = setup
        source = TaskFactory(project=project)
        target = TaskFactory(project=project)
        resp = client.post(self.url, {"kind": "related", "source": source.pk, "target": target.pk})
        assert resp.status_code == 302
