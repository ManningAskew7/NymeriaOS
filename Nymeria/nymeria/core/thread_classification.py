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
    ("whatsapp_", "whatsapp"),
    ("teams_", "teams"),
    ("twitch_", "twitch"),
    ("trigger-", "trigger"),
    ("agent-", "callable"),
    ("spawned-", "callable"),
)

NATIVE_PLATFORM_PREFIXES: tuple[str, ...] = (
    "discord_",
    "telegram_",
    "slack_",
    "whatsapp_",
    "teams_",
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
    groups/supergroups, Slack channels, Teams channels/group chats, and
    Twitch stream chats. These threads are inherently shared by every
    linked user in the channel.

    Convention (set by bot ``make_thread_id`` helpers):
      - ``discord_dm_<channel_id>``          → 1:1 DM, per-user
      - ``discord_<guild>_<channel>``        → shared channel
      - ``telegram_<positive_chat_id>``      → 1:1 DM
      - ``telegram_-<digits>``              → group/supergroup (negative IDs)
      - ``slack_dm_<team>_<user>``           → 1:1 DM, per-user
      - ``slack_<team>_<channel>``           → shared Slack channel/group DM
      - ``whatsapp_<sender>``                → 1:1 Cloud API customer chat
      - ``whatsapp_group_<id>``              → future shared WhatsApp group
      - ``teams_dm_<tenant>_<user>``         → 1:1 DM, per-user
      - ``teams_<tenant>_<conversation>``    → shared Teams chat/channel thread
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
    if thread_id.startswith("whatsapp_group_"):
        return True
    if thread_id.startswith("whatsapp_"):
        return False
    if thread_id.startswith("teams_dm_"):
        return False
    if thread_id.startswith("teams_"):
        return True
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
    "whatsapp",
    "teams",
)

# Prefix -> (platform, type) table for parse_thread_metadata, evaluated in
# most-specific-first order. The discord prefixes are handled separately (the
# guild form splits the id rather than stripping the prefix). A ``None`` type
# omits the "type" key from the result, matching the historical route output.
_METADATA_PREFIXES: tuple[tuple[str, str, str | None], ...] = (
    ("telegram_", "telegram", None),
    ("slack_", "slack", None),
    ("whatsapp_", "whatsapp", None),
    ("teams_dm_", "teams", "dm"),
    ("teams_", "teams", "conversation"),
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
