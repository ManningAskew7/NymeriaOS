"""Twitch moderation and chat tools for Nymeria.

These are OPTIONAL_TOOLS — enabled per-thread via thread config.
They require a running Twitch bot instance with Helix API access.
The bot sets the module-level _bot_ref on startup.
"""

import asyncio
import logging
from typing import Optional

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Module-level bot reference, set by twitch_bot.py on startup
_bot_ref = None


def set_bot_ref(bot):
    """Set the TwitchIO bot reference for API calls."""
    global _bot_ref
    _bot_ref = bot


def _get_bot():
    """Get the bot reference or raise."""
    if _bot_ref is None:
        raise RuntimeError("Twitch bot is not running. These tools require the twitch-bot service.")
    return _bot_ref


def _run_async(coro):
    """Run an async coroutine from sync tool context."""
    bot = _get_bot()
    future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
    return future.result(timeout=15)


async def _helix_request(method: str, endpoint: str, **kwargs) -> httpx.Response:
    """Make an authenticated Twitch Helix API request."""
    bot = _get_bot()
    headers = {
        "Client-ID": bot._client_id,
        "Authorization": f"Bearer {bot._access_token}",
        "Content-Type": "application/json",
    }
    url = f"https://api.twitch.tv/helix/{endpoint}"
    async with httpx.AsyncClient() as client:
        resp = await client.request(method, url, headers=headers, **kwargs)
        return resp


async def _resolve_user_id(username: str) -> Optional[str]:
    """Resolve a Twitch username to numeric user ID."""
    bot = _get_bot()
    return await bot.resolve_user_id(username)


@tool
def twitch_send(message: str) -> str:
    """Send a message to the Twitch channel chat.

    Args:
        message: Message to send (max 500 characters).
    """
    if len(message) > 500:
        message = message[:497] + "..."

    async def _send():
        bot = _get_bot()
        await bot._send_to_channel(message)
        return f"Sent message to #{bot._channel_name}: {message[:100]}..."

    return _run_async(_send())


@tool
def twitch_timeout(username: str, duration: int = 300, reason: str = "") -> str:
    """Timeout a user in Twitch chat. Requires moderator permissions.

    Args:
        username: Twitch username to timeout.
        duration: Timeout duration in seconds (1-1800, default 300 = 5 minutes).
        reason: Reason for the timeout.
    """
    duration = max(1, min(1800, duration))

    async def _timeout():
        bot = _get_bot()
        user_id = await _resolve_user_id(username)
        if not user_id:
            return f"Error: Could not find user '{username}'."

        resp = await _helix_request(
            "POST",
            "moderation/bans",
            params={
                "broadcaster_id": bot._broadcaster_id,
                "moderator_id": bot._bot_user_id,
            },
            json={"data": {"user_id": user_id, "duration": duration, "reason": reason}},
        )
        if resp.status_code == 200:
            return f"Timed out {username} for {duration}s. Reason: {reason or 'No reason given'}"
        return f"Error timing out {username}: {resp.status_code} {resp.text}"

    return _run_async(_timeout())


@tool
def twitch_ban(username: str, reason: str = "") -> str:
    """Permanently ban a user from Twitch chat. Requires moderator permissions.

    Args:
        username: Twitch username to ban.
        reason: Reason for the ban.
    """
    async def _ban():
        bot = _get_bot()
        user_id = await _resolve_user_id(username)
        if not user_id:
            return f"Error: Could not find user '{username}'."

        resp = await _helix_request(
            "POST",
            "moderation/bans",
            params={
                "broadcaster_id": bot._broadcaster_id,
                "moderator_id": bot._bot_user_id,
            },
            json={"data": {"user_id": user_id, "reason": reason}},
        )
        if resp.status_code == 200:
            return f"Banned {username}. Reason: {reason or 'No reason given'}"
        return f"Error banning {username}: {resp.status_code} {resp.text}"

    return _run_async(_ban())


@tool
def twitch_unban(username: str) -> str:
    """Unban or untimeout a user in Twitch chat. Requires moderator permissions.

    Args:
        username: Twitch username to unban.
    """
    async def _unban():
        bot = _get_bot()
        user_id = await _resolve_user_id(username)
        if not user_id:
            return f"Error: Could not find user '{username}'."

        resp = await _helix_request(
            "DELETE",
            "moderation/bans",
            params={
                "broadcaster_id": bot._broadcaster_id,
                "moderator_id": bot._bot_user_id,
                "user_id": user_id,
            },
        )
        if resp.status_code == 204:
            return f"Unbanned {username}."
        return f"Error unbanning {username}: {resp.status_code} {resp.text}"

    return _run_async(_unban())


@tool
def twitch_announce(message: str, color: str = "primary") -> str:
    """Send a highlighted announcement to Twitch chat. Requires moderator permissions.

    Args:
        message: Announcement text.
        color: Announcement color: primary, blue, green, orange, purple.
    """
    valid_colors = ("primary", "blue", "green", "orange", "purple")
    if color not in valid_colors:
        color = "primary"

    async def _announce():
        bot = _get_bot()
        resp = await _helix_request(
            "POST",
            "chat/announcements",
            params={
                "broadcaster_id": bot._broadcaster_id,
                "moderator_id": bot._bot_user_id,
            },
            json={"message": message, "color": color},
        )
        if resp.status_code == 204:
            return f"Announcement sent ({color}): {message[:100]}..."
        return f"Error sending announcement: {resp.status_code} {resp.text}"

    return _run_async(_announce())


TWITCH_TOOLS = [twitch_send, twitch_timeout, twitch_ban, twitch_unban, twitch_announce]
