"""The create dialog's "does this already exist?" fragment.

Everything here is about restraint: it answers nothing until the title is
worth a lookup, it never leaks tasks from a workspace the caller is not
in, and it renders empty rather than erroring when the embedding host is
absent.
"""

from django.test import override_settings

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks import similarity
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory

URL = "/tasks/similar/"
ENABLED = override_settings(
    ACTA_EMBEDDING_URL="http://embed.test:11434",
    ACTA_EMBEDDING_MODEL="test-model",
)


@pytest.fixture
def setup(db):
    """A workspace, its owner and a project."""
    workspace = WorkspaceFactory()
    project = ProjectFactory(workspace=workspace, slug_prefix="HNT")
    return workspace, project, workspace.owner


@pytest.fixture(autouse=True)
def clear_cache():
    """Drop the per-workspace matrix cache between tests."""
    similarity._CACHE.clear()
    yield
    similarity._CACHE.clear()


def stub(monkeypatch, mapping):
    """Point :func:`similarity.embed` at a lookup table."""
    monkeypatch.setattr(
        similarity,
        "embed",
        lambda texts, timeout=None: [mapping[text.split("\n")[0]] for text in texts],
    )


@pytest.mark.django_db
class TestSimilarTasksHint:

    @ENABLED
    def test_a_near_duplicate_is_offered(self, client, setup, monkeypatch):
        _, project, user = setup
        existing = TaskFactory(project=project, title="Налаштувати бекапи")
        mapping = {"Налаштувати бекапи": [1.0, 0.0], "настроить бекапы": [1.0, 0.0]}
        stub(monkeypatch, mapping)
        similarity.store([existing])
        stub(monkeypatch, mapping)

        client.force_login(user)
        body = client.get(URL, {"title": "настроить бекапы", "project": "HNT"}).content.decode()
        assert existing.slug in body
        assert existing.title in body

    @ENABLED
    def test_a_distant_task_is_not_offered(self, client, setup, monkeypatch):
        _, project, user = setup
        existing = TaskFactory(project=project, title="Налаштувати бекапи")
        mapping = {"Налаштувати бекапи": [1.0, 0.0], "купить кофе": [0.0, 1.0]}
        stub(monkeypatch, mapping)
        similarity.store([existing])
        stub(monkeypatch, mapping)

        client.force_login(user)
        body = client.get(URL, {"title": "купить кофе", "project": "HNT"}).content.decode()
        assert existing.slug not in body
        assert body.strip() == ""

    @ENABLED
    def test_a_short_title_asks_nothing(self, client, setup, monkeypatch):
        _, _, user = setup

        def _never(texts, timeout=None):
            raise AssertionError("the host must not be asked about three letters")

        monkeypatch.setattr(similarity, "embed", _never)
        client.force_login(user)
        assert client.get(URL, {"title": "бек", "project": "HNT"}).content.decode().strip() == ""

    @ENABLED
    def test_a_project_the_caller_cannot_see_answers_nothing(self, client, setup, monkeypatch):
        _, project, _ = setup
        TaskFactory(project=project, title="Налаштувати бекапи")
        stub(monkeypatch, {"Налаштувати бекапи": [1.0, 0.0]})
        client.force_login(UserFactory())
        assert client.get(URL, {"title": "настроить бекапы", "project": "HNT"}).content.decode().strip() == ""

    def test_without_an_embedding_host_it_renders_empty(self, client, setup):
        _, project, user = setup
        TaskFactory(project=project, title="Налаштувати бекапи")
        client.force_login(user)
        with override_settings(ACTA_EMBEDDING_URL=""):
            assert client.get(URL, {"title": "настроить бекапы", "project": "HNT"}).content.decode().strip() == ""

    def test_anonymous_callers_are_sent_to_the_login_page(self, client, setup):
        assert client.get(URL, {"title": "настроить бекапы", "project": "HNT"}).status_code == 302


@pytest.mark.django_db
class TestLinkPickerSuggestions:
    """An empty search box offers what looks related instead of nothing."""

    @ENABLED
    def test_an_empty_query_answers_with_neighbours(self, client, setup, monkeypatch):
        _, project, user = setup
        anchor = TaskFactory(project=project, title="Налаштувати бекапи")
        near = TaskFactory(project=project, title="Перевірити бекапи")
        TaskFactory(project=project, title="Купити каву")
        mapping = {
            "Налаштувати бекапи": [1.0, 0.0],
            "Перевірити бекапи": [0.99, 0.14],
            "Купити каву": [0.0, 1.0],
        }
        stub(monkeypatch, mapping)
        similarity.store([anchor, near, Task.objects.get(title="Купити каву")])

        client.force_login(user)
        payload = client.get(
            f"/projects/{project.slug_prefix}/{anchor.number}/links/search/",
            {"q": ""},
        ).json()
        assert payload["suggested"] is True
        assert [row["slug"] for row in payload["results"]] == [near.slug]

    @ENABLED
    def test_an_already_linked_task_is_not_suggested(self, client, setup, monkeypatch):
        _, project, user = setup
        anchor = TaskFactory(project=project, title="Налаштувати бекапи")
        near = TaskFactory(project=project, title="Перевірити бекапи")
        anchor.related.add(near)
        mapping = {"Налаштувати бекапи": [1.0, 0.0], "Перевірити бекапи": [0.99, 0.14]}
        stub(monkeypatch, mapping)
        similarity.store([anchor, near])

        client.force_login(user)
        payload = client.get(
            f"/projects/{project.slug_prefix}/{anchor.number}/links/search/",
            {"q": ""},
        ).json()
        assert payload["results"] == []

    def test_without_a_host_an_empty_query_stays_empty(self, client, setup):
        _, project, user = setup
        anchor = TaskFactory(project=project, title="Налаштувати бекапи")
        TaskFactory(project=project, title="Перевірити бекапи")
        client.force_login(user)
        payload = client.get(
            f"/projects/{project.slug_prefix}/{anchor.number}/links/search/",
            {"q": ""},
        ).json()
        assert payload["results"] == []
        assert payload["suggested"] is False


@pytest.mark.django_db
class TestAssigneeSuggestion:
    """The assignee dropdown's "usually does this" group."""

    @ENABLED
    def test_a_repeat_assignee_is_offered_on_an_unassigned_task(self, client, setup, monkeypatch):
        from apps.accounts.tests.factories import UserFactory
        from apps.workspaces.models import WorkspaceMember

        workspace, project, user = setup
        regular = UserFactory()
        WorkspaceMember.objects.create(user=regular, workspace=workspace)
        first = TaskFactory(project=project, title="Налаштувати бекапи", assignee=regular)
        second = TaskFactory(project=project, title="Перевірити бекапи", assignee=regular)
        anchor = TaskFactory(project=project, title="Відновити бекапи")
        mapping = {
            "Налаштувати бекапи": [1.0, 0.0],
            "Перевірити бекапи": [1.0, 0.0],
            "Відновити бекапи": [1.0, 0.0],
        }
        stub(monkeypatch, mapping)
        similarity.store([first, second, anchor])

        client.force_login(user)
        body = client.get(f"/projects/{project.slug_prefix}/{anchor.number}/meta/").content.decode()
        assert "Usually does this" in body
        assert regular.display_name in body

    @ENABLED
    def test_an_assigned_task_is_not_second_guessed(self, client, setup, monkeypatch):
        from apps.accounts.tests.factories import UserFactory
        from apps.workspaces.models import WorkspaceMember

        workspace, project, user = setup
        regular = UserFactory()
        WorkspaceMember.objects.create(user=regular, workspace=workspace)
        first = TaskFactory(project=project, title="Налаштувати бекапи", assignee=regular)
        second = TaskFactory(project=project, title="Перевірити бекапи", assignee=regular)
        anchor = TaskFactory(project=project, title="Відновити бекапи", assignee=user)
        mapping = {
            "Налаштувати бекапи": [1.0, 0.0],
            "Перевірити бекапи": [1.0, 0.0],
            "Відновити бекапи": [1.0, 0.0],
        }
        stub(monkeypatch, mapping)
        similarity.store([first, second, anchor])

        client.force_login(user)
        body = client.get(f"/projects/{project.slug_prefix}/{anchor.number}/meta/").content.decode()
        # Someone already owns it; the guess would only be noise.
        assert "Usually does this" not in body
