"""Tests for notification delivery thread config defaults."""

from __future__ import annotations

from nymeria.core.thread_config import ThreadConfig


def test_notification_delivery_defaults_are_not_customizations():
    config = ThreadConfig(thread_id="thread-1")

    assert config.telegram_autonomous_delivery == "full"
    assert config.in_app_notification_level == "notify_only"
    assert config.has_customizations() is False


def test_notification_delivery_overrides_are_customizations():
    telegram_config = ThreadConfig(
        thread_id="thread-1",
        telegram_autonomous_delivery="notify_only",
    )
    in_app_config = ThreadConfig(
        thread_id="thread-2",
        in_app_notification_level="all_autonomous",
    )

    assert telegram_config.has_customizations() is True
    assert in_app_config.has_customizations() is True
