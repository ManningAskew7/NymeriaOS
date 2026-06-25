"""Tests for centralized thread classification helpers.

Covers classify_platform, is_shared_channel, is_native_platform_thread,
and verifies that backend callers delegate correctly.
"""

from __future__ import annotations

import pytest

from nymeria.core.thread_classification import (
    CHATAPP_BINDING_PLATFORMS,
    NATIVE_PLATFORM_PREFIXES,
    NATIVE_THREAD_PLATFORMS,
    PLATFORM_PREFIXES,
    classify_platform,
    is_native_platform_thread,
    is_shared_channel,
    parse_thread_metadata,
)

# The 17 de-prefixed native platform names (the source-of-truth literal the
# derived NATIVE_THREAD_PLATFORMS frozenset must reproduce).
_EXPECTED_NATIVE_PLATFORMS = {
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
# NATIVE_THREAD_PLATFORMS / CHATAPP_BINDING_PLATFORMS (API read-model sets)
# ---------------------------------------------------------------------------

class TestPlatformSets:

    def test_native_thread_platforms_is_frozenset(self):
        assert isinstance(NATIVE_THREAD_PLATFORMS, frozenset)

    def test_native_thread_platforms_matches_literal(self):
        # Drift guard: the derived set (strip the trailing separator from each
        # native prefix) must equal the hand-maintained literal exactly.
        assert NATIVE_THREAD_PLATFORMS == _EXPECTED_NATIVE_PLATFORMS

    def test_native_thread_platforms_derived_from_prefixes(self):
        assert NATIVE_THREAD_PLATFORMS == frozenset(
            prefix[:-1] for prefix in NATIVE_PLATFORM_PREFIXES
        )

    def test_chatapp_binding_platforms_is_ordered_tuple(self):
        assert isinstance(CHATAPP_BINDING_PLATFORMS, tuple)
        # Lookup order is the resolution order (first bound provider wins);
        # lock the exact sequence.
        assert CHATAPP_BINDING_PLATFORMS == (
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
        )

    def test_binding_platforms_are_native(self):
        # Proper subset: discord/twitch/trigger are native but not bindable.
        assert set(CHATAPP_BINDING_PLATFORMS) < NATIVE_THREAD_PLATFORMS

    def test_binding_platforms_exclude_non_bindable_natives(self):
        # discord/twitch/trigger are native but have no chat-app binding flow.
        for excluded in ("discord", "twitch", "trigger"):
            assert excluded not in CHATAPP_BINDING_PLATFORMS


# ---------------------------------------------------------------------------
# parse_thread_metadata
# ---------------------------------------------------------------------------

class TestParseThreadMetadata:

    @pytest.mark.parametrize(
        "thread_id, expected",
        [
            # discord: dm strips the prefix, guild splits the id
            ("discord_dm_12345", {"platform": "discord", "type": "dm", "channel_id": "12345"}),
            (
                "discord_111_222",
                {"platform": "discord", "type": "guild", "guild_id": "111", "channel_id": "222"},
            ),
            (
                "discord_111",
                {"platform": "discord", "type": "guild", "guild_id": "111", "channel_id": None},
            ),
            (
                "discord_",
                {"platform": "discord", "type": "guild", "guild_id": "", "channel_id": None},
            ),
            # untyped single-prefix channels (no "type" key)
            ("telegram_12345", {"platform": "telegram", "channel_id": "12345"}),
            ("telegram_-98765", {"platform": "telegram", "channel_id": "-98765"}),
            ("slack_C01ABC", {"platform": "slack", "channel_id": "C01ABC"}),
            ("matrix_room", {"platform": "matrix", "channel_id": "room"}),
            ("whatsapp_15551234567", {"platform": "whatsapp", "channel_id": "15551234567"}),
            # whatsapp has no group sub-type in the metadata ladder
            ("whatsapp_group_123", {"platform": "whatsapp", "channel_id": "group_123"}),
            ("messenger_page_psid", {"platform": "messenger", "channel_id": "page_psid"}),
            ("instagram_ig_user", {"platform": "instagram", "channel_id": "ig_user"}),
            # dm-before-generic ordering
            ("webex_dm_person", {"platform": "webex", "type": "dm", "channel_id": "person"}),
            ("webex_room", {"platform": "webex", "type": "room", "channel_id": "room"}),
            ("mattermost_dm_s_u", {"platform": "mattermost", "type": "dm", "channel_id": "s_u"}),
            ("mattermost_s_c", {"platform": "mattermost", "type": "channel", "channel_id": "s_c"}),
            ("zulip_dm_r_u", {"platform": "zulip", "type": "dm", "channel_id": "r_u"}),
            ("zulip_r_s", {"platform": "zulip", "type": "stream", "channel_id": "r_s"}),
            ("rocketchat_dm_s_u", {"platform": "rocketchat", "type": "dm", "channel_id": "s_u"}),
            ("rocketchat_s_r", {"platform": "rocketchat", "type": "room", "channel_id": "s_r"}),
            ("teams_dm_t_u", {"platform": "teams", "type": "dm", "channel_id": "t_u"}),
            ("teams_t_c", {"platform": "teams", "type": "conversation", "channel_id": "t_c"}),
            ("googlechat_dm_user", {"platform": "googlechat", "type": "dm", "channel_id": "user"}),
            ("googlechat_space", {"platform": "googlechat", "type": "space", "channel_id": "space"}),
            ("line_dm_user", {"platform": "line", "type": "dm", "channel_id": "user"}),
            ("line_group_g", {"platform": "line", "type": "group", "channel_id": "g"}),
            ("line_room_r", {"platform": "line", "type": "room", "channel_id": "r"}),
            ("signal_dm_user", {"platform": "signal", "type": "dm", "channel_id": "user"}),
            ("signal_group_g", {"platform": "signal", "type": "group", "channel_id": "g"}),
        ],
    )
    def test_known_prefixes(self, thread_id: str, expected: dict):
        assert parse_thread_metadata(thread_id) == expected

    @pytest.mark.parametrize(
        "thread_id",
        [
            # bare line_/signal_ without dm/group/room have NO generic entry
            "line_unknown",
            "signal_unknown",
            # twitch/trigger/callable prefixes are not in the metadata ladder
            "twitch_mychannel",
            "trigger-hourly",
            "agent-my-agent",
            "spawned-task",
            # plain desktop / unknown ids
            "some-uuid-1234",
            "imported-abc123",
            "",
        ],
    )
    def test_desktop_fallthrough(self, thread_id: str):
        assert parse_thread_metadata(thread_id) == {"platform": "desktop"}

    def test_key_order_preserved(self):
        # FastAPI serializes dicts in insertion order; lock the historical order.
        assert list(parse_thread_metadata("discord_dm_1").keys()) == [
            "platform",
            "type",
            "channel_id",
        ]
        assert list(parse_thread_metadata("discord_1_2").keys()) == [
            "platform",
            "type",
            "guild_id",
            "channel_id",
        ]
        assert list(parse_thread_metadata("teams_t_c").keys()) == [
            "platform",
            "type",
            "channel_id",
        ]
        assert list(parse_thread_metadata("telegram_1").keys()) == [
            "platform",
            "channel_id",
        ]


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

    def test_telegram_native_prefixes(self):
        from nymeria.triggers.telegram_bot import _NATIVE_SWITCH_THREAD_PREFIXES
        assert _NATIVE_SWITCH_THREAD_PREFIXES is NATIVE_PLATFORM_PREFIXES
