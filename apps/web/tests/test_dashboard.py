"""Smoke tests for the workspace dashboard context builder + view.

The dashboard fans out into ~10 aggregate sections; these pin the shape
of the context, the happy-path render, and that the query count stays
bounded (no per-member / per-project N+1)."""

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web.dashboard import build_dashboard_context
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


def _seed(workspace, members=3, projects=2, tasks=12):
    """Populate a workspace with members, projects, and assorted tasks."""
    users = [workspace.owner]
    for _ in range(members - 1):
        u = UserFactory()
        WorkspaceMemberFactory(workspace=workspace, user=u, role=WorkspaceMember.MEMBER)
        users.append(u)
    projs = [ProjectFactory(workspace=workspace) for _ in range(projects)]
    statuses = [
        Task.STATUS_PLANNED,
        Task.STATUS_TODO,
        Task.STATUS_IN_PROGRESS,
        Task.STATUS_IN_REVIEW,
        Task.STATUS_DONE,
    ]
    for i in range(tasks):
        TaskFactory(
            project=projs[i % projects],
            assignee=users[i % len(users)],
            status=statuses[i % len(statuses)],
        )
    return users, projs


@pytest.mark.django_db
class TestDashboardContext:
    def test_context_has_every_section(self):
        ws = WorkspaceFactory()
        _seed(ws)
        ctx = build_dashboard_context(ws, ws.owner, "14d")
        for key in [
            "kpis",
            "alerts",
            "pipeline",
            "cfd",
            "velocity",
            "dist_project",
            "dist_prio",
            "dist_label",
            "members",
            "overloaded",
            "hygiene",
            "heatmap",
        ]:
            assert key in ctx, f"missing {key}"
        assert len(ctx["kpis"]) == 4
        assert len(ctx["cfd"]["weeks"]) == 8
        assert len(ctx["heatmap"]) == 7
        assert len(ctx["hygiene"]) == 5

    def test_all_ranges_build(self):
        ws = WorkspaceFactory()
        _seed(ws)
        for rng in ["7d", "14d", "30d", "90d"]:
            ctx = build_dashboard_context(ws, ws.owner, rng)
            assert ctx["dash_range"] == rng

    def test_empty_workspace_does_not_crash(self):
        ws = WorkspaceFactory()
        ctx = build_dashboard_context(ws, ws.owner, "14d")
        assert ctx["dash_open_total"] == 0
        assert ctx["kpis"][0]["value"] == 0

    def test_done_count_uses_completed_at(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        TaskFactory(project=project, status=Task.STATUS_DONE)  # completed_at stamped on save
        ctx = build_dashboard_context(ws, ws.owner, "30d")
        done_tile = next(k for k in ctx["kpis"] if k["key"] == "done")
        assert done_tile["value"] >= 1


@pytest.mark.django_db
class TestDashboardView:
    def test_member_gets_dashboard(self, settings):
        settings.ALLOWED_HOSTS = ["*"]
        ws = WorkspaceFactory()
        _seed(ws)
        client = Client()
        client.force_login(ws.owner)
        resp = client.get("/?range=30d")
        assert resp.status_code == 200
        assert b"Workspace dashboard" in resp.content
        assert b"matrix-body" in resp.content

    def test_query_count_bounded(self, settings):
        """Adding more members/projects/tasks must not grow the query count."""
        settings.ALLOWED_HOSTS = ["*"]
        ws = WorkspaceFactory()
        _seed(ws, members=6, projects=4, tasks=40)
        client = Client()
        client.force_login(ws.owner)
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get("/?range=30d")
        assert resp.status_code == 200
        assert len(ctx.captured_queries) < 60, len(ctx.captured_queries)
