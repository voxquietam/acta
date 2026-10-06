"""Tests for the gantt's window controls.

The chart lives inside the Plan panel — there is no Timeline tab, its
Timeline render is the same chart. It is a window onto the plan: the
browser picks the span from the zoom and the ‹ › offset, and the server
hands down only today. These tests hold the contract between the two — the
controls the JS binds to must be in the markup, and the data span must
not be, or a later edit "restoring" it would quietly bring back the old
whole-canvas chart.
"""

from django.urls import reverse

import pytest

from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.mark.django_db
class TestTimelineWindowControls:
    """The sub-toolbar's window nav, and the body's weekend lane."""

    def _timeline(self, client):
        """Render the All Tasks plan panel as a gantt and return its HTML.

        Returns:
            The panel's decoded body.
        """
        workspace = WorkspaceFactory()
        project = ProjectFactory(workspace=workspace)
        TaskFactory(
            project=project,
            reporter=workspace.owner,
        )
        client.force_login(workspace.owner)
        resp = client.get(
            reverse("web:all_tasks") + "?panel=plan&render=timeline",
            HTTP_HX_REQUEST="true",
        )
        assert resp.status_code == 200
        return resp.content.decode()

    def test_window_nav_renders(self, client):
        """‹ › and the range label are the JS's handles — all three must exist."""
        body = self._timeline(client)
        for marker in (
            'data-tl="prev"',
            'data-tl="next"',
            'data-tl="range"',
            'data-tl="today-btn"',
        ):
            assert marker in body, f"{marker} missing — the window cannot be moved"

    def test_weekend_lane_renders(self, client):
        """Weekend bands need a container; the JS fills it in day zoom only."""
        assert 'data-tl="weekends"' in self._timeline(client)

    def test_chart_takes_today_but_not_the_data_span(self, client):
        """The window is chosen in the browser, so only today rides in.

        ``chart_start`` / ``chart_end`` stay server-side, where they decide
        which milestones are worth sending; handing them to the chart again
        is how it would go back to drawing one canvas across the whole span.
        """
        body = self._timeline(client)
        assert "data-today=" in body
        assert "data-chart-start" not in body
        assert "data-chart-end" not in body

    def test_outside_window_label_rides_in(self, client):
        """A milestone the window has paged past is reported, so it needs its string."""
        assert "data-i18n-outside-window=" in self._timeline(client)
