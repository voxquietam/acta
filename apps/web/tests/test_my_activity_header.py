"""The header above the My Activity feed: window, counts, rhythm.

The feed says what someone did; the header says what came of it. These
pin the two things that are easy to get quietly wrong — which rows a
window admits, and which filters the counts are allowed to notice.
"""

import datetime

from django.urls import reverse
from django.utils import timezone

import pytest

from apps.activity.models import ActivityLog
from apps.comments.models import Comment
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.models import Task
from apps.tasks.tests.factories import TaskFactory
from apps.web import activity_header
from apps.workspaces.tests.factories import WorkspaceFactory

pytestmark = pytest.mark.django_db


def event(task, event_type, payload, ago_days, actor):
    """Write one activity row at a chosen distance in the past.

    Args:
        task: The task it points at.
        event_type: The logged event type.
        payload: Its payload.
        ago_days: How many days back to date it.
        actor: Who did it.

    Returns:
        The written row.
    """
    row = ActivityLog.objects.create(
        workspace=task.project.workspace,
        project=task.project,
        actor=actor,
        target_type=ActivityLog.TARGET_TASK,
        target_id=task.id,
        event_type=event_type,
        payload=payload,
    )
    ActivityLog.objects.filter(pk=row.pk).update(created_at=timezone.now() - datetime.timedelta(days=ago_days))
    return row


def closed(task, ago_days, actor):
    """Log a close, which is what the first tile counts."""
    return event(task, "task.status_changed", {"from": "in-progress", "to": Task.STATUS_DONE}, ago_days, actor)


@pytest.fixture
def workspace():
    """A workspace with one project and one task to hang events on."""
    ws = WorkspaceFactory()
    project = ProjectFactory(workspace=ws)
    return ws, project, TaskFactory(project=project)


class TestTheWindow:
    """Everything below the range switch obeys it."""

    def test_the_default_is_a_month(self):
        assert activity_header.resolve_range("")["days"] == 30

    def test_an_unknown_key_is_the_default_rather_than_an_error(self):
        """It arrives from a query string, where anything can arrive."""
        assert activity_header.resolve_range("everything")["key"] == activity_header.DEFAULT_RANGE

    def test_the_window_reaches_back_to_midnight_on_its_first_day(self):
        """Otherwise "last 7 days" quietly means "last 6 days and a bit"."""
        window = activity_header.resolve_range("7d")

        assert window["first_day"] == window["today"] - datetime.timedelta(days=6)
        assert timezone.localtime(window["since"]).time() == datetime.time.min

    def test_the_feed_stops_at_the_edge_of_the_range(self, client, workspace):
        ws, _project, task = workspace
        closed(task, 3, ws.owner)
        closed(task, 40, ws.owner)
        client.force_login(ws.owner)

        near = client.get(reverse("web:my_activity"), {"tab": "activity", "range": "30d"})
        far = client.get(reverse("web:my_activity"), {"tab": "activity", "range": "90d"})

        assert near.context["my_activity_count"] == 1
        assert far.context["my_activity_count"] == 2


class TestTheTiles:
    """Five counts, and a comparison that does not take sides."""

    def test_each_tile_counts_its_own_kind(self, client, workspace):
        ws, _project, task = workspace
        closed(task, 2, ws.owner)
        closed(task, 3, ws.owner)
        event(task, "task.created", {}, 4, ws.owner)
        event(task, "task.status_changed", {"from": "in-progress", "to": Task.STATUS_IN_REVIEW}, 5, ws.owner)
        event(task, "task.status_changed", {"from": Task.STATUS_DONE, "to": "to-do"}, 6, ws.owner)
        Comment.objects.create(task=task, author=ws.owner, body="a word")
        client.force_login(ws.owner)

        tiles = {t["key"]: t["value"] for t in client.get(reverse("web:my_activity")).context["summary_tiles"]}

        assert tiles == {"closed": 2, "created": 1, "comments": 1, "review": 1, "reopened": 1}

    def test_the_delta_compares_the_window_before(self, client, workspace):
        ws, _project, task = workspace
        closed(task, 2, ws.owner)
        closed(task, 3, ws.owner)
        closed(task, 40, ws.owner)
        client.force_login(ws.owner)

        tiles = {t["key"]: t for t in client.get(reverse("web:my_activity")).context["summary_tiles"]}

        assert tiles["closed"]["value"] == 2
        assert tiles["closed"]["before"] == 1
        assert tiles["closed"]["delta"] == "+1"

    def test_the_tiles_ignore_the_chips_and_the_search(self, client, workspace):
        """They describe the period. A count that moved with every chip
        would be nothing to compare anything against."""
        ws, _project, task = workspace
        closed(task, 2, ws.owner)
        event(task, "task.created", {}, 3, ws.owner)
        client.force_login(ws.owner)

        narrowed = client.get(
            reverse("web:my_activity"),
            {"tab": "activity", "types": "created", "q": "nothing matches this"},
        )
        tiles = {t["key"]: t["value"] for t in narrowed.context["summary_tiles"]}

        assert tiles["closed"] == 1
        assert tiles["created"] == 1

    def test_a_still_period_reads_as_zero_rather_than_as_a_drop(self, client, workspace):
        ws, _project, _task = workspace
        client.force_login(ws.owner)

        tiles = {t["key"]: t for t in client.get(reverse("web:my_activity")).context["summary_tiles"]}

        assert tiles["closed"]["value"] == 0
        assert tiles["closed"]["delta"] == "±0"


class TestTheRhythm:
    """A picture of the list underneath it, so the two cannot disagree."""

    def test_one_bar_per_day_of_the_window(self, client, workspace):
        ws, _project, _task = workspace
        client.force_login(ws.owner)

        rhythm = client.get(reverse("web:my_activity"), {"range": "7d"}).context["rhythm"]

        assert len(rhythm["bars"]) == 7

    def test_a_quiet_day_is_a_hairline_not_a_gap(self, client, workspace):
        """A row of bars with holes in it reads as missing data."""
        ws, _project, task = workspace
        closed(task, 0, ws.owner)
        client.force_login(ws.owner)

        bars = client.get(reverse("web:my_activity"), {"tab": "activity", "range": "7d"}).context["rhythm"]["bars"]

        assert bars[-1]["height"] > activity_header.RHYTHM_FLOOR
        assert all(bar["height"] == 1 for bar in bars[:-1])

    def test_it_follows_the_filters_even_though_the_tiles_do_not(self, client, workspace):
        ws, _project, task = workspace
        closed(task, 1, ws.owner)
        event(task, "task.created", {}, 1, ws.owner)
        client.force_login(ws.owner)

        filtered = client.get(reverse("web:my_activity"), {"tab": "activity", "types": "created"})

        assert sum(bar["count"] for bar in filtered.context["rhythm"]["bars"]) == 1

    def test_an_empty_window_says_so_in_one_line(self, client, workspace):
        ws, _project, _task = workspace
        client.force_login(ws.owner)

        assert "no events" in client.get(reverse("web:my_activity")).context["rhythm"]["read"]


class TestDayHeadings:
    """The date is overhead a reader pays once a day, not once a row."""

    def test_the_nearest_two_days_are_named(self):
        today = datetime.date(2026, 10, 9)

        assert activity_header.day_heading(today, today) == "Today"
        assert activity_header.day_heading(today - datetime.timedelta(days=1), today) == "Yesterday"

    def test_older_days_carry_their_weekday(self):
        """A gap in the feed only reads as a weekend if the days are named."""
        today = datetime.date(2026, 10, 9)

        assert activity_header.day_heading(datetime.date(2026, 10, 2), today) == "Oct 2 · Fri"

    def test_the_feed_is_split_into_days(self, client, workspace):
        ws, _project, task = workspace
        closed(task, 0, ws.owner)
        closed(task, 1, ws.owner)
        client.force_login(ws.owner)

        days = client.get(reverse("web:my_activity"), {"tab": "activity"}).context["my_days"]

        assert [day["heading"] for day in days] == ["Today", "Yesterday"]

    def test_a_day_split_across_a_page_does_not_announce_itself_twice(self, client, workspace):
        ws, _project, task = workspace
        closed(task, 1, ws.owner)
        client.force_login(ws.owner)
        yesterday = (timezone.localdate() - datetime.timedelta(days=1)).isoformat()

        page = client.get(
            reverse("web:my_activity"),
            {"tab": "activity", "items": 1, "after": yesterday},
        )

        assert page.context["my_days"][0]["repeat"] is True


class TestProjectChips:
    """Filters for projects this person was actually in."""

    def test_only_touched_projects_are_offered(self, client, workspace):
        ws, project, task = workspace
        ProjectFactory(workspace=ws, slug_prefix="QUIET")
        closed(task, 1, ws.owner)
        client.force_login(ws.owner)

        chips = client.get(reverse("web:my_activity"), {"tab": "activity"}).context["project_chips"]

        assert [chip["key"] for chip in chips] == [project.slug_prefix]

    def test_a_chosen_project_stays_offered_even_once_it_filters_to_nothing(self, client, workspace):
        """Otherwise the chip that is doing the filtering disappears and
        the reader cannot switch it off."""
        ws, _project, task = workspace
        quiet = ProjectFactory(workspace=ws, slug_prefix="QUIET")
        closed(task, 1, ws.owner)
        client.force_login(ws.owner)

        chips = client.get(
            reverse("web:my_activity"),
            {"tab": "activity", "projects": quiet.slug_prefix},
        ).context["project_chips"]

        assert [chip["key"] for chip in chips] == [quiet.slug_prefix]
