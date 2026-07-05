"""Tests for the unified notification dispatch module."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import nymeria.core.notification_channels as nc
import nymeria.core.notification_destinations as nd
from nymeria.core.notification_channels import SendResult, _BaseChannel
from nymeria.core.notification_destinations import NotificationDestinationsRepo
from nymeria.core.notification_dispatch import (
    create_autonomous_notification,
    create_in_app_notification,
    get_in_app_notification_level,
    send_external_notifications,
    send_telegram_default,
    should_notify_autonomous,
    thread_has_telegram_route,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_thread_config_manager(level: str = "notify_only"):
    tc = SimpleNamespace(in_app_notification_level=level)
    mgr = MagicMock()
    mgr.get_config.return_value = tc
    return mgr


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


# ── get_in_app_notification_level ────────────────────────────────────────────


def test_notification_level_returns_default_for_empty_thread_id():
    assert get_in_app_notification_level("", MagicMock()) == "notify_only"


def test_notification_level_returns_default_when_no_config():
    mgr = MagicMock()
    mgr.get_config.return_value = None
    assert get_in_app_notification_level("thread-1", mgr) == "notify_only"


def test_notification_level_reads_thread_config():
    mgr = _make_thread_config_manager("all_autonomous")
    assert get_in_app_notification_level("thread-1", mgr) == "all_autonomous"


def test_notification_level_returns_default_on_exception():
    mgr = MagicMock()
    mgr.get_config.side_effect = RuntimeError("db error")
    assert get_in_app_notification_level("thread-1", mgr) == "notify_only"


def test_notification_level_coerces_none_to_default():
    tc = SimpleNamespace(in_app_notification_level=None)
    mgr = MagicMock()
    mgr.get_config.return_value = tc
    assert get_in_app_notification_level("thread-1", mgr) == "notify_only"


def test_notification_level_coerces_empty_string_to_default():
    tc = SimpleNamespace(in_app_notification_level="")
    mgr = MagicMock()
    mgr.get_config.return_value = tc
    assert get_in_app_notification_level("thread-1", mgr) == "notify_only"


def test_notification_level_off():
    mgr = _make_thread_config_manager("off")
    assert get_in_app_notification_level("thread-1", mgr) == "off"


# ── should_notify_autonomous ─────────────────────────────────────────────────


def test_should_notify_true_for_all_autonomous():
    mgr = _make_thread_config_manager("all_autonomous")
    assert should_notify_autonomous("thread-1", mgr) is True


def test_should_notify_false_for_notify_only():
    mgr = _make_thread_config_manager("notify_only")
    assert should_notify_autonomous("thread-1", mgr) is False


def test_should_notify_false_for_off():
    mgr = _make_thread_config_manager("off")
    assert should_notify_autonomous("thread-1", mgr) is False


def test_should_notify_false_for_empty_thread():
    assert should_notify_autonomous("", MagicMock()) is False


# ── Platform senders — unconfigured returns None ─────────────────────────────


def test_telegram_default_returns_none_when_unconfigured():
    assert send_telegram_default("hi", _settings()) is None


def test_telegram_default_returns_none_without_chat_id():
    assert send_telegram_default("hi", _settings(telegram_bot_token="tok")) is None


# ── thread_has_telegram_route ────────────────────────────────────────────────


def test_telegram_route_empty_thread():
    assert thread_has_telegram_route("") is False


def test_telegram_route_telegram_prefix():
    assert thread_has_telegram_route("telegram_123") is True


def test_telegram_route_non_telegram_without_agent():
    fake_agent_mod = SimpleNamespace(get_current_agent=lambda: None)
    with patch.dict(sys.modules, {"nymeria.core.agent": fake_agent_mod}):
        assert thread_has_telegram_route("desktop-abc") is False


# ── create_in_app_notification ───────────────────────────────────────────────


def test_create_in_app_skips_when_off():
    result = create_in_app_notification("msg", "user-1", "thread-1", in_app_level="off")
    assert result is not None
    assert "Skipped" in result


def test_create_in_app_creates_notification():
    fake_notification = SimpleNamespace(id="notif-1")
    fake_notifications_mod = SimpleNamespace(create_notification=lambda **kw: fake_notification)
    fake_event_bus_mod = SimpleNamespace(publish_autonomous_event=lambda **kw: None)
    with patch.dict(sys.modules, {
        "nymeria.core.notifications": fake_notifications_mod,
        "nymeria.core.event_bus": fake_event_bus_mod,
    }):
        result = create_in_app_notification("hello world", "user-1", "thread-1")
        assert result is not None
        assert "Sent to Desktop" in result
        assert "notif-1" in result


# ── create_autonomous_notification ───────────────────────────────────────────


def test_autonomous_notification_skips_when_not_all_autonomous():
    mgr = _make_thread_config_manager("notify_only")
    calls = []
    fake_notifications_mod = SimpleNamespace(create_notification=lambda **kw: calls.append(kw))
    with patch.dict(sys.modules, {"nymeria.core.notifications": fake_notifications_mod}):
        create_autonomous_notification(
            user_id="user-1",
            thread_id="thread-1",
            summary="done",
            settings=_settings(),
            thread_config_manager=mgr,
        )
    assert len(calls) == 0


def test_autonomous_notification_creates_when_all_autonomous():
    mgr = _make_thread_config_manager("all_autonomous")
    calls = []
    fake_notifications_mod = SimpleNamespace(
        create_notification=lambda **kw: (calls.append(kw), SimpleNamespace(id="n1"))[-1],
    )
    with patch.dict(sys.modules, {"nymeria.core.notifications": fake_notifications_mod}):
        create_autonomous_notification(
            user_id="user-1",
            thread_id="thread-1",
            summary="Task completed successfully",
            settings=_settings(),
            thread_config_manager=mgr,
        )
    assert len(calls) == 1
    assert calls[0]["user_id"] == "user-1"
    assert calls[0]["thread_id"] == "thread-1"


def test_autonomous_notification_sends_fcm_when_enabled():
    mgr = _make_thread_config_manager("all_autonomous")
    fcm_calls = []
    fake_notifications_mod = SimpleNamespace(
        create_notification=lambda **kw: SimpleNamespace(id="n1"),
    )
    fake_fcm_mod = SimpleNamespace(
        send_to_all_devices=lambda **kw: fcm_calls.append(kw),
    )
    with patch.dict(sys.modules, {
        "nymeria.core.notifications": fake_notifications_mod,
        "nymeria.core.fcm": fake_fcm_mod,
    }):
        create_autonomous_notification(
            user_id="user-1",
            thread_id="thread-1",
            summary="FCM test",
            settings=_settings(fcm_enabled=True),
            thread_config_manager=mgr,
        )
    assert len(fcm_calls) == 1
    assert fcm_calls[0]["user_id"] == "user-1"


def test_autonomous_notification_skips_fcm_when_disabled():
    mgr = _make_thread_config_manager("all_autonomous")
    fcm_calls = []
    fake_notifications_mod = SimpleNamespace(
        create_notification=lambda **kw: SimpleNamespace(id="n1"),
    )
    fake_fcm_mod = SimpleNamespace(
        send_to_all_devices=lambda **kw: fcm_calls.append(kw),
    )
    with patch.dict(sys.modules, {
        "nymeria.core.notifications": fake_notifications_mod,
        "nymeria.core.fcm": fake_fcm_mod,
    }):
        create_autonomous_notification(
            user_id="user-1",
            thread_id="thread-1",
            summary="no FCM",
            settings=_settings(fcm_enabled=False),
            thread_config_manager=mgr,
        )
    assert len(fcm_calls) == 0


def test_autonomous_notification_skips_fcm_for_empty_summary():
    mgr = _make_thread_config_manager("all_autonomous")
    fcm_calls = []
    fake_notifications_mod = SimpleNamespace(
        create_notification=lambda **kw: SimpleNamespace(id="n1"),
    )
    fake_fcm_mod = SimpleNamespace(
        send_to_all_devices=lambda **kw: fcm_calls.append(kw),
    )
    with patch.dict(sys.modules, {
        "nymeria.core.notifications": fake_notifications_mod,
        "nymeria.core.fcm": fake_fcm_mod,
    }):
        create_autonomous_notification(
            user_id="user-1",
            thread_id="thread-1",
            summary="",
            settings=_settings(fcm_enabled=True),
            thread_config_manager=mgr,
        )
    assert len(fcm_calls) == 0


def test_autonomous_notification_with_task_id():
    mgr = _make_thread_config_manager("all_autonomous")
    calls = []
    fake_notifications_mod = SimpleNamespace(
        create_notification=lambda **kw: (calls.append(kw), SimpleNamespace(id="n1"))[-1],
    )
    with patch.dict(sys.modules, {"nymeria.core.notifications": fake_notifications_mod}):
        create_autonomous_notification(
            user_id="user-1",
            thread_id="thread-1",
            task_id="todo-42",
            summary="done",
            settings=_settings(),
            thread_config_manager=mgr,
        )
    assert calls[0]["task_id"] == "todo-42"


# ── send_external_notifications (channel-registry profile dispatch) ──────────
#
# send_external_notifications now routes the watchdog's external delivery
# through the user's "default" notification profile (the same channel registry
# the notify tool uses), instead of the old per-platform env senders.


class _FakeOKChannel(_BaseChannel):
    name = "faketest"

    def send(self, message, destination, ctx, repo):
        return SendResult.success("ok")


class _FakeBoomChannel(_BaseChannel):
    name = "faketest"

    def send(self, message, destination, ctx, repo):
        raise RuntimeError("channel boom")


def _tmp_repo(tmp_path: Path, monkeypatch) -> NotificationDestinationsRepo:
    nd.reset_destinations_repo_for_tests()
    repo = NotificationDestinationsRepo(tmp_path / "accounts.db")
    monkeypatch.setattr(nd, "_repo", repo)
    return repo


def test_external_notifications_dispatches_via_default_profile(tmp_path, monkeypatch):
    repo = _tmp_repo(tmp_path, monkeypatch)
    monkeypatch.setitem(nc._REGISTRY, "faketest", _FakeOKChannel())
    repo.create_destination(user_id="u1", name="fake-dest", type="faketest")
    repo.create_profile(user_id="u1", name="default", destination_names=["fake-dest"])

    results = send_external_notifications("hello", _settings(), user_id="u1")
    assert results == ["fake-dest"]


def test_external_notifications_empty_when_no_profile(tmp_path, monkeypatch):
    # No env config => auto-seed creates nothing and no "default" profile exists,
    # so dispatch delivers to nothing.
    _tmp_repo(tmp_path, monkeypatch)
    results = send_external_notifications("hello", _settings(), user_id="u1")
    assert results == []


def test_external_notifications_isolates_channel_exception(tmp_path, monkeypatch):
    # A channel that raises is caught per-destination inside dispatch_to_profile;
    # the function returns an empty success list and never propagates.
    repo = _tmp_repo(tmp_path, monkeypatch)
    monkeypatch.setitem(nc._REGISTRY, "faketest", _FakeBoomChannel())
    repo.create_destination(user_id="u1", name="fake-dest", type="faketest")
    repo.create_profile(user_id="u1", name="default", destination_names=["fake-dest"])

    results = send_external_notifications("hello", _settings(), user_id="u1")
    assert results == []


# ── Callers use the shared module (regression / delegation checks) ───────────


def test_ticker_should_notify_delegates_to_shared_helper(monkeypatch):
    """Verify the Ticker's _should_create_autonomous_notification calls the
    shared helper rather than a local copy of the logic."""
    import nymeria.core.ticker as ticker_mod

    calls = []
    monkeypatch.setattr(
        ticker_mod,
        "should_notify_autonomous",
        lambda tid, tcm: (calls.append((tid, tcm)), True)[-1],
    )

    # After AGENT-019, the ticker holds the thread_config_manager directly
    # (DI), no longer reaches through ``self.agent``.
    fake_thread_config_manager = MagicMock()
    ticker = ticker_mod.Ticker.__new__(ticker_mod.Ticker)
    ticker.thread_config_manager = fake_thread_config_manager

    result = ticker._should_create_autonomous_notification("thread-x")
    assert result is True
    assert calls[0][0] == "thread-x"
    assert calls[0][1] is fake_thread_config_manager


def test_notify_tool_routes_through_dispatch():
    """The tool module dispatches through ``send_via_profile`` and
    ``get_in_app_notification_level`` from the dispatch module rather than
    calling per-platform helpers directly.
    """
    tool_mod = sys.modules["nymeria.tools.notify"]
    import nymeria.core.notification_dispatch as dispatch_mod

    assert tool_mod.send_via_profile is dispatch_mod.send_via_profile
    assert tool_mod.get_in_app_notification_level is dispatch_mod.get_in_app_notification_level
