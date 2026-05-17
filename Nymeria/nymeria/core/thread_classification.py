"""Centralized thread ID classification.

Single source of truth for mapping thread ID prefixes to platforms,
shared-channel detection, and native-platform checks. All backend
modules should import from here instead of maintaining inline copies.

Frontend mirrors (TypeScript) exist in desktop/mobile — keep them in
sync when adding a new platform prefix.
"""

from __future__ import annotations

PLATFORM_PREFIXES: tuple[tuple[str, str], ...] = (
    ("discord_", "discord"),
    ("telegram_", "telegram"),
    ("slack_", "slack"),
    ("matrix_", "matrix"),
    ("whatsapp_", "whatsapp"),
    ("messenger_", "messenger"),
    ("instagram_", "instagram"),
    ("webex_", "webex"),
    ("mattermost_", "mattermost"),
    ("zulip_", "zulip"),
    ("rocketchat_", "rocketchat"),
    ("teams_", "teams"),
    ("googlechat_", "googlechat"),
    ("line_", "line"),
    ("signal_", "signal"),
    ("twitch_", "twitch"),
    ("trigger-", "trigger"),
    ("agent-", "callable"),
    ("spawned-", "callable"),
)

NATIVE_PLATFORM_PREFIXES: tuple[str, ...] = (
    "discord_",
    "telegram_",
    "slack_",
    "matrix_",
    "whatsapp_",
    "messenger_",
    "instagram_",
    "webex_",
    "mattermost_",
    "zulip_",
    "rocketchat_",
    "teams_",
    "googlechat_",
    "line_",
    "signal_",
    "twitch_",
    "trigger-",
)


def classify_platform(thread_id: str) -> str:
    """Return the platform name for a thread ID based on its prefix.

    Returns one of the known native/chat platforms, ``"trigger"``,
    ``"callable"``, or ``"desktop"``.
    """
    for prefix, platform in PLATFORM_PREFIXES:
        if thread_id.startswith(prefix):
            return platform
    return "desktop"


def is_shared_channel(thread_id: str) -> bool:
    """Return True for multi-user shared channels.

    Shared channels are Discord guild/server channels, Telegram
    groups/supergroups, Slack channels, Matrix rooms, Webex group spaces,
    Mattermost channels, Zulip streams, Rocket.Chat rooms, Teams channels/group
    chats, Google Chat spaces/group chats, LINE group/room chats, Signal group
    chats, and Twitch stream chats. These
    threads are inherently shared by every linked user in the channel.

    Convention (set by bot ``make_thread_id`` helpers):
      - ``discord_dm_<channel_id>``          → 1:1 DM, per-user
      - ``discord_<guild>_<channel>``        → shared channel
      - ``telegram_<positive_chat_id>``      → 1:1 DM
      - ``telegram_-<digits>``              → group/supergroup (negative IDs)
      - ``slack_dm_<team>_<user>``           → 1:1 DM, per-user
      - ``slack_<team>_<channel>``           → shared Slack channel/group DM
      - ``matrix_<room_id>``                 → shared Matrix room
      - ``whatsapp_<sender>``                → 1:1 Cloud API customer chat
      - ``whatsapp_group_<id>``              → future shared WhatsApp group
      - ``messenger_<page>_<psid>``          → 1:1 Messenger Page customer chat
      - ``instagram_<ig>_<sender>``          → 1:1 Instagram professional account DM
      - ``webex_dm_<person_id>``             → 1:1 DM, per-user
      - ``webex_<room_id>``                  → shared Webex group space
      - ``mattermost_dm_<server>_<user>``    → 1:1 DM, per-user
      - ``mattermost_<server>_<channel>``    → shared Mattermost channel/group DM
      - ``zulip_dm_<realm>_<user>``          → 1:1 DM, per-user
      - ``zulip_<realm>_<stream>``           → shared Zulip stream/topic
      - ``rocketchat_dm_<server>_<user>``    → 1:1 DM, per-user
      - ``rocketchat_<server>_<room>``       → shared Rocket.Chat room
      - ``teams_dm_<tenant>_<user>``         → 1:1 DM, per-user
      - ``teams_<tenant>_<conversation>``    → shared Teams chat/channel thread
      - ``googlechat_dm_<user>``             → 1:1 DM, per-user
      - ``googlechat_<space>``               → shared Google Chat space/group chat
      - ``line_dm_<user>``                   → 1:1 DM, per-user
      - ``line_group_<group>``               → shared LINE group chat
      - ``line_room_<room>``                 → shared LINE multi-person room
      - ``signal_dm_<user>``                 → 1:1 DM, per-user
      - ``signal_group_<group>``             → shared Signal group chat
      - ``twitch_<channel_name>``            → shared stream chat
    """
    if thread_id.startswith("discord_dm_"):
        return False
    if thread_id.startswith("discord_"):
        return True
    if thread_id.startswith("telegram_-"):
        return True
    if thread_id.startswith("slack_dm_"):
        return False
    if thread_id.startswith("slack_"):
        return True
    if thread_id.startswith("matrix_"):
        return True
    if thread_id.startswith("whatsapp_group_"):
        return True
    if thread_id.startswith("whatsapp_"):
        return False
    if thread_id.startswith("messenger_"):
        return False
    if thread_id.startswith("instagram_"):
        return False
    if thread_id.startswith("webex_dm_"):
        return False
    if thread_id.startswith("webex_"):
        return True
    if thread_id.startswith("mattermost_dm_"):
        return False
    if thread_id.startswith("mattermost_"):
        return True
    if thread_id.startswith("zulip_dm_"):
        return False
    if thread_id.startswith("zulip_"):
        return True
    if thread_id.startswith("rocketchat_dm_"):
        return False
    if thread_id.startswith("rocketchat_"):
        return True
    if thread_id.startswith("teams_dm_"):
        return False
    if thread_id.startswith("teams_"):
        return True
    if thread_id.startswith("googlechat_dm_"):
        return False
    if thread_id.startswith("googlechat_"):
        return True
    if thread_id.startswith("line_dm_"):
        return False
    if thread_id.startswith(("line_group_", "line_room_")):
        return True
    if thread_id.startswith("line_"):
        return False
    if thread_id.startswith("signal_group_"):
        return True
    if thread_id.startswith("signal_"):
        return False
    if thread_id.startswith("twitch_"):
        return True
    return False


def is_native_platform_thread(thread_id: str) -> bool:
    """Return True for platform-native IDs that should keep native routing."""
    return thread_id.startswith(NATIVE_PLATFORM_PREFIXES)
