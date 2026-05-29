"""Tests for the refactored ``notify`` tool: profile resolution, audit-log
behaviour (every call writes an in-app row regardless of channel), and
backward compatibility with the legacy ``platform`` arg.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet

import importlib

import nymeria.core.notification_destinations as nd
from nymeria.core.notification_destinations import NotificationDestinationsRepo
from nymeria.tools.notify import notify

# ``nymeria.tools.__init__.py`` re-exports ``notify`` (the StructuredTool)
# which shadows the module attribute, so a plain ``import ... as`` returns
# the tool, not the module. Resolve through importlib to get the module.
notify_mod = importlib.import_module("nymeria.tools.notify")


@pytest.fixture()
def fake_env(tmp_path: Path, monkeypatch):
    """Wire the destinations repo, notification store, and agent fakes onto
    a tmp data dir so the notify tool runs end-to-end without touching real
    user data.
    """
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())

    # Reset the destinations-repo singleton + point it at the tmp DB.
    nd.reset_destinations_repo_for_tests()
    repo = NotificationDestinationsRepo(tmp_path / "accounts.db")
    monkeypatch.setattr(nd, "_repo", repo)

    # Settings: no env-config destinations so auto-seed creates nothing.
    settings = SimpleNamespace(
        data_dir=tmp_path,
        telegram_bot_token=None,
        telegram_default_chat_id=None,
        discord_webhook_url=None,
        slack_webhook_url=None,
        teams_team_id=None,
        teams_channel_id=None,
        teams_account_id=None,
        fcm_enabled=False,
    )
    monkeypatch.setattr(notify_mod, "get_settings", lambda: settings)
    # notification_dispatch.send_via_profile uses a function-local import for
    # get_settings, and the notify tool passes ``settings`` through explicitly,
    # so no module-level monkeypatch is needed there.

    # In-app store -> tmp dir.
    from nymeria.core.notifications import NotificationStore
    import nymeria.core.notifications as notifications_mod

    store = NotificationStore(tmp_path)
    notifications_mod._notification_store = store

    # Fake agent: thread config = no overrides; profile manager returns no
    # user-level default. The notify tool reaches both via get_current_agent.
    thread_config_manager = MagicMock()
    thread_config_manager.get_config.return_value = SimpleNamespace(
        in_app_notification_level="notify_only",
        notification_profile=None,
    )

    fake_profile = SimpleNamespace()
    fake_profile.get_notification_preferences = lambda: {"default_profile": "default"}
    profile_manager = MagicMock()
    profile_manager.get_profile.return_value = fake_profile

    agent = SimpleNamespace(
        thread_config_manager=thread_config_manager,
        profile_manager=profile_manager,
        user_profile_manager=profile_manager,
    )
    def fake_get_current_agent():
        return agent

    import nymeria.core.agent as agent_mod
    monkeypatch.setattr(agent_mod, "get_current_agent", fake_get_current_agent)
    # notification_dispatch also imports get_in_app_notification_level which
    # reads thread_config_manager directly; no patch needed because the agent
    # is wired above.

    return SimpleNamespace(
        repo=repo, store=store, settings=settings, agent=agent,
    )


def _invoke(message="hello", **kwargs):
    return notify.invoke(
        {"message": message, **kwargs},
        config={"configurable": {"user_id": "alice", "thread_id": "t1"}},
    )


# -- audit log -------------------------------------------------------------


def test_notify_with_no_profile_still_writes_in_app_row(fake_env):
    """The in-app feed is the audit log: even when no destinations exist,
    a row is created so the user sees something happened.
    """
    result = _invoke("nothing configured")
    assert "Logged" in result or "delivered" in result.lower()
    rows = fake_env.store.get_all("alice")
    assert len(rows) == 1
    assert rows[0].summary == "nothing configured"
    assert rows[0].delivered_to == []
    assert rows[0].profile == "default"


def test_notify_via_webhook_profile_logs_audit_with_destination(fake_env):
    fake_env.repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        config={"url": "https://example.com/hook"},
    )
    fake_env.repo.create_profile(
        user_id="alice", name="default", destination_names=["hook"],
    )
    with patch(
        "nymeria.core.notification_channels._send_with_egress_policy",
        return_value=MagicMock(status_code=200),
    ):
        result = _invoke("audit me")
    assert "delivered to hook" in result.lower() or "success" in result.lower()
    rows = fake_env.store.get_all("alice")
    assert len(rows) == 1
    row = rows[0]
    assert row.delivered_to == ["hook"]
    assert row.profile == "default"
    assert row.attempted == ["hook"]


def test_notify_records_partial_failure_in_audit_log(fake_env):
    fake_env.repo.create_destination(
        user_id="alice", name="ok", type="webhook",
        config={"url": "https://ok.example.com"},
    )
    fake_env.repo.create_destination(
        user_id="alice", name="bad", type="webhook",
        config={"url": "https://bad.example.com"},
    )
    fake_env.repo.create_profile(
        user_id="alice", name="default",
        destination_names=["ok", "bad"],
    )

    def fake_send(method, url, headers=None, json=None, timeout=None):
        if "bad" in url:
            return MagicMock(status_code=500, text="server error")
        return MagicMock(status_code=200)

    with patch(
        "nymeria.core.notification_channels._send_with_egress_policy",
        side_effect=fake_send,
    ):
        _invoke("split outcome")

    rows = fake_env.store.get_all("alice")
    assert len(rows) == 1
    row = rows[0]
    assert row.delivered_to == ["ok"]
    assert "bad" in row.errors
    assert sorted(row.attempted) == ["bad", "ok"]


def test_notify_skips_audit_when_in_app_level_off(fake_env):
    """Per-thread setting in_app_notification_level=off suppresses the audit
    log row even though external destinations still fire.
    """
    fake_env.repo.create_destination(
        user_id="alice", name="hook", type="webhook",
        config={"url": "https://example.com/hook"},
    )
    fake_env.repo.create_profile(
        user_id="alice", name="default", destination_names=["hook"],
    )
    fake_env.agent.thread_config_manager.get_config.return_value = SimpleNamespace(
        in_app_notification_level="off",
        notification_profile=None,
    )

    with patch(
        "nymeria.core.notification_channels._send_with_egress_policy",
        return_value=MagicMock(status_code=200),
    ):
        _invoke("silent please")
    assert fake_env.store.get_all("alice") == []


# -- profile resolution ----------------------------------------------------


def test_notify_explicit_profile_overrides_default(fake_env):
    fake_env.repo.create_destination(
        user_id="alice", name="phone", type="webhook",
        config={"url": "https://phone.example.com"},
    )
    fake_env.repo.create_destination(
        user_id="alice", name="email", type="webhook",
        config={"url": "https://email.example.com"},
    )
    fake_env.repo.create_profile(
        user_id="alice", name="default", destination_names=["phone"],
    )
    fake_env.repo.create_profile(
        user_id="alice", name="urgent", destination_names=["email"],
    )
    seen_urls = []

    def fake_send(method, url, headers=None, json=None, timeout=None):
        seen_urls.append(url)
        return MagicMock(status_code=200)

    with patch(
        "nymeria.core.notification_channels._send_with_egress_policy",
        side_effect=fake_send,
    ):
        _invoke("send me urgent", profile="urgent")
    assert seen_urls == ["https://email.example.com"]


def test_notify_thread_override_takes_precedence_over_user_default(fake_env):
    fake_env.repo.create_destination(
        user_id="alice", name="phone", type="webhook",
        config={"url": "https://phone.example.com"},
    )
    fake_env.repo.create_destination(
        user_id="alice", name="email", type="webhook",
        config={"url": "https://email.example.com"},
    )
    fake_env.repo.create_profile(
        user_id="alice", name="default", destination_names=["phone"],
    )
    fake_env.repo.create_profile(
        user_id="alice", name="quiet", destination_names=["email"],
    )

    fake_env.agent.thread_config_manager.get_config.return_value = SimpleNamespace(
        in_app_notification_level="notify_only",
        notification_profile="quiet",
    )

    seen_urls = []
    def fake_send(method, url, headers=None, json=None, timeout=None):
        seen_urls.append(url)
        return MagicMock(status_code=200)

    with patch(
        "nymeria.core.notification_channels._send_with_egress_policy",
        side_effect=fake_send,
    ):
        _invoke("thread override")
    assert seen_urls == ["https://email.example.com"]


# -- backward compatibility ------------------------------------------------


def test_notify_legacy_platform_arg_is_accepted(fake_env):
    """``platform="discord"`` (legacy) should not raise -- the notify tool
    accepts it and falls back to the user's default profile (since the
    legacy mapping target ``discord-default`` does not exist in the empty
    setup). Verifies the BC alias path doesn't crash.
    """
    result = _invoke("legacy", platform="discord")
    # No destinations -> "[Logged]" path, but most importantly no exception.
    assert isinstance(result, str)
    assert fake_env.store.get_all("alice")
