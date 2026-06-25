"""Centralized thread ID classification.

Single source of truth for mapping thread ID prefixes to platforms,
shared-channel detection, and native-platform checks. All backend
modules should import from here instead of maintaining inline copies.

Frontend mirrors (TypeScript) exist in desktop/mobile — keep them in
sync when adding a new platform prefix.
"""

from __future__ import annotations

from typing import Any

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


# Platform-name sets used by the API read models to resolve the sidebar
# platform for a thread. Derived from / kept in lock-step with the prefix
# table above so a new platform only needs editing in one place.

# De-prefixed names of every platform that keeps native routing (the trailing
# separator, ``_`` or ``-``, is stripped from each native prefix).
NATIVE_THREAD_PLATFORMS: frozenset[str] = frozenset(
    prefix[:-1] for prefix in NATIVE_PLATFORM_PREFIXES
)

# Platforms that support an explicit chat-app binding lookup. Curated subset of
# the native platforms (excludes discord, twitch, and trigger, which have no
# binding flow); iteration order is the resolution order (first bound wins).
CHATAPP_BINDING_PLATFORMS: tuple[str, ...] = (
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

# Prefix -> (platform, type) table for parse_thread_metadata, evaluated in
# most-specific-first order. The discord prefixes are handled separately (the
# guild form splits the id rather than stripping the prefix). A ``None`` type
# omits the "type" key from the result, matching the historical route output.
_METADATA_PREFIXES: tuple[tuple[str, str, str | None], ...] = (
    ("telegram_", "telegram", None),
    ("slack_", "slack", None),
    ("matrix_", "matrix", None),
    ("whatsapp_", "whatsapp", None),
    ("messenger_", "messenger", None),
    ("instagram_", "instagram", None),
    ("webex_dm_", "webex", "dm"),
    ("webex_", "webex", "room"),
    ("mattermost_dm_", "mattermost", "dm"),
    ("mattermost_", "mattermost", "channel"),
    ("zulip_dm_", "zulip", "dm"),
    ("zulip_", "zulip", "stream"),
    ("rocketchat_dm_", "rocketchat", "dm"),
    ("rocketchat_", "rocketchat", "room"),
    ("teams_dm_", "teams", "dm"),
    ("teams_", "teams", "conversation"),
    ("googlechat_dm_", "googlechat", "dm"),
    ("googlechat_", "googlechat", "space"),
    ("line_dm_", "line", "dm"),
    ("line_group_", "line", "group"),
    ("line_room_", "line", "room"),
    ("signal_dm_", "signal", "dm"),
    ("signal_group_", "signal", "group"),
)


def parse_thread_metadata(thread_id: str) -> dict[str, Any]:
    """Return platform metadata (``platform``/``type``/``channel_id``) for a thread ID.

    The single source of truth for the per-platform prefix-to-metadata mapping
    served by ``GET /threads/{id}/metadata``. Output dict shape and key order
    match the historical inline ladder exactly:

      - ``discord_dm_<id>``    -> ``{"platform","type":"dm","channel_id"}``
      - ``discord_<g>_<c>``    -> ``{"platform","type":"guild","guild_id","channel_id"}``
      - typed channels         -> ``{"platform","type","channel_id"}``
      - untyped channels       -> ``{"platform","channel_id"}``
      - anything else          -> ``{"platform":"desktop"}``
    """
    if thread_id.startswith("discord_dm_"):
        return {
            "platform": "discord",
            "type": "dm",
            "channel_id": thread_id[len("discord_dm_"):],
        }
    if thread_id.startswith("discord_"):
        parts = thread_id.split("_")
        return {
            "platform": "discord",
            "type": "guild",
            "guild_id": parts[1] if len(parts) >= 2 else None,
            "channel_id": parts[2] if len(parts) >= 3 else None,
        }
    for prefix, platform, channel_type in _METADATA_PREFIXES:
        if thread_id.startswith(prefix):
            meta: dict[str, Any] = {"platform": platform}
            if channel_type is not None:
                meta["type"] = channel_type
            meta["channel_id"] = thread_id[len(prefix):]
            return meta
    return {"platform": "desktop"}
