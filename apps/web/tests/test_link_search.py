"""Link-target picker typeahead (web:task_link_search).

Covers the forgiving matching the picker relies on: order-independent
word matching on the title, and slug matching by full key, bare prefix,
prefix-with-dash, or bare number — so a half-typed query still surfaces
the target. Regression guard for "I can't find the task by slug" reports.
"""

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


def _search(client, host, **params):
    url = reverse("web:task_link_search", args=[host.project.slug_prefix, host.number])
    return {t["slug"] for t in client.get(url, params).json()["results"]}


@pytest.mark.django_db
class TestTaskLinkSearch:
    def test_title_words_order_independent(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host task")
        target = TaskFactory(project=project, title="Підготувати шаблон звіту по ДВВ")
        client.force_login(ws.owner)
        # Words appear out of order and with a gap in the title ("звіту по ДВВ").
        assert target.slug in _search(client, host, q="звіт двв")

    def test_match_by_full_slug(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        target = TaskFactory(project=project, title="unrelated title")
        client.force_login(ws.owner)
        assert target.slug in _search(client, host, q=target.slug)

    def test_match_by_bare_prefix(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        target = TaskFactory(project=project, title="unrelated title")
        client.force_login(ws.owner)
        # Typing just the project key (no number) still surfaces its tasks.
        assert target.slug in _search(client, host, q="rep")

    def test_match_by_prefix_with_trailing_dash(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        target = TaskFactory(project=project, title="unrelated title")
        client.force_login(ws.owner)
        assert target.slug in _search(client, host, q="REPS-")

    def test_match_by_bare_number(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        target = TaskFactory(project=project, title="unrelated title")
        client.force_login(ws.owner)
        assert target.slug in _search(client, host, q=str(target.number))

    def test_match_by_assignee_full_name_order_independent(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        assignee = UserFactory(username="dsen", first_name="Денис Олександрович", last_name="Сенчишен")
        target = TaskFactory(project=project, title="unrelated title", assignee=assignee)
        client.force_login(ws.owner)
        # Surname first, given name second — order must not matter.
        assert target.slug in _search(client, host, q="сенчишен денис")

    def test_match_by_assignee_username(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        assignee = UserFactory(username="breynolds", first_name="Burt", last_name="Reynolds")
        target = TaskFactory(project=project, title="unrelated title", assignee=assignee)
        client.force_login(ws.owner)
        assert target.slug in _search(client, host, q="breyn")

    def test_assignee_name_does_not_overmatch(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        assignee = UserFactory(username="dsen", first_name="Денис", last_name="Сенчишен")
        target = TaskFactory(project=project, title="unrelated title", assignee=assignee)
        client.force_login(ws.owner)
        # A name word paired with a word matching neither title nor name
        # must not surface the task (AND semantics).
        assert target.slug not in _search(client, host, q="денис петренко")

    def test_already_linked_excluded(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        target = TaskFactory(project=project, title="unrelated title")
        host.related.add(target)
        client.force_login(ws.owner)
        assert target.slug not in _search(client, host, q=target.slug)

    def test_other_workspace_excluded(self, client):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        foreign = TaskFactory(project=ProjectFactory(slug_prefix="REPS"), title="unrelated title")
        client.force_login(ws.owner)
        assert foreign.slug not in _search(client, host, q=foreign.slug)
