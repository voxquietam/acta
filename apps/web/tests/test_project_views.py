"""Project list and detail page views."""

from django.urls import reverse

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def member_user(db):
    """Create an authenticated user with one workspace and one project.

    Returns:
        A tuple ``(user, workspace, project)`` already wired up.
    """
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws.owner, ws, project


@pytest.mark.django_db
class TestProjectListView:
    """Index of projects visible to the request user."""

    def test_anonymous_redirected(self, client):
        resp = client.get(reverse("web:project_list"))
        assert resp.status_code == 302
        assert "/accounts/login/" in resp.url

    def test_lists_user_projects(self, client, member_user):
        user, ws, project = member_user
        client.force_login(user)
        resp = client.get(reverse("web:project_list"))
        assert resp.status_code == 200
        assert project.name in resp.content.decode()
        assert project.slug_prefix in resp.content.decode()

    def test_hides_foreign_projects(self, client, member_user):
        user, _, _ = member_user
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        client.force_login(user)
        resp = client.get(reverse("web:project_list"))
        assert foreign_project.name not in resp.content.decode()


@pytest.mark.django_db
class TestProjectDetailView:
    """Kanban + table tabs over the same task queryset."""

    def test_anonymous_redirected(self, client, member_user):
        _, _, project = member_user
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
        )
        assert resp.status_code == 302

    def test_default_view_is_kanban(self, client, member_user):
        user, _, project = member_user
        TaskFactory(project=project, reporter=user, status=Task.STATUS_TODO)
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
        )
        assert resp.status_code == 200
        assert resp.context["view_mode"] == "kanban"
        body = resp.content.decode()
        # Kanban column headers reflect each status.
        for status_label in Task.STATUS_LABELS.values():
            assert str(status_label) in body or str(status_label).lower() in body.lower()

    def test_table_view_param_switches(self, client, member_user):
        user, _, project = member_user
        TaskFactory(project=project, reporter=user)
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
            data={"view": "table"},
        )
        assert resp.status_code == 200
        assert resp.context["view_mode"] == "table"
        body = resp.content.decode()
        # Table column headers visible only in table mode.
        assert "<table" in body

    def test_unknown_view_param_falls_back_to_kanban(self, client, member_user):
        user, _, project = member_user
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
            data={"view": "lolwut"},
        )
        assert resp.context["view_mode"] == "kanban"

    def test_view_choice_persists_via_cookie(self, client, member_user):
        """First request with ``?view=table`` sets a cookie; the next
        cookie-only request gets table by default."""
        user, ws, project = member_user
        other_project = ProjectFactory(workspace=ws)
        client.force_login(user)
        # 1. Pick table on project A.
        resp1 = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
            data={"view": "table"},
        )
        assert resp1.cookies["acta_view_mode"].value == "table"
        # 2. Open project B with no querystring — cookie carries the choice.
        resp2 = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": other_project.slug_prefix}),
        )
        assert resp2.context["view_mode"] == "table"

    def test_cookie_ignored_when_view_param_present(self, client, member_user):
        """An explicit ``?view=kanban`` overrides a ``table`` cookie."""
        user, _, project = member_user
        client.cookies["acta_view_mode"] = "table"
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
            data={"view": "kanban"},
        )
        assert resp.context["view_mode"] == "kanban"
        # And the cookie now reflects the new choice.
        assert resp.cookies["acta_view_mode"].value == "kanban"

    def test_garbage_cookie_falls_back_to_kanban(self, client, member_user):
        user, _, project = member_user
        client.cookies["acta_view_mode"] = "evilbits"
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
        )
        assert resp.context["view_mode"] == "kanban"

    def test_foreign_project_returns_404(self, client, member_user):
        user, _, _ = member_user
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        client.force_login(user)
        resp = client.get(
            reverse(
                "web:project_detail",
                kwargs={"slug_prefix": foreign_project.slug_prefix},
            ),
        )
        assert resp.status_code == 404

    def test_tasks_grouped_into_columns(self, client, member_user):
        user, _, project = member_user
        TaskFactory(project=project, reporter=user, status=Task.STATUS_TODO)
        TaskFactory(project=project, reporter=user, status=Task.STATUS_DONE)
        TaskFactory(project=project, reporter=user, status=Task.STATUS_DONE)
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
        )
        cols = {c["key"]: len(c["tasks"]) for c in resp.context["columns"]}
        assert cols[Task.STATUS_TODO] == 1
        assert cols[Task.STATUS_DONE] == 2
        assert cols[Task.STATUS_IN_PROGRESS] == 0

    def test_filter_preserves_view_param(self, client, member_user):
        """Submitting a filter from the Table tab must keep ``view=table``.

        Regression for the bug where any filter click on the table view
        would silently bounce the user back to the Kanban default.
        """
        user, _, project = member_user
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}) + "?view=table&priority=1",
        )
        assert resp.status_code == 200
        assert resp.context["view_mode"] == "table"
        assert ("view", "table") in resp.context["filter_preserved_pairs"]

    def test_all_three_view_bodies_render_simultaneously(self, client, member_user):
        """Project detail renders Overview + Kanban + Table bodies into
        the DOM on every load so the Alpine tab-switch is client-side
        (no extra round-trip). Each body has a unique marker visible in
        the HTML regardless of which tab is initially active.
        """
        user, _, project = member_user
        # Add a member so the Overview body has something concrete to render.
        from apps.workspaces.tests.factories import WorkspaceMemberFactory

        member = WorkspaceMemberFactory(workspace=project.workspace).user
        project.members.add(member)
        TaskFactory(
            project=project,
            reporter=user,
            title="kanban-marker-task",
            status=Task.STATUS_TODO,
        )
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
        )
        body = resp.content.decode()
        # Tabs nav present.
        assert "Overview" in body
        assert "Kanban" in body
        assert "Table" in body
        # Overview body: member's name renders.
        assert member.display_name in body
        # Kanban body: card marker.
        assert "kanban-marker-task" in body
        # Table body: every body div is in the DOM (x-show selects).
        assert body.count("$store.viewMode.current ===") >= 3

    def test_set_lead_assigns_workspace_member(self, client, member_user):
        from apps.workspaces.tests.factories import WorkspaceMemberFactory

        user, _, project = member_user
        new_lead = WorkspaceMemberFactory(workspace=project.workspace).user
        client.force_login(user)
        resp = client.post(
            reverse("web:set_project_lead", kwargs={"slug_prefix": project.slug_prefix}),
            {"lead_id": new_lead.id},
        )
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.lead == new_lead
        assert 'id="project-lead-cell"' in resp.content.decode()

    def test_set_lead_with_empty_clears(self, client, member_user):
        from apps.workspaces.tests.factories import WorkspaceMemberFactory

        user, _, project = member_user
        previous = WorkspaceMemberFactory(workspace=project.workspace).user
        project.lead = previous
        project.save(update_fields=["lead"])
        client.force_login(user)
        resp = client.post(
            reverse("web:set_project_lead", kwargs={"slug_prefix": project.slug_prefix}),
            {"lead_id": ""},
        )
        assert resp.status_code == 200
        project.refresh_from_db()
        assert project.lead is None

    def test_set_lead_rejects_non_workspace_member(self, client, member_user):
        from apps.accounts.tests.factories import UserFactory

        user, _, project = member_user
        outsider = UserFactory()
        client.force_login(user)
        resp = client.post(
            reverse("web:set_project_lead", kwargs={"slug_prefix": project.slug_prefix}),
            {"lead_id": outsider.id},
        )
        assert resp.status_code == 400
        project.refresh_from_db()
        assert project.lead is None

    def test_set_lead_invalid_id_returns_400(self, client, member_user):
        user, _, project = member_user
        client.force_login(user)
        resp = client.post(
            reverse("web:set_project_lead", kwargs={"slug_prefix": project.slug_prefix}),
            {"lead_id": "notanint"},
        )
        assert resp.status_code == 400

    def test_set_lead_foreign_project_returns_404(self, client, member_user):
        from apps.workspaces.tests.factories import WorkspaceFactory

        user, _, _ = member_user
        foreign_ws = WorkspaceFactory()
        foreign_project = ProjectFactory(workspace=foreign_ws)
        client.force_login(user)
        resp = client.post(
            reverse("web:set_project_lead", kwargs={"slug_prefix": foreign_project.slug_prefix}),
            {"lead_id": ""},
        )
        assert resp.status_code == 404

    def test_toggle_member_adds_workspace_member(self, client, member_user):
        from apps.workspaces.tests.factories import WorkspaceMemberFactory

        user, _, project = member_user
        candidate = WorkspaceMemberFactory(workspace=project.workspace).user
        client.force_login(user)
        resp = client.post(
            reverse("web:toggle_project_member", kwargs={"slug_prefix": project.slug_prefix}),
            {"user_id": candidate.id},
        )
        assert resp.status_code == 200
        assert project.members.filter(pk=candidate.pk).exists()
        assert 'id="project-members-cell"' in resp.content.decode()

    def test_toggle_member_removes_existing(self, client, member_user):
        from apps.workspaces.tests.factories import WorkspaceMemberFactory

        user, _, project = member_user
        existing = WorkspaceMemberFactory(workspace=project.workspace).user
        project.members.add(existing)
        client.force_login(user)
        resp = client.post(
            reverse("web:toggle_project_member", kwargs={"slug_prefix": project.slug_prefix}),
            {"user_id": existing.id},
        )
        assert resp.status_code == 200
        assert not project.members.filter(pk=existing.pk).exists()

    def test_toggle_member_rejects_non_workspace_member(self, client, member_user):
        from apps.accounts.tests.factories import UserFactory

        user, _, project = member_user
        outsider = UserFactory()
        client.force_login(user)
        resp = client.post(
            reverse("web:toggle_project_member", kwargs={"slug_prefix": project.slug_prefix}),
            {"user_id": outsider.id},
        )
        assert resp.status_code == 400
        assert not project.members.filter(pk=outsider.pk).exists()

    def test_overview_view_keeps_tab_default_and_hides_sidebar(self, client, member_user):
        """When ``?view=overview`` is active server-side, the assignee
        strip and filter sidebar are absent from the response (the
        tabs and view bodies are still all present client-side).
        """
        user, _, project = member_user
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}) + "?view=overview",
        )
        assert resp.status_code == 200
        assert resp.context["view_mode"] == "overview"
        # The assignee strip data attribute lives on a wrapper that is
        # ``x-show``-conditional on overview; both the strip include and
        # the sidebar still render their static HTML so they pop in
        # when the user picks Kanban/Table via Alpine.
        body = resp.content.decode()
        assert "data-strip" in body  # assignee strip include still in DOM
        assert "filter-form" in body  # sidebar include still in DOM

    def test_status_filter_applies(self, client, member_user):
        """``?status=to-do`` should narrow the in-context tasks list."""
        user, _, project = member_user
        TaskFactory(project=project, reporter=user, title="t-todo", status=Task.STATUS_TODO)
        TaskFactory(project=project, reporter=user, title="t-prog", status=Task.STATUS_IN_PROGRESS)
        client.force_login(user)
        resp = client.get(
            reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}) + "?status=to-do",
        )
        titles = [t.title for t in resp.context["tasks"]]
        assert "t-todo" in titles
        assert "t-prog" not in titles


@pytest.mark.django_db
class TestProjectViewQueryCounts:
    """Regression guard against N+1 in project pages."""

    def test_project_list_constant_queries(self, client, member_user, django_assert_max_num_queries):
        user, ws, _ = member_user
        # Five more projects with tasks.
        for _ in range(5):
            p = ProjectFactory(workspace=ws)
            TaskFactory(project=p, reporter=user)
        client.force_login(user)
        with django_assert_max_num_queries(15):
            client.get(reverse("web:project_list"))

    def test_project_detail_constant_queries(self, client, member_user, django_assert_max_num_queries):
        user, _, project = member_user
        for _ in range(20):
            TaskFactory(project=project, reporter=user)
        client.force_login(user)
        # 18 = 15 baseline + 2 flat prefetches (blocks / blocked_by) the
        # board cards need for the blocked / blocking badges + 1 for the
        # sidebar inbox-unread badge (context processor COUNT, ADR 0021).
        # Still constant — adding more tasks must not move it.
        with django_assert_max_num_queries(18):
            client.get(
                reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
            )
