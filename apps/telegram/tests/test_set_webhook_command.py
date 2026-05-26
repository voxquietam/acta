"""Tests for the ``telegram_set_webhook`` management command."""

from django.core.management import call_command
from django.test import override_settings

from apps.telegram import client


@override_settings(ACTA_PUBLIC_BASE_URL="https://acta.example.com", TELEGRAM_WEBHOOK_SECRET="s3cr3t")
def test_uses_env_base_url_when_no_flag(monkeypatch):
    """With no ``--base-url`` the command registers ``ACTA_PUBLIC_BASE_URL``."""
    calls = {}

    def fake_set(url, secret_token=""):
        calls.update(url=url, secret=secret_token)
        return True

    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "set_webhook", fake_set)
    call_command("telegram_set_webhook")
    assert calls["url"] == "https://acta.example.com/telegram/webhook/s3cr3t/"
    assert calls["secret"] == "s3cr3t"


@override_settings(ACTA_PUBLIC_BASE_URL="https://acta.example.com", TELEGRAM_WEBHOOK_SECRET="s3cr3t")
def test_flag_overrides_env(monkeypatch):
    calls = {}
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "set_webhook", lambda url, secret_token="": calls.update(url=url) or True)
    call_command("telegram_set_webhook", base_url="https://override.example.com")
    assert calls["url"] == "https://override.example.com/telegram/webhook/s3cr3t/"


def test_skips_when_not_configured(monkeypatch):
    """Deploy-safe: no token → skip without raising, never calls the API."""
    called = {"set": False}
    monkeypatch.setattr(client, "is_configured", lambda: False)
    monkeypatch.setattr(client, "set_webhook", lambda *a, **k: called.update(set=True) or True)
    call_command("telegram_set_webhook")  # must not raise
    assert called["set"] is False


@override_settings(ACTA_PUBLIC_BASE_URL="", TELEGRAM_WEBHOOK_SECRET="s")
def test_skips_when_no_base_url(monkeypatch):
    """Deploy-safe: configured but no base URL → skip without raising."""
    called = {"set": False}
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "set_webhook", lambda *a, **k: called.update(set=True) or True)
    call_command("telegram_set_webhook")
    assert called["set"] is False


def test_delete(monkeypatch):
    called = {"deleted": False}
    monkeypatch.setattr(client, "is_configured", lambda: True)
    monkeypatch.setattr(client, "delete_webhook", lambda: called.update(deleted=True) or True)
    call_command("telegram_set_webhook", delete=True)
    assert called["deleted"] is True
