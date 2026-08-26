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

    def test_slug_plus_title_word_narrows_instead_of_exploding(self, client):
        """``"REPS-42 звіт"`` must AND the slug with the word, not OR them.

        The old parser ran ``rpartition("-")`` over the whole query, so the
        number landed in ``"42 звіт"``, failed ``isdigit()`` and was dropped —
        leaving a bare prefix OR-ed in, which returned the whole project.
        """
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        target = TaskFactory(project=project, title="Квартальний звіт")
        noise = TaskFactory(project=project, title="Щось інше")
        client.force_login(ws.owner)
        found = _search(client, host, q=f"{target.slug} звіт")
        assert target.slug in found
        assert noise.slug not in found

    def test_slug_with_space_instead_of_dash(self, client):
        """``"REPS 42"`` is how people type a key without reaching for the dash."""
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        target = TaskFactory(project=project, title="unrelated title")
        client.force_login(ws.owner)
        assert target.slug in _search(client, host, q=f"REPS {target.number}")

    def test_exact_slug_outranks_fresher_tasks(self, client):
        """An exact slug hit survives truncation however stale it is.

        Ordering by ``-updated_at`` alone buried the match under whatever was
        touched most recently, which reads to users as "the task isn't there".
        """
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        target = TaskFactory(project=project, title="unrelated title")
        # Every one of these is touched after the target, and each matches the
        # bare-prefix reading of the query.
        for i in range(30):
            TaskFactory(project=project, title=f"noise {i}")
        client.force_login(ws.owner)
        url = reverse("web:task_link_search", args=[host.project.slug_prefix, host.number])
        results = client.get(url, {"q": target.slug}).json()["results"]
        assert results[0]["slug"] == target.slug

    def test_multi_word_title_search_is_unaffected_by_a_trailing_number(self, client):
        """``"sprint 3"`` is title text, not a slug — both readings must work."""
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws, slug_prefix="REPS")
        host = TaskFactory(project=project, title="Host")
        target = TaskFactory(project=project, title="Sprint 3 planning")
        client.force_login(ws.owner)
        assert target.slug in _search(client, host, q="sprint 3")
