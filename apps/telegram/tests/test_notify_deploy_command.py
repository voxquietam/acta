"""Tests for the ``notify_deploy`` management command (deploy heads-up)."""

from django.core.management import call_command
from django.test import override_settings

import pytest

from apps.telegram import client
from apps.telegram.tests.factories import TelegramAccountFactory


def _capture(monkeypatch):
    """Patch the client to record chat ids instead of hitting Telegram."""
    sent: list[int] = []
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "send_message", lambda chat_id, text: sent.append(chat_id) or True)
    return sent


@pytest.mark.django_db
def test_broadcasts_to_enabled_accounts(monkeypatch):
    """Default: fan the heads-up out to every enabled linked account only."""
    sent = _capture(monkeypatch)
    a = TelegramAccountFactory(enabled=True)
    b = TelegramAccountFactory(enabled=True)
    TelegramAccountFactory(enabled=False)  # opted out — must be skipped

    call_command("notify_deploy", branch="dev", commit="abc1234")

    assert sorted(sent) == sorted([a.chat_id, b.chat_id])


@pytest.mark.django_db
@override_settings(TELEGRAM_DEPLOY_CHAT_ID="555")
def test_override_chat_id_sends_only_there(monkeypatch):
    """``TELEGRAM_DEPLOY_CHAT_ID`` set → single chat, accounts ignored."""
    sent = _capture(monkeypatch)
    TelegramAccountFactory(enabled=True)

    call_command("notify_deploy", branch="master")

    assert sent == [555]


def test_skips_when_not_configured(monkeypatch):
    """Deploy-safe: no bot token → no send, no raise."""
    called = {"sent": False}
    monkeypatch.setattr(client, "is_configured", lambda: False)
    monkeypatch.setattr(client, "send_message", lambda *a, **k: called.update(sent=True) or True)

    call_command("notify_deploy", branch="dev")  # must not raise

    assert called["sent"] is False


@pytest.mark.django_db
def test_no_reachable_chats_is_noop(monkeypatch):
    """Configured but nobody linked → no send, no raise."""
    sent = _capture(monkeypatch)

    call_command("notify_deploy", branch="dev")

    assert sent == []
