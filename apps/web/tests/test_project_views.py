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
        with django_assert_max_num_queries(15):
            client.get(
                reverse("web:project_detail", kwargs={"slug_prefix": project.slug_prefix}),
            )
