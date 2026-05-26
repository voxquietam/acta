"""Integration tests for the read-only ``ActivityLogViewSet``.

Covers workspace scoping, write-method rejection (the log is append-only
via ``log_event``), and the ``event_type`` filter used by the activity
feed.
"""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.activity.models import ActivityLog
from apps.activity.services import log_event
from apps.projects.tests.factories import ProjectFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def member():
    return UserFactory()


@pytest.fixture
def workspace(member):
    return WorkspaceFactory(owner=member)


@pytest.fixture
def client(member):
    c = APIClient()
    c.force_authenticate(member)
    return c


def _make_event(workspace, actor, event_type="task.created"):
    """Append one activity row in ``workspace`` for ``actor``."""
    project = ProjectFactory(workspace=workspace)
    return log_event(
        workspace=workspace,
        project=project,
        actor=actor,
        event_type=event_type,
        target_type=ActivityLog.TARGET_TASK,
        target_id=1,
        payload={},
    )


@pytest.mark.django_db
class TestActivityLogReadOnly:
    def test_list_scoped_to_membership(self, client, member, workspace):
        mine = _make_event(workspace, member)
        foreign_ws = WorkspaceFactory()
        _make_event(foreign_ws, foreign_ws.owner)
        resp = client.get("/api/v1/activity/")
        assert resp.status_code == 200
        ids = {row["id"] for row in resp.data["results"]}
        assert ids == {mine.id}

    def test_retrieve_foreign_event_404(self, client):
        foreign_ws = WorkspaceFactory()
        event = _make_event(foreign_ws, foreign_ws.owner)
        resp = client.get(f"/api/v1/activity/{event.id}/")
        assert resp.status_code == 404

    def test_event_type_filter(self, client, member, workspace):
        created = _make_event(workspace, member, event_type="task.created")
        _make_event(workspace, member, event_type="task.deleted")
        resp = client.get("/api/v1/activity/?event_type=task.created")
        ids = {row["id"] for row in resp.data["results"]}
        assert ids == {created.id}

    def test_write_methods_rejected(self, client, member, workspace):
        event = _make_event(workspace, member)
        assert client.post("/api/v1/activity/", {}).status_code == 405
        assert client.patch(f"/api/v1/activity/{event.id}/", {}).status_code == 405
        assert client.delete(f"/api/v1/activity/{event.id}/").status_code == 405
