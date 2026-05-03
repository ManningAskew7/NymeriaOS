"""Runtime adapter used by Twitch tools.

This module intentionally lives outside ``nymeria.tools`` so tool hot-reload
does not reset the active Twitch bot registration. Tool objects from old and
newly reloaded modules both resolve through this stable runtime service.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


class TwitchRuntimeUnavailable(RuntimeError):
    """Raised when Twitch tools are invoked without a running Twitch bot."""


class TwitchToolRuntime:
    """Stable service facade over the current Twitch bot instance."""

    def __init__(self) -> None:
        self._bot_ref: Callable[[], Any | None] | None = None

    def register_bot(self, bot: Any) -> None:
        """Register the currently running Twitch bot."""
        try:
            self._bot_ref = weakref.ref(bot)
        except TypeError:
            # Some test doubles may not support weakrefs. The production
            # TwitchIO bot does, and either path preserves the same facade.
            self._bot_ref = lambda: bot
        logger.debug("Registered Twitch tool runtime for bot %r", bot)

    def unregister_bot(self, bot: Any | None = None) -> None:
        """Clear the active bot registration.

        If *bot* is supplied, only clear when it matches the currently
        registered object so a late close from an old instance cannot erase a
        newer registration.
        """
        current = self._current_bot()
        if bot is None or current is bot:
            self._bot_ref = None
            logger.debug("Unregistered Twitch tool runtime")

    def _current_bot(self) -> Any | None:
        if self._bot_ref is None:
            return None
        return self._bot_ref()

    def _require_bot(self) -> Any:
        bot = self._current_bot()
        if bot is None:
            raise TwitchRuntimeUnavailable(
                "Twitch bot is not running. These tools require the twitch-bot service."
            )
        return bot

    def _require_running_bot(self) -> Any:
        bot = self._require_bot()
        if getattr(bot, "_stopped", False):
            raise TwitchRuntimeUnavailable(
                "Bot is stopped. All tools disabled until !start is used."
            )
        return bot

    @property
    def channel_name(self) -> str:
        return str(getattr(self._require_bot(), "_channel_name", ""))

    @property
    def broadcaster_id(self) -> Optional[str]:
        value = getattr(self._require_bot(), "_broadcaster_id", None)
        return str(value) if value is not None else None

    @property
    def bot_user_id(self) -> Optional[str]:
        value = getattr(self._require_bot(), "_bot_user_id", None)
        return str(value) if value is not None else None

    @property
    def client_id(self) -> str:
        return str(getattr(self._require_bot(), "_client_id", ""))

    def run(self, coro_factory: Callable[[], Awaitable[str]]) -> str:
        """Run an async Twitch operation on the bot loop from a sync tool."""
        bot = self._require_running_bot()
        future = asyncio.run_coroutine_threadsafe(coro_factory(), bot.loop)
        return future.result(timeout=15)

    def read_chat(self, count: int) -> str:
        """Return formatted recent chat buffer contents."""
        bot = self._require_bot()
        messages = bot._buffer.get_recent(count)
        if not messages:
            return "Chat buffer is empty. No messages received yet."

        from ..triggers.twitch_bot import format_chat_context

        formatted = format_chat_context(messages)
        total = len(bot._buffer)
        return f"[{len(messages)} of {total} buffered messages]\n{formatted}"

    async def send_to_channel(self, message: str) -> None:
        bot = self._require_running_bot()
        await bot._send_to_channel(message)

    async def resolve_user_id(self, username: str) -> Optional[str]:
        bot = self._require_running_bot()
        return await bot.resolve_user_id(username)

    def _get_live_token(self, use_broadcaster: bool = False) -> str:
        """Get the current valid token from TwitchIO's auto-refreshed storage."""
        bot = self._require_running_bot()
        tokens = bot.tokens

        if use_broadcaster:
            if bot._broadcaster_id and bot._broadcaster_id in tokens:
                return tokens[bot._broadcaster_id]["token"]
            if bot._broadcaster_token:
                return bot._broadcaster_token
            raise TwitchRuntimeUnavailable(
                "Broadcaster token not configured. Set TWITCH_BROADCASTER_TOKEN in .env.docker."
            )

        if bot._bot_user_id and bot._bot_user_id in tokens:
            return tokens[bot._bot_user_id]["token"]
        return bot._access_token

    async def helix_request(
        self,
        method: str,
        endpoint: str,
        *,
        use_broadcaster_token: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated Twitch Helix API request."""
        token = self._get_live_token(use_broadcaster=use_broadcaster_token)
        headers = {
            "Client-ID": self.client_id,
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        url = f"https://api.twitch.tv/helix/{endpoint}"
        async with httpx.AsyncClient() as client:
            return await client.request(method, url, headers=headers, **kwargs)


_TWITCH_RUNTIME = TwitchToolRuntime()


def get_twitch_runtime() -> TwitchToolRuntime:
    """Return the stable Twitch tool runtime service."""
    return _TWITCH_RUNTIME


def register_twitch_bot(bot: Any) -> None:
    """Register the active Twitch bot for tool calls."""
    _TWITCH_RUNTIME.register_bot(bot)


def unregister_twitch_bot(bot: Any | None = None) -> None:
    """Unregister the active Twitch bot for tool calls."""
    _TWITCH_RUNTIME.unregister_bot(bot)
