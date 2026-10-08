"""Where an estimate came from, recorded at every door it can come through.

The forecast is allowed to weigh a person's estimate differently from an
agent's guess, which is only safe if the two can be told apart after the
fact. The channel answers it everywhere but MCP, where a number the
person dictated and a number the agent invented arrive identically — so
the tool declares it, and silence is read as the agent.
"""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.mcp.context import mcp_request_scope
from apps.mcp.tools import CALLABLES
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.bulk import _run_bulk_update
from apps.tasks.models import Task
from apps.tasks.services import size_source_for
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def project():
    """Return a project whose workspace owner is its only member."""
    return ProjectFactory(workspace=WorkspaceFactory(owner=UserFactory()))


class TestTheHelper:
    """The one rule, before any caller applies it."""

    def test_no_estimate_has_no_source(self):
        assert size_source_for(None) is None

    def test_off_the_channel_the_web_is_a_person(self):
        assert size_source_for(5) == Task.SIZE_BY_HUMAN

    def test_off_the_channel_mcp_is_the_agent(self):
        with mcp_request_scope():
            assert size_source_for(5) == Task.SIZE_BY_AGENT

    def test_a_tool_may_say_the_person_gave_it(self):
        with mcp_request_scope():
            assert size_source_for(5, from_user=True) == Task.SIZE_BY_HUMAN

    def test_but_silence_is_not_a_claim(self):
        """The whole point of the default: forgetting costs weight, never fakes it."""
        with mcp_request_scope():
            assert size_source_for(5, from_user=None) == Task.SIZE_BY_AGENT


class TestThroughTheApi:
    """Every write that sets a size passes through the serializer."""

    def _client(self, project):
        client = APIClient()
        client.force_authenticate(user=project.workspace.owner)
        return client

    def test_a_size_set_over_the_api_is_a_persons(self, project):
        response = self._client(project).post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "Estimate me", "size": 3},
            format="json",
        )

        assert response.status_code == 201
        assert Task.objects.get(id=response.data["id"]).size_source == Task.SIZE_BY_HUMAN

    def test_clearing_the_size_clears_the_source(self, project):
        task = TaskFactory(project=project, size=8, size_source=Task.SIZE_BY_AGENT)

        response = self._client(project).patch(f"/api/v1/tasks/{task.id}/", {"size": None}, format="json")

        task.refresh_from_db()
        assert response.status_code == 200
        assert task.size is None
        assert task.size_source is None

    def test_the_wire_cannot_award_itself_a_persons_estimate(self, project):
        """Read-only on the serializer: a client that could set it would."""
        response = self._client(project).post(
            "/api/v1/tasks/",
            {"project": project.id, "title": "Nice try", "size": 3, "size_source": Task.SIZE_BY_HUMAN},
            format="json",
        )

        assert response.status_code == 201
        with mcp_request_scope():
            # Same payload, arriving as an agent, is still the agent's.
            second = self._client(project).post(
                "/api/v1/tasks/",
                {"project": project.id, "title": "Nice try twice", "size": 3, "size_source": Task.SIZE_BY_HUMAN},
                format="json",
            )
        assert Task.objects.get(id=second.data["id"]).size_source == Task.SIZE_BY_AGENT


class TestThroughBulk:
    """Bulk writes with ``QuerySet.update``, so nothing derives it downstream."""

    def test_a_bulk_size_carries_its_source(self, project):
        tasks = [TaskFactory(project=project) for _ in range(3)]

        _run_bulk_update(
            user=project.workspace.owner,
            ids=[task.id for task in tasks],
            updates={"size": 5},
        )

        assert {task.size_source for task in Task.objects.filter(id__in=[t.id for t in tasks])} == {
            Task.SIZE_BY_HUMAN,
        }

    def test_a_bulk_clear_clears_the_source_too(self, project):
        task = TaskFactory(project=project, size=13, size_source=Task.SIZE_BY_HUMAN)

        _run_bulk_update(
            user=project.workspace.owner,
            ids=[task.id],
            updates={"size": None},
        )

        task.refresh_from_db()
        assert task.size is None
        assert task.size_source is None


class TestThroughMcp:
    """The only door where the channel cannot answer on its own."""

    def _call(self, name, user, arguments):
        with mcp_request_scope():
            return CALLABLES[name](user, arguments)

    def test_an_agents_own_number_is_marked_as_its_own(self, project):
        task = TaskFactory(project=project)

        self._call("acta_task_update", project.workspace.owner, {"slug": task.slug, "size": 5})

        task.refresh_from_db()
        assert task.size == 5
        assert task.size_source == Task.SIZE_BY_AGENT

    def test_a_number_the_person_gave_is_theirs(self, project):
        task = TaskFactory(project=project)

        self._call(
            "acta_task_update",
            project.workspace.owner,
            {"slug": task.slug, "size": 5, "size_from_user": True},
        )

        task.refresh_from_db()
        assert task.size_source == Task.SIZE_BY_HUMAN

    def test_a_task_created_by_an_agent_keeps_the_agents_mark(self, project):
        self._call(
            "acta_task_create",
            project.workspace.owner,
            {"project": project.slug_prefix, "title": "Guessed at", "size": 8},
        )

        assert Task.objects.get(title="Guessed at").size_source == Task.SIZE_BY_AGENT


class TestThroughTheWeb:
    """A person looking at the task picked the number off the menu."""

    def test_the_inline_picker_credits_the_person(self, client, project):
        task = TaskFactory(project=project)
        client.force_login(project.workspace.owner)

        response = client.post(
            f"/projects/{task.project.slug_prefix}/{task.number}/size/",
            {"size": "5"},
        )

        task.refresh_from_db()
        assert response.status_code == 200
        assert task.size_source == Task.SIZE_BY_HUMAN


class TestTheMarkOnThePage:
    """A guess has to look like one, or nobody will overrule it."""

    def _rail(self, client, project, task):
        client.force_login(project.workspace.owner)
        url = f"/{project.workspace.slug}/projects/{project.slug_prefix}/{task.number}/"
        return client.get(url).content.decode()

    def test_an_agents_estimate_is_marked_and_invites_correction(self, client, project):
        task = TaskFactory(project=project, size=5, size_source=Task.SIZE_BY_AGENT)

        body = self._rail(client, project, task)

        assert "lu-sparkles" in body
        assert "Estimated by an agent" in body

    def test_a_persons_estimate_carries_no_mark(self, client, project):
        task = TaskFactory(project=project, size=5, size_source=Task.SIZE_BY_HUMAN)

        body = self._rail(client, project, task)

        assert "Estimated by an agent" not in body

    def test_an_inherited_estimate_carries_no_mark_either(self, client, project):
        """Null source with a size is legacy, not a claim about an agent."""
        task = TaskFactory(project=project, size=5, size_source=None)

        body = self._rail(client, project, task)

        assert "Estimated by an agent" not in body

    def test_a_person_overruling_it_clears_the_mark(self, client, project):
        task = TaskFactory(project=project, size=5, size_source=Task.SIZE_BY_AGENT)
        client.force_login(project.workspace.owner)

        client.post(f"/projects/{project.slug_prefix}/{task.number}/size/", {"size": "8"})
        task.refresh_from_db()

        assert task.size == 8
        assert task.size_source == Task.SIZE_BY_HUMAN
        assert "Estimated by an agent" not in self._rail(client, project, task)
