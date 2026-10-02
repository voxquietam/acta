"""The payload the floating filter dock renders from.

The dock is a presentation layer: what matters on the server side is
that every axis the rail used to offer still travels, that the current
selection comes back marked, and that the page stops shipping the rail.
"""

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.labels.tests.factories import LabelFactory
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    """A workspace with a member, a project and one task."""
    workspace = WorkspaceFactory()
    user = UserFactory()
    WorkspaceMember.objects.create(user=user, workspace=workspace)
    project = ProjectFactory(workspace=workspace, slug_prefix="DCK")
    TaskFactory(project=project, reporter=user)
    # An axis with no options is dropped from the payload, so the
    # workspace needs at least one label for the label axis to exist.
    LabelFactory(workspace=workspace, name="seed")
    return workspace, project, user


def dock_of(client, url):
    """Return the dock payload embedded in a rendered page."""
    import json
    import re

    body = client.get(url).content.decode()
    match = re.search(r'<script id="filter-dock-data" type="application/json">(.*?)</script>', body, re.S)
    assert match, "the page carries no dock payload"
    return json.loads(match.group(1))


@pytest.mark.django_db
class TestDockPayload:

    def test_every_axis_the_rail_had_still_travels(self, client, setup):
        _, _, user = setup
        client.force_login(user)
        data = dock_of(client, "/tasks/")
        keys = [f["key"] for f in data["fields"]]
        assert "status" in keys
        assert "priority" in keys
        assert "label" in keys
        assert "project" in keys
        assert "size" in keys
        assert "date" in keys

    def test_assignee_is_not_an_axis(self, client, setup):
        _, _, user = setup
        client.force_login(user)
        data = dock_of(client, "/tasks/")
        # The avatar strip above the list owns that one; two controls for
        # one axis is how people filter by someone twice.
        assert "assignee" not in [f["key"] for f in data["fields"]]

    def test_the_current_selection_comes_back_marked(self, client, setup):
        _, _, user = setup
        client.force_login(user)
        data = dock_of(client, "/tasks/?status=to-do&xstatus=done")
        status = next(f for f in data["fields"] if f["key"] == "status")
        by_value = {o["v"]: o for o in status["options"]}
        assert by_value["to-do"]["in"] is True
        assert by_value["done"]["ex"] is True
        assert by_value["in-progress"]["in"] is False

    def test_labels_carry_their_colour_and_group(self, client, setup):
        workspace, _, user = setup
        LabelFactory(workspace=workspace, name="backend", color="#6366f1")
        client.force_login(user)
        data = dock_of(client, "/tasks/")
        label = next(f for f in data["fields"] if f["key"] == "label")
        row = next(o for o in label["options"] if o["n"] == "backend")
        assert row["c"] == "#6366f1"

    def test_date_is_a_list_of_presets(self, client, setup):
        _, _, user = setup
        client.force_login(user)
        data = dock_of(client, "/tasks/")
        date = next(f for f in data["fields"] if f["key"] == "date")
        # Six fields with a from / to pair each became a handful of
        # presets that still write the old triple.
        assert date["single"] is True
        assert {o["v"] for o in date["options"]} >= {"overdue", "due-week"}

    def test_a_project_page_offers_no_project_axis(self, client, setup):
        _, project, user = setup
        client.force_login(user)
        data = dock_of(client, f"/projects/{project.slug_prefix}/")
        assert "project" not in [f["key"] for f in data["fields"]]

    def test_the_rail_is_gone(self, client, setup):
        _, _, user = setup
        client.force_login(user)
        body = client.get("/tasks/").content.decode()
        assert "acta-flt-rail" not in body
        assert "acta-dock" in body
