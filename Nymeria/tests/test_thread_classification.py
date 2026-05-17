"""Tests for centralized thread classification helpers.

Covers classify_platform, is_shared_channel, is_native_platform_thread,
and verifies that backend callers delegate correctly.
"""

from __future__ import annotations

import pytest

from nymeria.core.thread_classification import (
    NATIVE_PLATFORM_PREFIXES,
    PLATFORM_PREFIXES,
    classify_platform,
    is_native_platform_thread,
    is_shared_channel,
)


# ---------------------------------------------------------------------------
# classify_platform
# ---------------------------------------------------------------------------

class TestClassifyPlatform:

    @pytest.mark.parametrize(
        "thread_id, expected",
        [
            ("discord_dm_12345", "discord"),
            ("discord_111_222", "discord"),
            ("discord_", "discord"),
            ("telegram_12345", "telegram"),
            ("telegram_-98765", "telegram"),
            ("slack_C01ABC", "slack"),
            ("matrix_room", "matrix"),
            ("whatsapp_15551234567", "whatsapp"),
            ("messenger_page_psid", "messenger"),
            ("instagram_ig_user", "instagram"),
            ("webex_room", "webex"),
            ("webex_dm_person", "webex"),
            ("mattermost_server_channel", "mattermost"),
            ("mattermost_dm_server_user", "mattermost"),
            ("zulip_realm_stream", "zulip"),
            ("zulip_dm_realm_user", "zulip"),
            ("rocketchat_server_room", "rocketchat"),
            ("rocketchat_dm_server_user", "rocketchat"),
            ("teams_tenant_conversation", "teams"),
            ("teams_dm_tenant_user", "teams"),
            ("googlechat_space", "googlechat"),
            ("googlechat_dm_user", "googlechat"),
            ("line_dm_user", "line"),
            ("line_group_group", "line"),
            ("line_room_room", "line"),
            ("signal_dm_15551234567", "signal"),
            ("signal_group_group", "signal"),
            ("twitch_mychannel", "twitch"),
            ("trigger-hourly-check", "trigger"),
            ("agent-my-agent-abc123", "callable"),
            ("spawned-task-abc123", "callable"),
        ],
    )
    def test_known_prefixes(self, thread_id: str, expected: str):
        assert classify_platform(thread_id) == expected

    @pytest.mark.parametrize(
        "thread_id",
        [
            "some-uuid-1234",
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            "",
            "my-custom-thread",
        ],
    )
    def test_desktop_fallback(self, thread_id: str):
        assert classify_platform(thread_id) == "desktop"

    def test_prefix_order_discord_dm_still_discord(self):
        assert classify_platform("discord_dm_12345") == "discord"

    def test_imported_thread_is_desktop(self):
        assert classify_platform("imported-abc123") == "desktop"


# ---------------------------------------------------------------------------
# is_shared_channel
# ---------------------------------------------------------------------------

class TestIsSharedChannel:

    @pytest.mark.parametrize(
        "thread_id",
        [
            "discord_111_222",
            "discord_guild_channel",
            "telegram_-98765",
            "telegram_-100123456789",
            "slack_T123_C01ABC",
            "matrix_room",
            "whatsapp_group_123",
            "webex_room",
            "mattermost_server_channel",
            "zulip_realm_stream",
            "rocketchat_server_room",
            "teams_tenant_conversation",
            "googlechat_space",
            "line_group_C123",
            "line_room_R123",
            "signal_group_G123",
            "twitch_mychannel",
        ],
    )
    def test_shared_channels(self, thread_id: str):
        assert is_shared_channel(thread_id) is True

    @pytest.mark.parametrize(
        "thread_id",
        [
            "discord_dm_12345",
            "telegram_12345",
            "slack_dm_T123_U123",
            "whatsapp_15551234567",
            "messenger_page_psid",
            "instagram_ig_user",
            "webex_dm_person",
            "mattermost_dm_server_user",
            "zulip_dm_realm_user",
            "rocketchat_dm_server_user",
            "teams_dm_tenant_user",
            "googlechat_dm_user",
            "line_dm_U123",
            "signal_dm_15551234567",
            "trigger-hourly",
            "agent-my-agent",
            "spawned-task",
            "some-uuid",
            "",
        ],
    )
    def test_non_shared(self, thread_id: str):
        assert is_shared_channel(thread_id) is False

    def test_discord_dm_is_not_shared(self):
        assert is_shared_channel("discord_dm_anything") is False

    def test_telegram_positive_id_is_private(self):
        assert is_shared_channel("telegram_12345") is False

    def test_telegram_negative_id_is_shared(self):
        assert is_shared_channel("telegram_-12345") is True


# ---------------------------------------------------------------------------
# is_native_platform_thread
# ---------------------------------------------------------------------------

class TestIsNativePlatformThread:

    @pytest.mark.parametrize(
        "thread_id",
        [
            "discord_dm_12345",
            "discord_111_222",
            "telegram_12345",
            "telegram_-98765",
            "slack_C01ABC",
            "matrix_room",
            "whatsapp_15551234567",
            "messenger_page_psid",
            "instagram_ig_user",
            "webex_room",
            "webex_dm_person",
            "mattermost_server_channel",
            "mattermost_dm_server_user",
            "zulip_realm_stream",
            "zulip_dm_realm_user",
            "rocketchat_server_room",
            "rocketchat_dm_server_user",
            "teams_tenant_conversation",
            "teams_dm_tenant_user",
            "googlechat_space",
            "googlechat_dm_user",
            "line_group_C123",
            "line_room_R123",
            "line_dm_U123",
            "signal_group_G123",
            "signal_dm_15551234567",
            "twitch_mychannel",
            "trigger-hourly",
        ],
    )
    def test_native_threads(self, thread_id: str):
        assert is_native_platform_thread(thread_id) is True

    @pytest.mark.parametrize(
        "thread_id",
        [
            "agent-my-agent",
            "spawned-task",
            "some-uuid",
            "",
            "imported-abc123",
        ],
    )
    def test_non_native_threads(self, thread_id: str):
        assert is_native_platform_thread(thread_id) is False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:

    def test_native_prefixes_are_tuple(self):
        assert isinstance(NATIVE_PLATFORM_PREFIXES, tuple)

    def test_native_prefixes_cover_all_native_platforms(self):
        native = {
            "discord",
            "telegram",
            "slack",
            "matrix",
            "whatsapp",
            "messenger",
            "instagram",
            "webex",
            "mattermost",
            "zulip",
            "rocketchat",
            "teams",
            "googlechat",
            "line",
            "signal",
            "twitch",
            "trigger",
        }
        prefix_platforms = set()
        for prefix, platform in PLATFORM_PREFIXES:
            if prefix in NATIVE_PLATFORM_PREFIXES:
                prefix_platforms.add(platform)
        assert native == prefix_platforms

    def test_callable_prefixes_not_native(self):
        for prefix, platform in PLATFORM_PREFIXES:
            if platform == "callable":
                assert prefix not in NATIVE_PLATFORM_PREFIXES


# ---------------------------------------------------------------------------
# Delegation — verify callers import from the central module
# ---------------------------------------------------------------------------

class TestDelegation:

    def test_thread_metadata_reexports(self):
        from nymeria.core.thread_metadata import classify_platform as tm_classify
        assert tm_classify is classify_platform

    def test_api_aliases(self):
        from nymeria.triggers.api import (
            _classify_thread_platform_from_id,
            _is_native_platform_thread,
            _is_shared_channel_thread,
        )
        assert _classify_thread_platform_from_id is classify_platform
        assert _is_shared_channel_thread is is_shared_channel
        assert _is_native_platform_thread is is_native_platform_thread

    def test_cli_alias(self):
        from nymeria.triggers.cli.commands.threads import _classify_platform
        assert _classify_platform is classify_platform

    def test_telegram_native_prefixes(self):
        from nymeria.triggers.telegram_bot import _NATIVE_SWITCH_THREAD_PREFIXES
        assert _NATIVE_SWITCH_THREAD_PREFIXES is NATIVE_PLATFORM_PREFIXES
