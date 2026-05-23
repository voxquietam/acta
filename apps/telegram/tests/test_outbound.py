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

    def test_default_wraps_preview_in_blockquote(self):
        n, _actor, _task = self._notif()
        out = tg._format_notification(n)
        assert "<blockquote expandable>snippet</blockquote>" in out

    def test_quote_placeholder_renders_blockquote(self):
        from apps.telegram.models import TelegramMessageTemplate

        n, actor, _task = self._notif()
        TelegramMessageTemplate.objects.create(kind=Notification.Kind.ASSIGNED, body="{actor}: {quote}")
        out = tg._format_notification(n)
        assert out == f"{actor.display_name}: <blockquote expandable>snippet</blockquote>"

    def test_quote_placeholder_empty_without_preview(self):
        from apps.telegram.models import TelegramMessageTemplate

        ws = WorkspaceFactory()
        n = Notification.objects.create(recipient=UserFactory(), workspace=ws, kind=Notification.Kind.ASSIGNED)
        TelegramMessageTemplate.objects.create(kind=Notification.Kind.ASSIGNED, body="hi{quote}")
        assert tg._format_notification(n) == "hi"


class TestCleanPreview:
    """`_clean_preview` turns raw markdown into a tidy plain-text snippet."""

    def test_empty_is_empty(self):
        assert tg._clean_preview("") == ""

    def test_unwraps_mention_and_task_tokens(self):
        out = tg._clean_preview("ping [@bob](mention:7) on [VND-2](task:5) please")
        assert out == "ping @bob on VND-2 please"

    def test_drops_recipients_own_mention(self):
        out = tg._clean_preview("[@me](mention:42) take a look", recipient_id=42)
        assert out == "take a look"

    def test_keeps_other_mentions_when_dropping_own(self):
        out = tg._clean_preview("[@me](mention:42) and [@bob](mention:7)", recipient_id=42)
        assert out == "and @bob"

    def test_image_only_falls_back_to_marker(self):
        assert tg._clean_preview("![](http://x/y.png)") == "🖼 image"

    def test_mention_only_of_recipient_with_image_is_marker(self):
        out = tg._clean_preview("[@me](mention:42) ![shot](http://x/y.png)", recipient_id=42)
        assert out == "🖼 image"

    def test_strips_emphasis_and_collapses_whitespace(self):
        out = tg._clean_preview("this is **very**\n\n  _important_")
        assert out == "this is very important"

    def test_unwraps_plain_markdown_links(self):
        assert tg._clean_preview("see [the docs](https://x/y)") == "see the docs"

    def test_truncates_long_text_with_ellipsis(self):
        out = tg._clean_preview("word " * 100, limit=40)
        assert out.endswith("…")
        assert len(out) <= 41


class TestChips:
    """Pure priority / due chip formatting (no DB)."""

    def test_priority_chip(self):
        from apps.tasks.models import Task

        assert tg._priority_chip(Task.URGENT) == "🔴 Urgent"
        assert tg._priority_chip(Task.LOW) == "🔵 Low"
        assert tg._priority_chip(Task.NO_PRIORITY) == ""

    def test_due_chip(self):
        import datetime

        assert tg._due_chip(None) == ""
        assert tg._due_chip(datetime.date(2026, 5, 30)).startswith("📅 due")


@pytest.mark.django_db
class TestAssignedContext:
    """{priority} / {due} / {meta} placeholders and default ASSIGNED enrichment."""

    def _assigned(self, priority, due):
        ws = WorkspaceFactory()
        project = ProjectFactory(workspace=ws)
        task = TaskFactory(project=project, priority=priority, due_date=due)
        return Notification.objects.create(
            recipient=UserFactory(),
            actor=UserFactory(),
            workspace=ws,
            kind=Notification.Kind.ASSIGNED,
            task=task,
            preview=task.title,
        )

    def test_meta_joins_priority_and_due(self):
        import datetime

        from apps.tasks.models import Task
        from apps.telegram.models import TelegramMessageTemplate

        n = self._assigned(Task.URGENT, datetime.date(2026, 5, 30))
        TelegramMessageTemplate.objects.create(kind=Notification.Kind.ASSIGNED, body="{meta}")
        out = tg._format_notification(n)
        assert "🔴 Urgent" in out and "📅 due" in out and " · " in out

    def test_meta_drops_separator_without_due(self):
        from apps.tasks.models import Task
        from apps.telegram.models import TelegramMessageTemplate

        n = self._assigned(Task.HIGH, None)
        TelegramMessageTemplate.objects.create(kind=Notification.Kind.ASSIGNED, body="{meta}")
        assert tg._format_notification(n) == "🟠 High"

    def test_tidy_drops_empty_meta_line(self):
        from apps.tasks.models import Task
        from apps.telegram.models import TelegramMessageTemplate

        n = self._assigned(Task.NO_PRIORITY, None)
        TelegramMessageTemplate.objects.create(kind=Notification.Kind.ASSIGNED, body="head\n{meta}\ntail")
        assert tg._format_notification(n) == "head\ntail"

    def test_default_assigned_enriches_and_skips_title_quote(self):
        from apps.tasks.models import Task

        n = self._assigned(Task.URGENT, None)
        out = tg._format_notification(n)
        assert out.count(n.task.title) == 1  # title shown once, not also quoted
        assert "🔴 Urgent" in out
        assert "<blockquote" not in out


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
