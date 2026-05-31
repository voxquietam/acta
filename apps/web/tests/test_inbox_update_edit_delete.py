"""Inbox preview — edit/delete affordances for project updates.

Covers the ``?source=inbox`` paths on ``update_edit_form`` /
``edit_project_update`` / ``delete_project_update`` plus the
``can_modify`` flag wiring on the preview-pane render so the buttons
only appear for authors / workspace admins.
"""

from django.urls import reverse

import pytest

from apps.projects.models import ProjectUpdate
from apps.projects.tests.factories import ProjectFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def setup(db):
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    update = ProjectUpdate.objects.create(
        project=project,
        author=ws.owner,
        health=ProjectUpdate.ON_TRACK,
        body="orig body",
    )
    return ws.owner, update


@pytest.mark.django_db
class TestInboxEditForm:

    def test_inbox_edit_form_targets_preview_body(self, client, setup):
        user, update = setup
        client.force_login(user)
        resp = client.get(reverse("web:update_edit_form", args=[update.id]) + "?source=inbox")
        assert resp.status_code == 200
        body = resp.content.decode()
        # Inbox-mode form targets the preview body slot, not the overview card.
        assert "#inbox-update-body" in body
        # ``hx-post`` MUST hit the save endpoint, not the form-rendering one;
        # an earlier draft pointed it back at update_edit_form which silently
        # 200'd the form HTML instead of saving the body.
        save_url = reverse("web:edit_project_update", args=[update.id])
        assert f'hx-post="{save_url}?source=inbox"' in body
        # Cancel restores the preview via the dedicated preview endpoint.
        assert reverse("web:inbox_update_preview", args=[update.id]) in body

    def test_inbox_edit_save_returns_body_slot_only(self, client, setup):
        user, update = setup
        client.force_login(user)
        resp = client.post(
            reverse("web:edit_project_update", args=[update.id]) + "?source=inbox",
            {"health": ProjectUpdate.AT_RISK, "body": "fresh"},
        )
        assert resp.status_code == 200
        update.refresh_from_db()
        assert update.body == "fresh"
        assert update.health == ProjectUpdate.AT_RISK
        body = resp.content.decode()
        # The response is just the body slot — no preview-pane header.
        assert "fresh" in body
        # OOB swap refreshes the health pill in the header so the user
        # sees the new health without a manual reload.
        assert 'id="inbox-update-health-pill"' in body
        assert 'hx-swap-oob="innerHTML"' in body


@pytest.mark.django_db
class TestInboxDelete:

    def test_inbox_delete_clears_preview_and_fires_event(self, client, setup):
        user, update = setup
        client.force_login(user)
        update_id = update.id
        resp = client.post(reverse("web:delete_project_update", args=[update.id]) + "?source=inbox")
        assert resp.status_code == 200
        assert not ProjectUpdate.objects.filter(pk=update_id).exists()
        # Preview pane shows the empty state.
        assert "Pick an update on the left" in resp.content.decode()
        # Fires the event that drives the inbox list refetch.
        assert "acta:update-deleted" in resp.headers.get("HX-Trigger", "")


@pytest.mark.django_db
class TestCanModifyOnPreviewEndpoint:

    def test_can_modify_attached_for_author(self, client, setup):
        user, update = setup
        client.force_login(user)
        resp = client.get(reverse("web:inbox_update_preview", args=[update.id]))
        assert resp.status_code == 200
        # Edit/delete buttons visible — keyed off the can_modify flag.
        body = resp.content.decode()
        assert reverse("web:update_edit_form", args=[update.id]) in body

    def test_other_member_sees_no_actions(self, client, setup):
        _author, update = setup
        ws = update.project.workspace
        other = WorkspaceFactory().owner
        # Bring the other user into this workspace as a plain member so they
        # can read the update but not modify it.
        from apps.workspaces.tests.factories import WorkspaceMemberFactory

        WorkspaceMemberFactory(workspace=ws, user=other)
        client.force_login(other)
        resp = client.get(reverse("web:inbox_update_preview", args=[update.id]))
        body = resp.content.decode()
        # Edit endpoint URL absent → no edit/delete affordance rendered.
        assert reverse("web:update_edit_form", args=[update.id]) not in body
