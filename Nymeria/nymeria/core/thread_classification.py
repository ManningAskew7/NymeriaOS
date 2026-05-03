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
    ("twitch_", "twitch"),
    ("trigger-", "trigger"),
    ("agent-", "callable"),
    ("spawned-", "callable"),
)

NATIVE_PLATFORM_PREFIXES: tuple[str, ...] = (
    "discord_",
    "telegram_",
    "slack_",
    "twitch_",
    "trigger-",
)


def classify_platform(thread_id: str) -> str:
    """Return the platform name for a thread ID based on its prefix.

    Returns one of: ``"discord"``, ``"telegram"``, ``"slack"``,
    ``"twitch"``, ``"trigger"``, ``"callable"``, or ``"desktop"``.
    """
    for prefix, platform in PLATFORM_PREFIXES:
        if thread_id.startswith(prefix):
            return platform
    return "desktop"


def is_shared_channel(thread_id: str) -> bool:
    """Return True for multi-user shared channels.

    Shared channels are Discord guild/server channels, Telegram
    groups/supergroups, and Twitch stream chats. These threads are
    inherently shared by every linked user in the channel.

    Convention (set by bot ``make_thread_id`` helpers):
      - ``discord_dm_<channel_id>``          → 1:1 DM, per-user
      - ``discord_<guild>_<channel>``        → shared channel
      - ``telegram_<positive_chat_id>``      → 1:1 DM
      - ``telegram_-<digits>``              → group/supergroup (negative IDs)
      - ``twitch_<channel_name>``            → shared stream chat
    """
    if thread_id.startswith("discord_dm_"):
        return False
    if thread_id.startswith("discord_"):
        return True
    if thread_id.startswith("telegram_-"):
        return True
    if thread_id.startswith("twitch_"):
        return True
    return False


def is_native_platform_thread(thread_id: str) -> bool:
    """Return True for platform-native IDs that should keep native routing."""
    return thread_id.startswith(NATIVE_PLATFORM_PREFIXES)
