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
