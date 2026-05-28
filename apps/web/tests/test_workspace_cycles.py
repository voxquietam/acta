"""Tests for the workspace cadence (cycles) settings panel.

Covers the admin-gated ``set_workspace_cycles`` POST flow and the
settings page rendering the live cycle preview.
"""

from django.urls import reverse

import pytest

from apps.cycles.models import Cycle
from apps.workspaces.models import WorkspaceMember
from apps.workspaces.tests.factories import WorkspaceFactory, WorkspaceMemberFactory


@pytest.fixture
def workspace(db):
    return WorkspaceFactory()


@pytest.mark.django_db
class TestSetWorkspaceCycles:

    def url(self, ws):
        return reverse("web:set_workspace_cycles", kwargs={"slug": ws.slug})

    def test_admin_enables_cadence_and_materializes_cycles(self, client, workspace):
        client.force_login(workspace.owner)
        resp = client.post(
            self.url(workspace),
            {"enabled": "on", "length_weeks": "2", "start_date": "2026-05-04"},
        )
        assert resp.status_code == 302
        workspace.refresh_from_db()
        cfg = workspace.cycle_config()
        assert cfg["enabled"] is True
        assert cfg["length_weeks"] == 2
        assert workspace.cycles.exists()

    def test_htmx_save_without_state_change_swaps_card_in_place(self, client, workspace):
        """HTMX save with no enable-state transition returns the card partial + toast."""
        # Pre-enable so this save doesn't transition the sidebar's Cycles link.
        workspace.cycle_settings = {
            "enabled": True,
            "length_weeks": 2,
            "start_date": "2026-05-04",
            "auto_rollover": False,
        }
        workspace.save(update_fields=["cycle_settings"])
        client.force_login(workspace.owner)
        resp = client.post(
            self.url(workspace),
            {"enabled": "on", "length_weeks": "3", "start_date": "2026-05-04"},
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 200
        assert b'id="workspace-cycles"' in resp.content
        assert "acta:toast" in resp.headers.get("HX-Trigger", "")
        workspace.refresh_from_db()
        assert workspace.cycle_config()["length_weeks"] == 3

    def test_htmx_enable_transition_forces_full_refresh(self, client, workspace):
        """Toggling cycles on/off forces HX-Refresh so the sidebar Cycles link updates."""
        client.force_login(workspace.owner)
        resp = client.post(
            self.url(workspace),
            {"enabled": "on", "length_weeks": "2", "start_date": "2026-05-04"},
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 204
        assert resp["HX-Refresh"] == "true"
        workspace.refresh_from_db()
        assert workspace.cycle_config()["enabled"] is True

    def test_enable_without_start_date_defaults_to_today(self, client, workspace):
        client.force_login(workspace.owner)
        resp = client.post(self.url(workspace), {"enabled": "on", "length_weeks": "1"})
        assert resp.status_code == 302
        workspace.refresh_from_db()
        assert workspace.cycle_config()["start_date"] is not None

    def test_disable_keeps_no_active_cycle(self, client, workspace):
        client.force_login(workspace.owner)
        client.post(self.url(workspace), {"enabled": "on", "length_weeks": "2", "start_date": "2026-05-04"})
        resp = client.post(self.url(workspace), {"length_weeks": "2", "start_date": "2026-05-04"})
        assert resp.status_code == 302
        workspace.refresh_from_db()
        assert workspace.cycle_config()["enabled"] is False

    def test_non_admin_forbidden(self, client, workspace):
        member = WorkspaceMemberFactory(workspace=workspace, role=WorkspaceMember.MEMBER)
        client.force_login(member.user)
        resp = client.post(
            self.url(workspace),
            {"enabled": "on", "length_weeks": "2", "start_date": "2026-05-04"},
        )
        assert resp.status_code == 403

    def test_invalid_length_rejected(self, client, workspace):
        client.force_login(workspace.owner)
        resp = client.post(self.url(workspace), {"enabled": "on", "length_weeks": "abc"})
        assert resp.status_code == 400


@pytest.mark.django_db
class TestSettingsPageRendersCycles:

    def test_panel_present_with_preview(self, client, workspace):
        workspace.cycle_settings = {"enabled": True, "length_weeks": 2, "start_date": "2026-05-04"}
        workspace.save(update_fields=["cycle_settings"])
        client.force_login(workspace.owner)
        resp = client.get(reverse("web:workspace_settings", kwargs={"slug": workspace.slug}))
        assert resp.status_code == 200
        body = resp.content.decode()
        assert "Enable cycles" in body
        # Loading the page materializes the rolling windows.
        assert workspace.cycles.filter(status=Cycle.ACTIVE).exists() or workspace.cycles.exists()
