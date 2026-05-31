"""Quiet hours: helper, queue branch in notify, digest flush, settings endpoint."""

import datetime

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import Notification
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.telegram import services as tg
from apps.telegram.models import TelegramAccount, TelegramQueuedNotification
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def sent(monkeypatch):
    """Capture client.send_message calls as (chat_id, text) tuples."""
    calls = []
    monkeypatch.setattr(
        "apps.telegram.client.send_message",
        lambda chat_id, text: calls.append((chat_id, text)) or True,
    )
    return calls


def _linked(user, **overrides):
    defaults = {"chat_id": 999, "username": "u", "enabled": True}
    defaults.update(overrides)
    return TelegramAccount.objects.create(user=user, **defaults)


class TestIsInQuietHours:
    """Pure helper — no DB, no time travel needed."""

    def test_none_endpoints_never_silent(self):
        t = datetime.time(22, 0)
        assert tg.is_in_quiet_hours(None, datetime.time(8, 0), t) is False
        assert tg.is_in_quiet_hours(datetime.time(18, 0), None, t) is False

    def test_zero_width_window_never_silent(self):
        same = datetime.time(12, 0)
        assert tg.is_in_quiet_hours(same, same, same) is False

    def test_normal_window_inside(self):
        # 09:00 → 17:00, now 12:00 — inside.
        assert tg.is_in_quiet_hours(datetime.time(9), datetime.time(17), datetime.time(12)) is True

    def test_normal_window_at_start_inside(self):
        # Window is half-open [start, end) — start itself is inside.
        assert tg.is_in_quiet_hours(datetime.time(9), datetime.time(17), datetime.time(9)) is True

    def test_normal_window_at_end_outside(self):
        # ...and end itself is outside.
        assert tg.is_in_quiet_hours(datetime.time(9), datetime.time(17), datetime.time(17)) is False

    def test_normal_window_outside(self):
        assert tg.is_in_quiet_hours(datetime.time(9), datetime.time(17), datetime.time(22)) is False

    def test_wrap_past_midnight_late_evening_inside(self):
        # 18:00 → 08:00, now 22:00 — inside the wrapped window.
        assert tg.is_in_quiet_hours(datetime.time(18), datetime.time(8), datetime.time(22)) is True

    def test_wrap_past_midnight_early_morning_inside(self):
        # 18:00 → 08:00, now 03:00 — still inside.
        assert tg.is_in_quiet_hours(datetime.time(18), datetime.time(8), datetime.time(3)) is True

    def test_wrap_past_midnight_daytime_outside(self):
        # 18:00 → 08:00, now 12:00 — outside.
        assert tg.is_in_quiet_hours(datetime.time(18), datetime.time(8), datetime.time(12)) is False


@pytest.mark.django_db
class TestNotifyQueueBranch:
    """``notify_via_telegram`` defers to the queue inside quiet hours."""

    def _now_inside_window(self, account, now):
        local = timezone.localtime(now).time()
        # Pin a 6-hour window that brackets ``now`` so the call lands inside.
        start = (datetime.datetime.combine(datetime.date(2026, 1, 1), local) - datetime.timedelta(hours=3)).time()
        end = (datetime.datetime.combine(datetime.date(2026, 1, 1), local) + datetime.timedelta(hours=3)).time()
        account.quiet_hours_enabled = True
        account.quiet_hours_start = start
        account.quiet_hours_end = end
        account.save(update_fields=["quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end"])

    def test_queues_inside_window(self, sent):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        user = UserFactory()
        account = _linked(user, chat_id=555)
        self._now_inside_window(account, timezone.now())
        n = Notification.objects.create(
            recipient=user,
            workspace=ws,
            kind=Notification.Kind.ASSIGNED,
            task=task,
            preview="do it",
        )
        assert tg.notify_via_telegram(n) is True
        assert sent == []
        queued = TelegramQueuedNotification.objects.get(account=account)
        assert queued.kind == Notification.Kind.ASSIGNED
        assert queued.task_id == task.id
        assert queued.delivered_at is None

    def test_sends_live_outside_window(self, sent):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        user = UserFactory()
        account = _linked(user, chat_id=555)
        # Quiet hours OFF — live delivery.
        account.quiet_hours_enabled = False
        account.save(update_fields=["quiet_hours_enabled"])
        n = Notification.objects.create(
            recipient=user,
            workspace=ws,
            kind=Notification.Kind.ASSIGNED,
            task=task,
            preview="do it",
        )
        assert tg.notify_via_telegram(n) is True
        assert len(sent) == 1
        assert not TelegramQueuedNotification.objects.exists()


@pytest.mark.django_db
class TestFlushDigest:
    """``flush_telegram_quiet_digests`` command."""

    def _queue(self, account, *, kind, task_id=None, body="msg"):
        return TelegramQueuedNotification.objects.create(
            account=account,
            kind=kind,
            task_id=task_id,
            body=body,
        )

    def test_does_not_send_while_inside_window(self, sent):
        user = UserFactory()
        account = _linked(user)
        now_local = timezone.localtime().time()
        account.quiet_hours_enabled = True
        account.quiet_hours_start = (
            datetime.datetime.combine(datetime.date.today(), now_local) - datetime.timedelta(hours=1)
        ).time()
        account.quiet_hours_end = (
            datetime.datetime.combine(datetime.date.today(), now_local) + datetime.timedelta(hours=1)
        ).time()
        account.save()
        self._queue(account, kind=Notification.Kind.ASSIGNED, task_id=1)
        call_command("flush_telegram_quiet_digests")
        assert sent == []
        assert TelegramQueuedNotification.objects.filter(delivered_at__isnull=True).count() == 1

    def test_sends_and_marks_delivered_when_outside_window(self, sent):
        user = UserFactory()
        account = _linked(user, chat_id=42)
        # Window already closed.
        account.quiet_hours_enabled = False
        account.save()
        self._queue(account, kind=Notification.Kind.ASSIGNED, task_id=1, body="row-a")
        self._queue(account, kind=Notification.Kind.COMMENT, task_id=2, body="row-b")
        call_command("flush_telegram_quiet_digests")
        assert len(sent) == 1
        chat_id, text = sent[0]
        assert chat_id == 42
        assert "row-a" in text and "row-b" in text
        assert TelegramQueuedNotification.objects.filter(delivered_at__isnull=True).count() == 0

    def test_dedup_same_task_kind_keeps_latest(self, sent):
        user = UserFactory()
        account = _linked(user)
        account.quiet_hours_enabled = False
        account.save()
        self._queue(account, kind=Notification.Kind.STATUS_CHANGE, task_id=7, body="status-1")
        self._queue(account, kind=Notification.Kind.STATUS_CHANGE, task_id=7, body="status-2")
        self._queue(account, kind=Notification.Kind.STATUS_CHANGE, task_id=7, body="status-3")
        call_command("flush_telegram_quiet_digests")
        text = sent[0][1]
        # Only the latest body for that (task, kind) survives the dedupe.
        assert "status-3" in text
        assert "status-1" not in text
        assert "status-2" not in text
        # All three rows marked delivered, not just the kept one.
        assert TelegramQueuedNotification.objects.filter(delivered_at__isnull=True).count() == 0

    def test_caps_at_20_with_more_footer(self, sent):
        user = UserFactory()
        account = _linked(user)
        account.quiet_hours_enabled = False
        account.save()
        # 25 distinct (task_id, kind) entries to bypass dedupe and stress the cap.
        for i in range(25):
            self._queue(account, kind=Notification.Kind.ASSIGNED, task_id=i, body=f"row-{i}")
        call_command("flush_telegram_quiet_digests")
        text = sent[0][1]
        # The first 20 are present; the overflow line trails.
        assert "row-0" in text
        assert "row-19" in text
        assert "row-20" not in text
        assert "+5 more" in text


@pytest.mark.django_db
class TestQuietHoursEndpoint:

    def test_save_persists_fields(self, client):
        user = UserFactory()
        _linked(user)
        client.force_login(user)
        resp = client.post(
            reverse("telegram:set_quiet_hours"),
            {"enabled": "on", "start": "18:00", "end": "08:00"},
        )
        assert resp.status_code == 200
        account = TelegramAccount.objects.get(user=user)
        assert account.quiet_hours_enabled is True
        assert account.quiet_hours_start == datetime.time(18, 0)
        assert account.quiet_hours_end == datetime.time(8, 0)

    def test_disable_flushes_pending_queue(self, client, sent):
        user = UserFactory()
        account = _linked(user)
        account.quiet_hours_enabled = True
        account.quiet_hours_start = datetime.time(18, 0)
        account.quiet_hours_end = datetime.time(8, 0)
        account.save()
        TelegramQueuedNotification.objects.create(account=account, kind=Notification.Kind.ASSIGNED, body="x")
        client.force_login(user)
        client.post(
            reverse("telegram:set_quiet_hours"),
            {"start": "18:00", "end": "08:00"},  # enabled checkbox absent → off
        )
        account.refresh_from_db()
        assert account.quiet_hours_enabled is False
        # Pending queue marked delivered (no digest will resurrect it).
        assert TelegramQueuedNotification.objects.filter(delivered_at__isnull=True).count() == 0

    def test_empty_times_force_disable(self, client):
        user = UserFactory()
        _linked(user)
        client.force_login(user)
        client.post(
            reverse("telegram:set_quiet_hours"),
            {"enabled": "on", "start": "", "end": ""},
        )
        account = TelegramAccount.objects.get(user=user)
        assert account.quiet_hours_enabled is False

    def test_save_response_carries_toast_trigger(self, client):
        # The Save button looks inert without a confirmation flash — the
        # HX-Trigger header drives the same acta:toast surface the
        # workspace settings save uses.
        user = UserFactory()
        _linked(user)
        client.force_login(user)
        resp = client.post(
            reverse("telegram:set_quiet_hours"),
            {"enabled": "on", "start": "18:00", "end": "08:00"},
        )
        assert "HX-Trigger" in resp.headers
        assert "acta:toast" in resp.headers["HX-Trigger"]
        assert "success" in resp.headers["HX-Trigger"]
