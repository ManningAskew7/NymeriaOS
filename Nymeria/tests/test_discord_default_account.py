"""Unlinked-sender default-account fallback (DISCORD_DEFAULT_ACCOUNT).

Opt-in fallback for shared-server deployments: an unlinked Discord sender
in an allowlisted guild resolves to a configured account instead of being
rejected, with no platform link created. Covers the resolve seam directly
(explicit links win; DMs and non-allowlisted guilds keep today's rejection;
a resolver outage propagates rather than remapping identities) plus the
settings parsing in ``__init__``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Optional

import pytest

from nymeria.triggers.api_client import NymeriaAPIClient
from nymeria.triggers.bot_helpers import PlatformResolveUnavailableError
from nymeria.triggers.discord_bot import NymeriaDiscordBot

GUILD = 123456789012345678
OTHER_GUILD = 111111111111111111


def _bot(
    *,
    linked: Optional[str],
    default_account: Optional[str] = "mates",
    guilds: Optional[set[int]] = None,
    unavailable: bool = False,
) -> NymeriaDiscordBot:
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot._default_account = default_account
    bot._default_account_guilds = {GUILD} if guilds is None else set(guilds)

    async def _resolve(discord_user_id):
        if unavailable:
            raise PlatformResolveUnavailableError(
                "discord", RuntimeError("backend down")
            )
        return linked

    bot._user_resolver = SimpleNamespace(resolve=_resolve)
    return bot


def test_unlinked_sender_in_allowlisted_guild_gets_default_account():
    bot = _bot(linked=None)
    assert asyncio.run(bot.resolve_user_id(42, guild_id=GUILD)) == "mates"


def test_dm_never_falls_back():
    bot = _bot(linked=None)
    assert asyncio.run(bot.resolve_user_id(42, guild_id=None)) is None


def test_non_allowlisted_guild_keeps_rejection():
    bot = _bot(linked=None)
    assert asyncio.run(bot.resolve_user_id(42, guild_id=OTHER_GUILD)) is None


def test_no_default_account_behaves_as_before():
    bot = _bot(linked=None, default_account=None)
    assert asyncio.run(bot.resolve_user_id(42, guild_id=GUILD)) is None


def test_empty_guild_allowlist_disables_fallback():
    bot = _bot(linked=None, guilds=set())
    assert asyncio.run(bot.resolve_user_id(42, guild_id=GUILD)) is None


def test_explicit_link_wins_over_default_account():
    bot = _bot(linked="alice")
    assert asyncio.run(bot.resolve_user_id(42, guild_id=GUILD)) == "alice"


def test_resolver_outage_propagates_instead_of_falling_back():
    bot = _bot(linked=None, unavailable=True)
    with pytest.raises(PlatformResolveUnavailableError):
        asyncio.run(bot.resolve_user_id(42, guild_id=GUILD))


class _Channel:
    def __init__(self, channel_id: int = 555):
        self.id = channel_id
        self.sent: list[str] = []

    async def send(self, content, **kwargs):
        self.sent.append(content)
        return SimpleNamespace(content=content)


def _message(guild_id: Optional[int], channel: _Channel) -> SimpleNamespace:
    return SimpleNamespace(
        id=888,
        author=SimpleNamespace(id=42, bot=False, display_name="Alice"),
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
        content="hello there",
        channel=channel,
        mentions=[],
    )


def _wired_bot(channel: _Channel) -> tuple[NymeriaDiscordBot, list[dict]]:
    """A bot whose on_message runs the REAL resolve_user_id wiring."""
    bot = _bot(linked=None)
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=1))
    bot.respond_mode = "all"
    bot._context_enabled = {channel.id: False}

    async def _collect_attachments(message):
        return [], []

    dispatched: list[dict] = []

    async def _stream_to_channel(**kwargs):
        dispatched.append(kwargs)

    bot._collect_attachments = _collect_attachments
    bot._stream_to_channel = _stream_to_channel
    return bot, dispatched


def test_on_message_threads_guild_context_into_the_fallback():
    """The chat path must pass its guild id, or the fallback never fires."""
    channel = _Channel()
    bot, dispatched = _wired_bot(channel)
    asyncio.run(bot.on_message(_message(GUILD, channel)))
    assert [d["user_id"] for d in dispatched] == ["mates"]
    assert channel.sent == []  # no rejection reply


def test_on_message_dm_still_rejects_unlinked():
    channel = _Channel()
    bot, dispatched = _wired_bot(channel)
    asyncio.run(bot.on_message(_message(None, channel)))
    assert dispatched == []
    assert len(channel.sent) == 1 and "isn't linked" in channel.sent[0]


def test_init_parses_default_account_settings(monkeypatch):
    from nymeria.config.settings import get_settings

    monkeypatch.setenv("DISCORD_DEFAULT_ACCOUNT", "mates")
    monkeypatch.setenv(
        "DISCORD_DEFAULT_ACCOUNT_GUILDS", f"{GUILD}, 987654321, junk"
    )
    get_settings.cache_clear()
    try:
        bot = NymeriaDiscordBot(
            api=NymeriaAPIClient(base_url="http://localhost:1", api_key="test"),
            respond_mode="mention",
        )
        assert bot._default_account == "mates"
        assert bot._default_account_guilds == {GUILD, 987654321}
    finally:
        get_settings.cache_clear()


def test_init_defaults_leave_fallback_off(monkeypatch):
    from nymeria.config.settings import get_settings

    monkeypatch.delenv("DISCORD_DEFAULT_ACCOUNT", raising=False)
    monkeypatch.delenv("DISCORD_DEFAULT_ACCOUNT_GUILDS", raising=False)
    get_settings.cache_clear()
    try:
        bot = NymeriaDiscordBot(
            api=NymeriaAPIClient(base_url="http://localhost:1", api_key="test"),
            respond_mode="mention",
        )
        assert bot._default_account is None
        assert bot._default_account_guilds == set()
    finally:
        get_settings.cache_clear()
