"""Semantic neighbours: what gets embedded, what gets compared, what never does.

The embedding host is stubbed throughout — these tests are about the
rules around the model, not the model. The interesting cases are the
ones where a naive implementation quietly lies: vectors built by a
different model, copies a recurring rule stamped out, and a host that is
simply not there.
"""

from django.test import override_settings

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.recurring.tests.factories import RecurringTaskFactory
from apps.tasks import similarity
from apps.tasks.models import Task, TaskEmbedding
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory

ENABLED = override_settings(
    ACTA_EMBEDDING_URL="http://embed.test:11434",
    ACTA_EMBEDDING_MODEL="test-model",
)


def fake_embed(vectors_by_text):
    """Return a stub for :func:`similarity.embed` driven by a lookup table."""

    def _embed(texts, timeout=None):
        return [vectors_by_text[text.split("\n")[0]] for text in texts]

    return _embed


@pytest.fixture
def setup(db):
    """A workspace with a project to hang tasks on."""
    workspace = WorkspaceFactory()
    project = ProjectFactory(workspace=workspace, slug_prefix="SIM")
    return workspace, project


@pytest.fixture(autouse=True)
def clear_cache():
    """Drop the per-workspace matrix cache between tests."""
    similarity._CACHE.clear()
    yield
    similarity._CACHE.clear()


def embed_tasks(monkeypatch, mapping, tasks):
    """Store vectors for ``tasks`` using a stubbed model."""
    monkeypatch.setattr(similarity, "embed", fake_embed(mapping))
    return similarity.store(tasks)


@pytest.mark.django_db
class TestStoring:

    @ENABLED
    def test_a_vector_is_written_once_and_not_again(self, setup, monkeypatch):
        _, project = setup
        task = TaskFactory(project=project, title="alpha")
        assert embed_tasks(monkeypatch, {"alpha": [1.0, 0.0]}, [task]) == 1
        assert TaskEmbedding.objects.get(task=task).dimensions == 2
        # Same text, so the second pass must not spend a round trip.
        assert embed_tasks(monkeypatch, {"alpha": [1.0, 0.0]}, [task]) == 0

    @ENABLED
    def test_editing_the_title_rebuilds_the_vector(self, setup, monkeypatch):
        _, project = setup
        task = TaskFactory(project=project, title="alpha")
        embed_tasks(monkeypatch, {"alpha": [1.0, 0.0]}, [task])
        first = TaskEmbedding.objects.get(task=task).text_hash
        task.title = "beta"
        task.save(update_fields=["title"])
        assert embed_tasks(monkeypatch, {"beta": [0.0, 1.0]}, [task]) == 1
        assert TaskEmbedding.objects.get(task=task).text_hash != first

    @ENABLED
    def test_archived_tasks_are_not_embedded(self, setup, monkeypatch):
        from django.utils import timezone

        _, project = setup
        task = TaskFactory(project=project, title="alpha", archived_at=timezone.now())
        assert embed_tasks(monkeypatch, {"alpha": [1.0, 0.0]}, [task]) == 0
        assert not TaskEmbedding.objects.exists()

    def test_nothing_is_embedded_without_a_host(self, setup, monkeypatch):
        _, project = setup
        task = TaskFactory(project=project, title="alpha")
        with override_settings(ACTA_EMBEDDING_URL=""):
            with pytest.raises(similarity.EmbeddingUnavailable):
                similarity.store([task])


@pytest.mark.django_db
class TestNeighbours:

    @ENABLED
    def test_the_closest_task_comes_first(self, setup, monkeypatch):
        workspace, project = setup
        near = TaskFactory(project=project, title="near")
        far = TaskFactory(project=project, title="far")
        vectors = {"near": [1.0, 0.0], "far": [0.0, 1.0], "query": [0.96, 0.28]}
        embed_tasks(monkeypatch, vectors, [near, far])
        monkeypatch.setattr(similarity, "embed", fake_embed(vectors))
        found = similarity.neighbours_of_text("query", workspace_id=workspace.id, limit=5)
        assert [task_id for task_id, _ in found] == [near.pk]

    @ENABLED
    def test_a_recurring_copy_is_never_a_neighbour(self, setup, monkeypatch):
        workspace, project = setup
        rule = RecurringTaskFactory(project=project, title="weekly")
        generated = TaskFactory(project=project, title="near", recurrence=rule)
        ordinary = TaskFactory(project=project, title="near-too")
        vectors = {"near": [1.0, 0.0], "near-too": [0.99, 0.14], "query": [1.0, 0.0]}
        embed_tasks(monkeypatch, vectors, [generated, ordinary])
        monkeypatch.setattr(similarity, "embed", fake_embed(vectors))
        found = similarity.neighbours_of_text("query", workspace_id=workspace.id, limit=5)
        # The generated copy is the better match and still must not appear:
        # a weekly task has dozens of identical siblings by design.
        assert [task_id for task_id, _ in found] == [ordinary.pk]

    @ENABLED
    def test_vectors_from_another_model_are_ignored(self, setup, monkeypatch):
        workspace, project = setup
        task = TaskFactory(project=project, title="near")
        vectors = {"near": [1.0, 0.0], "query": [1.0, 0.0]}
        embed_tasks(monkeypatch, vectors, [task])
        TaskEmbedding.objects.update(model="some-other-model")
        monkeypatch.setattr(similarity, "embed", fake_embed(vectors))
        assert similarity.neighbours_of_text("query", workspace_id=workspace.id) == []

    @ENABLED
    def test_a_task_is_not_its_own_neighbour(self, setup, monkeypatch):
        _, project = setup
        task = TaskFactory(project=project, title="near")
        other = TaskFactory(project=project, title="near-too")
        vectors = {"near": [1.0, 0.0], "near-too": [0.99, 0.14]}
        embed_tasks(monkeypatch, vectors, [task, other])
        monkeypatch.setattr(similarity, "embed", fake_embed(vectors))
        found = similarity.neighbours_of_task(task)
        assert [task_id for task_id, _ in found] == [other.pk]

    @ENABLED
    def test_an_unreachable_host_is_not_an_error(self, setup, monkeypatch):
        workspace, project = setup
        task = TaskFactory(project=project, title="near")
        embed_tasks(monkeypatch, {"near": [1.0, 0.0]}, [task])

        def _boom(texts, timeout=None):
            raise similarity.EmbeddingUnavailable("connection refused")

        monkeypatch.setattr(similarity, "embed", _boom)
        # A side feature must not take the page down with it.
        assert similarity.neighbours_of_text("query", workspace_id=workspace.id) == []

    def test_the_feature_is_silent_when_off(self, setup):
        workspace, _ = setup
        with override_settings(ACTA_EMBEDDING_URL=""):
            assert similarity.neighbours_of_text("anything", workspace_id=workspace.id) == []

    @ENABLED
    def test_a_new_vector_invalidates_the_cached_matrix(self, setup, monkeypatch):
        workspace, project = setup
        first = TaskFactory(project=project, title="near")
        vectors = {"near": [1.0, 0.0], "near-too": [1.0, 0.0], "query": [1.0, 0.0]}
        embed_tasks(monkeypatch, vectors, [first])
        monkeypatch.setattr(similarity, "embed", fake_embed(vectors))
        assert len(similarity.neighbours_of_text("query", workspace_id=workspace.id, limit=5)) == 1

        second = TaskFactory(project=project, title="near-too")
        embed_tasks(monkeypatch, vectors, [second])
        monkeypatch.setattr(similarity, "embed", fake_embed(vectors))
        assert len(similarity.neighbours_of_text("query", workspace_id=workspace.id, limit=5)) == 2


@pytest.mark.django_db
class TestRebuildTrigger:
    """Which saves are worth a worker job and which are not."""

    @ENABLED
    def test_a_text_edit_queues_a_rebuild(self, setup, monkeypatch):
        _, project = setup
        task = TaskFactory(project=project)
        queued = []
        monkeypatch.setattr(similarity, "schedule", lambda t: queued.append(t.pk))
        similarity.on_task_saved(Task, task, created=False, update_fields={"title"})
        assert queued == [task.pk]

    @ENABLED
    def test_a_status_change_does_not(self, setup, monkeypatch):
        _, project = setup
        task = TaskFactory(project=project)
        queued = []
        monkeypatch.setattr(similarity, "schedule", lambda t: queued.append(t.pk))
        # A kanban drag writes status and order dozens of times a minute;
        # each would otherwise cost a job whose outcome is "unchanged".
        similarity.on_task_saved(Task, task, created=False, update_fields={"status", "order"})
        assert queued == []

    @ENABLED
    def test_a_save_that_names_no_fields_is_assumed_to_touch_the_text(self, setup, monkeypatch):
        _, project = setup
        task = TaskFactory(project=project)
        queued = []
        monkeypatch.setattr(similarity, "schedule", lambda t: queued.append(t.pk))
        similarity.on_task_saved(Task, task, created=False, update_fields=None)
        assert queued == [task.pk]

    def test_nothing_is_queued_when_the_feature_is_off(self, setup, monkeypatch):
        _, project = setup
        task = TaskFactory(project=project)
        queued = []
        monkeypatch.setattr(similarity, "schedule", lambda t: queued.append(t.pk))
        with override_settings(ACTA_EMBEDDING_URL=""):
            similarity.on_task_saved(Task, task, created=True)
        assert queued == []
