"""Telegram account-linking: tokens, update handling, webhook, settings."""

import json

from django.urls import reverse

import pytest

from apps.accounts.tests.factories import UserFactory
from apps.telegram.models import TelegramAccount
from apps.telegram.services import link_deep_link, make_link_token, process_update, resolve_link_token

CHAT = 4242


def _start_update(token, chat_id=CHAT, username="bob"):
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id},
            "from": {"username": username},
            "text": f"/start {token}",
        },
    }


@pytest.mark.django_db
class TestLinkToken:

    def test_round_trip(self):
        user = UserFactory()
        assert resolve_link_token(make_link_token(user)) == user

    def test_bad_token_returns_none(self):
        assert resolve_link_token("garbage") is None

    def test_token_is_reused_across_calls(self):
        # The settings page re-mints on every poll; it must stay stable, or
        # polling would invalidate the token the user is about to tap.
        user = UserFactory()
        assert make_link_token(user) == make_link_token(user)

    def test_token_is_telegram_safe(self):
        import re

        token = make_link_token(UserFactory())
        assert len(token) <= 64
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)

    def test_token_is_single_use(self):
        user = UserFactory()
        token = make_link_token(user)
        assert resolve_link_token(token) == user
        assert resolve_link_token(token) is None  # consumed

    def test_expired_token_returns_none(self, monkeypatch):
        user = UserFactory()
        token = make_link_token(user)
        # Force the signer to treat any age as too old.
        import apps.telegram.services as svc

        monkeypatch.setattr(svc, "LINK_TOKEN_MAX_AGE", -1)
        assert resolve_link_token(token) is None

    def test_deep_link_none_without_bot_username(self, settings):
        settings.TELEGRAM_BOT_USERNAME = ""
        assert link_deep_link(UserFactory()) is None

    def test_deep_link_built_with_bot_username(self, settings):
        settings.TELEGRAM_BOT_USERNAME = "acta_bot"
        url = link_deep_link(UserFactory())
        assert url.startswith("https://t.me/acta_bot?start=")


@pytest.mark.django_db
class TestProcessUpdate:

    def test_start_links_account(self):
        user = UserFactory()
        process_update(_start_update(make_link_token(user)))
        acct = TelegramAccount.objects.get(user=user)
        assert acct.chat_id == CHAT
        assert acct.username == "bob"

    def test_start_with_bad_token_does_not_link(self):
        process_update(_start_update("nope"))
        assert TelegramAccount.objects.count() == 0

    def test_relinking_moves_chat_to_new_user(self):
        u1, u2 = UserFactory(), UserFactory()
        process_update(_start_update(make_link_token(u1)))
        process_update(_start_update(make_link_token(u2)))  # same chat_id
        assert not TelegramAccount.objects.filter(user=u1).exists()
        assert TelegramAccount.objects.get(user=u2).chat_id == CHAT

    def test_stop_unlinks(self):
        user = UserFactory()
        process_update(_start_update(make_link_token(user)))
        process_update({"message": {"chat": {"id": CHAT}, "text": "/stop"}})
        assert not TelegramAccount.objects.filter(user=user).exists()


@pytest.mark.django_db
class TestWebhook:

    def test_bad_secret_404(self, client, settings):
        settings.TELEGRAM_WEBHOOK_SECRET = "s3cret"
        resp = client.post(reverse("telegram:webhook", args=["wrong"]), data="{}", content_type="application/json")
        assert resp.status_code == 404

    def test_valid_webhook_links(self, client, settings):
        settings.TELEGRAM_WEBHOOK_SECRET = "s3cret"
        user = UserFactory()
        body = json.dumps(_start_update(make_link_token(user)))
        resp = client.post(
            reverse("telegram:webhook", args=["s3cret"]),
            data=body,
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="s3cret",
        )
        assert resp.status_code == 200
        assert TelegramAccount.objects.filter(user=user).exists()

    def test_valid_secret_but_missing_header_404(self, client, settings):
        settings.TELEGRAM_WEBHOOK_SECRET = "s3cret"
        resp = client.post(reverse("telegram:webhook", args=["s3cret"]), data="{}", content_type="application/json")
        assert resp.status_code == 404


@pytest.mark.django_db
class TestSettingsViews:

    def test_status_renders(self, client, settings):
        settings.TELEGRAM_BOT_USERNAME = "acta_bot"
        user = UserFactory()
        client.force_login(user)
        resp = client.get(reverse("telegram:status"))
        assert resp.status_code == 200
        assert b"Connect Telegram" in resp.content

    def test_disconnect_removes_account(self, client):
        user = UserFactory()
        TelegramAccount.objects.create(user=user, chat_id=CHAT, username="bob")
        client.force_login(user)
        resp = client.post(reverse("telegram:disconnect"))
        assert resp.status_code == 200
        assert not TelegramAccount.objects.filter(user=user).exists()
