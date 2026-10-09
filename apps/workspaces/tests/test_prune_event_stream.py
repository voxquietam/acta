"""Bounding the persisted SSE event table.

``django_eventstream`` writes a row per broadcast so a reconnecting
browser can replay what it missed. Nothing read them after that window
and nothing deleted them, so the table only ever grew.
"""

import datetime

from django.core.management import call_command
from django.utils import timezone

import pytest

pytestmark = pytest.mark.django_db


def _event(channel, *, age_days):
    """Write one stored event, backdated by ``age_days``."""
    from django_eventstream.models import Event

    event = Event.objects.create(channel=channel, type="task.updated", data="{}")
    Event.objects.filter(pk=event.pk).update(created=timezone.now() - datetime.timedelta(days=age_days))
    return event


class TestPruning:
    def test_it_drops_what_no_client_could_still_replay(self, capsys):
        from django_eventstream.models import Event

        old = _event("workspace-1", age_days=30)
        recent = _event("workspace-1", age_days=1)

        call_command("prune_event_stream")

        assert not Event.objects.filter(pk=old.pk).exists()
        assert Event.objects.filter(pk=recent.pk).exists()

    def test_a_week_is_the_default_window(self):
        """Generous on purpose: pruning past a client's last id answers
        ``stream-reset``, which the browser does not handle."""
        from django_eventstream.models import Event

        just_inside = _event("workspace-1", age_days=6)
        just_outside = _event("workspace-1", age_days=8)

        call_command("prune_event_stream")

        assert Event.objects.filter(pk=just_inside.pk).exists()
        assert not Event.objects.filter(pk=just_outside.pk).exists()

    def test_dry_run_deletes_nothing(self, capsys):
        from django_eventstream.models import Event

        stale = _event("workspace-1", age_days=30)

        call_command("prune_event_stream", "--dry-run")

        assert Event.objects.filter(pk=stale.pk).exists()
        assert "would delete" in capsys.readouterr().out

    def test_the_window_is_adjustable(self):
        from django_eventstream.models import Event

        two_days_old = _event("workspace-1", age_days=2)

        call_command("prune_event_stream", "--older-than-days", "1")

        assert not Event.objects.filter(pk=two_days_old.pk).exists()
