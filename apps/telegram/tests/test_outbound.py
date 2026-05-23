"""Outbound fan-out: notify() mirrors notifications to a linked Telegram chat."""

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import Notification
from apps.notifications.services import notify
from apps.projects.tests.factories import ProjectFactory
from apps.tasks.tests.factories import TaskFactory
from apps.telegram import services as tg
from apps.telegram.models import TelegramAccount
from apps.workspaces.tests.factories import WorkspaceFactory


@pytest.fixture
def sent(monkeypatch):
    """Capture client.send_message calls as (chat_id, text) tuples."""
    calls = []
    monkeypatch.setattr(
        "apps.telegram.client.send_message", lambda chat_id, text: calls.append((chat_id, text)) or True
    )
    return calls


def _linked(user, chat_id=999, enabled=True):
    return TelegramAccount.objects.create(user=user, chat_id=chat_id, username="u", enabled=enabled)


@pytest.mark.django_db
class TestNotifyViaTelegram:

    def test_sends_for_linked_enabled(self, sent):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        user = UserFactory()
        _linked(user, chat_id=555)
        n = Notification.objects.create(
            recipient=user, workspace=ws, kind=Notification.Kind.ASSIGNED, task=task, preview="do it"
        )
        assert tg.notify_via_telegram(n) is True
        assert len(sent) == 1
        assert sent[0][0] == 555
        assert task.slug in sent[0][1]

    def test_skips_when_disabled(self, sent):
        ws = WorkspaceFactory()
        user = UserFactory()
        _linked(user, enabled=False)
        n = Notification.objects.create(recipient=user, workspace=ws, kind=Notification.Kind.MENTION, preview="x")
        assert tg.notify_via_telegram(n) is False
        assert sent == []

    def test_skips_when_not_linked(self, sent):
        ws = WorkspaceFactory()
        n = Notification.objects.create(recipient=UserFactory(), workspace=ws, kind=Notification.Kind.COMMENT)
        assert tg.notify_via_telegram(n) is False
        assert sent == []

    def test_skips_muted_kind(self, sent):
        ws = WorkspaceFactory()
        user = UserFactory()
        _linked(user)
        TelegramAccount.objects.filter(user=user).update(muted_kinds=[Notification.Kind.STATUS_CHANGE])
        muted = Notification.objects.create(recipient=user, workspace=ws, kind=Notification.Kind.STATUS_CHANGE)
        kept = Notification.objects.create(recipient=user, workspace=ws, kind=Notification.Kind.MENTION, preview="hi")
        assert tg.notify_via_telegram(muted) is False
        assert tg.notify_via_telegram(kept) is True
        assert len(sent) == 1


@pytest.mark.django_db
class TestNotifyHookFiresOnCommit:

    def test_notify_dispatches_telegram_on_commit(self, sent, django_capture_on_commit_callbacks):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        recipient, actor = UserFactory(), UserFactory()
        _linked(recipient, chat_id=777)
        with django_capture_on_commit_callbacks(execute=True):
            notify(
                recipient_id=recipient.id,
                actor=actor,
                kind=Notification.Kind.MENTION,
                workspace_id=ws.id,
                task=task,
                preview="hey @you",
            )
        assert any(chat_id == 777 for chat_id, _text in sent)


@pytest.mark.django_db
class TestMessageTemplates:

    def _notif(self):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project)
        actor = UserFactory()
        return (
            Notification.objects.create(
                recipient=UserFactory(),
                actor=actor,
                workspace=ws,
                kind=Notification.Kind.ASSIGNED,
                task=task,
                preview="snippet",
            ),
            actor,
            task,
        )

    def test_custom_template_overrides_default(self):
        from apps.telegram.models import TelegramMessageTemplate

        n, actor, task = self._notif()
        TelegramMessageTemplate.objects.create(kind=Notification.Kind.ASSIGNED, body="🔔 {actor} → {slug}: {title}")
        out = tg._format_notification(n)
        assert out == f"🔔 {actor.display_name} → {task.slug}: {task.title}"

    def test_unknown_placeholder_left_as_is(self):
        from apps.telegram.models import TelegramMessageTemplate

        n, _actor, _task = self._notif()
        TelegramMessageTemplate.objects.create(kind=Notification.Kind.ASSIGNED, body="hi {bogus}")
        assert tg._format_notification(n) == "hi {bogus}"

    def test_falls_back_to_default_without_template(self):
        n, _actor, task = self._notif()
        out = tg._format_notification(n)
        assert task.slug in out  # default format still renders the task


@pytest.mark.django_db
class TestToggleView:

    def test_toggle_flips_enabled(self, client):
        from django.urls import reverse

        user = UserFactory()
        acct = _linked(user, enabled=True)
        client.force_login(user)
        resp = client.post(reverse("telegram:toggle"))
        assert resp.status_code == 200
        acct.refresh_from_db()
        assert acct.enabled is False

    def test_toggle_kind_mutes_and_unmutes(self, client):
        from django.urls import reverse

        user = UserFactory()
        acct = _linked(user)
        client.force_login(user)
        url = reverse("telegram:toggle_kind")
        client.post(url, {"kind": Notification.Kind.COMMENT})
        acct.refresh_from_db()
        assert Notification.Kind.COMMENT in acct.muted_kinds
        client.post(url, {"kind": Notification.Kind.COMMENT})
        acct.refresh_from_db()
        assert Notification.Kind.COMMENT not in acct.muted_kinds
