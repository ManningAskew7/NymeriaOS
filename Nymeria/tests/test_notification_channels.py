"""Tests for the notification channel registry: dispatch resolution,
profile lookup, auto-seed behaviour, and end-to-end webhook delivery.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from nymeria.core.notification_channels import (
    SendContext,
    SendResult,
    dispatch_to_profile,
    ensure_seeded_destinations,
    get_channel_type,
    list_channel_type_names,
    resolve_profile_name,
)
from nymeria.core.notification_destinations import NotificationDestinationsRepo


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> NotificationDestinationsRepo:
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    return NotificationDestinationsRepo(tmp_path / "accounts.db")


def _settings(**overrides):
    defaults = {
        "telegram_bot_token": None,
        "telegram_default_chat_id": None,
        "discord_webhook_url": None,
        "slack_webhook_url": None,
        "teams_team_id": None,
        "teams_channel_id": None,
        "teams_account_id": None,
        "fcm_enabled": False,
        "data_dir": "/tmp/test-data",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# -- registry --------------------------------------------------------------


def test_registry_has_builtin_types():
    names = list_channel_type_names()
    for builtin in ("telegram", "discord", "slack", "teams", "webhook", "fcm", "email_outlook"):
        assert builtin in names


# -- resolve_profile_name --------------------------------------------------


def test_resolve_profile_explicit_wins():
    assert resolve_profile_name(
        user_id="x", repo=MagicMock(),
        explicit="urgent", thread_default="quiet", user_default="default",
    ) == "urgent"


def test_resolve_profile_thread_over_user():
    assert resolve_profile_name(
        user_id="x", repo=MagicMock(),
        thread_default="thread-prof", user_default="user-prof",
    ) == "thread-prof"


def test_resolve_profile_falls_back_to_default():
    assert resolve_profile_name(user_id="x", repo=MagicMock()) == "default"


# -- dispatch_to_profile --------------------------------------------------


def test_dispatch_missing_profile_returns_empty(repo):
    ctx = SendContext(user_id="alice", thread_id="t", settings=_settings())
    result = dispatch_to_profile(
        message="hi", profile_name="default", ctx=ctx, repo=repo,
    )
    assert result.attempted == []
    assert result.delivered_to == []
    assert result.errors == {}


def test_dispatch_skips_disabled_destinations(repo):
    repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        config={"url": "https://example.com"}, enabled=False,
    )
    repo.create_profile(
        user_id="alice", name="default", destination_names=["hook"],
    )
    ctx = SendContext(user_id="alice", thread_id="t", settings=_settings())
    result = dispatch_to_profile(
        message="hi", profile_name="default", ctx=ctx, repo=repo,
    )
    assert result.attempted == []  # disabled destinations are not even attempted
    assert result.delivered_to == []


def test_dispatch_records_missing_destination_as_error(repo):
    repo.create_profile(
        user_id="alice", name="default",
        destination_names=["gone"],
    )
    ctx = SendContext(user_id="alice", thread_id="t", settings=_settings())
    result = dispatch_to_profile(
        message="hi", profile_name="default", ctx=ctx, repo=repo,
    )
    assert "gone" in result.errors
    assert "not found" in result.errors["gone"]


def test_dispatch_webhook_round_trip(repo):
    repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        config={"url": "https://example.com/hook"},
    )
    repo.create_profile(
        user_id="alice", name="default", destination_names=["hook"],
    )
    ctx = SendContext(user_id="alice", thread_id="t", settings=_settings())

    mock_response = MagicMock(status_code=200)
    with patch(
        "nymeria.core.notification_channels._send_with_egress_policy",
        return_value=mock_response,
    ):
        result = dispatch_to_profile(
            message="hi", profile_name="default", ctx=ctx, repo=repo,
        )
    assert result.delivered_to == ["hook"]
    assert result.errors == {}


def test_dispatch_webhook_sends_bearer_token_header(repo):
    repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        config={"url": "https://example.com/hook"},
        secret_fields={"bearer_token": "secret-123"},
    )
    repo.create_profile(
        user_id="alice", name="default", destination_names=["hook"],
    )
    ctx = SendContext(user_id="alice", thread_id="t", settings=_settings())
    mock_response = MagicMock(status_code=200)
    captured = {}
    def fake_send(method, url, headers=None, json=None, timeout=None):
        captured["headers"] = headers
        captured["json"] = json
        captured["method"] = method
        captured["url"] = url
        return mock_response
    with patch(
        "nymeria.core.notification_channels._send_with_egress_policy",
        side_effect=fake_send,
    ):
        dispatch_to_profile(
            message="hello world", profile_name="default", ctx=ctx, repo=repo,
        )
    assert captured["headers"]["Authorization"] == "Bearer secret-123"
    assert captured["json"]["message"] == "hello world"
    assert captured["json"]["user_id"] == "alice"


def test_dispatch_records_send_error(repo):
    repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        config={"url": "https://example.com/hook"},
    )
    repo.create_profile(
        user_id="alice", name="default", destination_names=["hook"],
    )
    ctx = SendContext(user_id="alice", thread_id="t", settings=_settings())
    with patch(
        "nymeria.core.notification_channels._send_with_egress_policy",
        return_value=MagicMock(status_code=500, text="oops"),
    ):
        result = dispatch_to_profile(
            message="hi", profile_name="default", ctx=ctx, repo=repo,
        )
    assert result.delivered_to == []
    assert "hook" in result.errors
    assert "500" in result.errors["hook"]


def test_dispatch_webhook_blocks_internal_url(repo):
    # H-1 regression: a webhook destination pointed at an internal/loopback
    # target must be blocked by the HTTP egress policy, surfacing as a send
    # error rather than an outbound SSRF request.
    repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        config={"url": "http://127.0.0.1:8000/hook"},
    )
    repo.create_profile(
        user_id="alice", name="default", destination_names=["hook"],
    )
    ctx = SendContext(user_id="alice", thread_id="t", settings=_settings())
    result = dispatch_to_profile(
        message="hi", profile_name="default", ctx=ctx, repo=repo,
    )
    assert result.delivered_to == []
    assert "hook" in result.errors


# -- auto-seed --------------------------------------------------------------


def test_auto_seed_creates_destinations_from_env(repo):
    settings = _settings(
        discord_webhook_url="https://discord.example.com/webhook/abc",
        slack_webhook_url="https://hooks.slack.com/services/foo",
    )
    created = ensure_seeded_destinations(
        user_id="alice", settings=settings, repo=repo,
    )
    assert set(created) >= {"discord-default", "slack-default"}

    discord_dest = repo.get_destination_by_name(
        user_id="alice", name="discord-default",
    )
    assert discord_dest is not None
    assert discord_dest.type == "discord"
    assert repo.get_secret_field(
        dest_id=discord_dest.id, field_name="webhook_url",
    ) == "https://discord.example.com/webhook/abc"


def test_auto_seed_is_idempotent(repo):
    settings = _settings(
        discord_webhook_url="https://discord.example.com/webhook/abc",
    )
    first = ensure_seeded_destinations(
        user_id="alice", settings=settings, repo=repo,
    )
    second = ensure_seeded_destinations(
        user_id="alice", settings=settings, repo=repo,
    )
    assert first == ["discord-default"]
    assert second == []


def test_auto_seed_skips_when_no_env_config(repo):
    settings = _settings()
    created = ensure_seeded_destinations(
        user_id="alice", settings=settings, repo=repo,
    )
    assert created == []
    # No 'default' profile created either since nothing was seeded.
    assert repo.get_profile_by_name(user_id="alice", name="default") is None


def test_auto_seed_creates_default_profile(repo):
    settings = _settings(
        telegram_bot_token="123:abc",
        telegram_default_chat_id="999",
    )
    ensure_seeded_destinations(
        user_id="alice", settings=settings, repo=repo,
    )
    profile = repo.get_profile_by_name(user_id="alice", name="default")
    assert profile is not None
    assert "telegram-default" in profile.destination_names


def test_send_result_helpers():
    assert SendResult.success("ok").ok is True
    assert SendResult.error("nope").ok is False
    assert SendResult.error("nope").detail == "nope"


def test_unknown_channel_type_returns_none():
    assert get_channel_type("nonexistent") is None
