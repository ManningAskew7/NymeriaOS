"""What the agent is told about WHO is speaking on Discord (#274).

The channel-context block names prior speakers but never the person being
answered, and an ``/ask`` interaction cannot appear in channel history at
all, so on a shared-account deployment (``DISCORD_DEFAULT_ACCOUNT``) the
agent could not tell who was asking. Covers the composer directly plus both
real handlers end to end.

Also covers the first-token guard: the chat route recognizes a backend
slash command only when the command is the message's FIRST token, so an
identity prefix (or the pre-existing context block) prepended to
``/compact`` silently demotes it to prose.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional

from nymeria.triggers.discord_bot import (
    NymeriaDiscordBot,
    compose_incoming_prompt,
    is_backend_command,
    sender_display_name,
)
from nymeria.triggers.discord_cogs.chat import ChatCog

CONTEXT = (
    "[Discord Channel Context: last 2 messages]\n"
    "[14:32] Alice: has anyone seen the deploy logs?\n"
    "[14:33] Bob: checking now\n"
    "[End of channel context]\n\n"
)


# =============================================================================
# The composer
# =============================================================================


def test_guild_message_names_the_asker():
    assert compose_incoming_prompt(
        "can you check?", sender_name="Alice"
    ) == "[Message from Alice]\ncan you check?"


def test_context_block_stays_first_and_intact():
    """The history comes first, the live question last and clearly marked."""
    composed = compose_incoming_prompt(
        "can you check?", sender_name="Alice", context=CONTEXT
    )
    assert composed == CONTEXT + "[Message from Alice]\ncan you check?"


def test_dm_is_not_prefixed():
    """1:1 conversation: the account already identifies the sender."""
    assert (
        compose_incoming_prompt("hey", sender_name="Alice", is_dm=True) == "hey"
    )
    assert (
        compose_incoming_prompt(
            "hey", sender_name="Alice", context=CONTEXT, is_dm=True
        )
        == CONTEXT + "hey"
    )


def test_backend_command_is_forwarded_verbatim():
    """A prefix would push /compact off the front and demote it to prose."""
    assert (
        compose_incoming_prompt("/compact", sender_name="Alice", context=CONTEXT)
        == "/compact"
    )
    assert (
        compose_incoming_prompt("/skill research", sender_name="Alice")
        == "/skill research"
    )
    assert (
        compose_incoming_prompt(
            "  /resume", sender_name="Alice", context=CONTEXT, is_dm=True
        )
        == "  /resume"
    )


def test_a_question_about_a_command_is_still_a_question():
    """Only a LEADING slash is a command; prose mentioning one is not."""
    assert compose_incoming_prompt(
        "what does /compact do?", sender_name="Alice"
    ) == "[Message from Alice]\nwhat does /compact do?"


def test_is_backend_command_keys_on_the_first_token():
    assert is_backend_command("/compact")
    assert is_backend_command("   /compact focus")
    assert not is_backend_command("what does /compact do?")
    assert not is_backend_command("")


def test_sender_display_name_prefers_the_nickname():
    """The same name the context block prints for that person."""
    assert (
        sender_display_name(SimpleNamespace(display_name="Alice", name="alice_1"))
        == "Alice"
    )
    assert (
        sender_display_name(SimpleNamespace(display_name="", name="alice_1"))
        == "alice_1"
    )


# =============================================================================
# on_message, end to end
# =============================================================================


GUILD = 123456789012345678


class _Channel:
    """A channel whose history is whatever the test hands it."""

    def __init__(self, channel_id: int = 555, history: Optional[list] = None):
        self.id = channel_id
        self.sent: list[str] = []
        self.history_calls = 0
        self._history = history or []

    async def send(self, content, **kwargs):
        self.sent.append(content)
        return SimpleNamespace(content=content)

    def history(self, **kwargs):
        self.history_calls += 1
        return _History(self._history)


class _History:
    def __init__(self, messages: list):
        self._iter = iter(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


def _prior(author: str, text: str, minute: int) -> SimpleNamespace:
    """A message already in the channel, as fetch_channel_context sees it."""
    return SimpleNamespace(
        author=SimpleNamespace(id=7, display_name=author),
        content=text,
        created_at=datetime(2026, 9, 1, 14, minute),
    )


def _message(
    guild_id: Optional[int],
    channel: _Channel,
    content: str = "hello there",
    display_name: str = "Alice",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=888,
        author=SimpleNamespace(id=42, bot=False, display_name=display_name),
        guild=SimpleNamespace(id=guild_id) if guild_id is not None else None,
        content=content,
        channel=channel,
        mentions=[],
    )


def _wired_bot(
    channel: _Channel,
    *,
    linked: Optional[str] = None,
    context_enabled: bool = False,
) -> tuple[NymeriaDiscordBot, list[dict]]:
    """A bot whose on_message runs the REAL resolve + compose wiring."""
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot._default_account = "mates"
    bot._default_account_guilds = {GUILD}

    async def _resolve(discord_user_id):
        return linked

    bot._user_resolver = SimpleNamespace(resolve=_resolve)
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=1))
    bot.respond_mode = "all"
    bot._context_enabled = {channel.id: context_enabled}

    async def _collect_attachments(message):
        return [], []

    dispatched: list[dict] = []

    async def _stream_to_channel(**kwargs):
        dispatched.append(kwargs)

    bot._collect_attachments = _collect_attachments
    bot._stream_to_channel = _stream_to_channel
    return bot, dispatched


def test_on_message_dispatches_the_askers_name():
    channel = _Channel()
    bot, dispatched = _wired_bot(channel)
    asyncio.run(bot.on_message(_message(GUILD, channel)))
    assert [d["message"] for d in dispatched] == [
        "[Message from Alice]\nhello there"
    ]
    # The rest of the payload is untouched.
    assert dispatched[0]["user_id"] == "mates"
    assert dispatched[0]["thread_id"] == f"discord_{GUILD}_555"


def test_on_message_dm_from_a_linked_user_is_not_prefixed():
    channel = _Channel()
    bot, dispatched = _wired_bot(channel, linked="alice")
    asyncio.run(bot.on_message(_message(None, channel)))
    assert [d["message"] for d in dispatched] == ["hello there"]


def test_on_message_command_survives_an_enabled_context_block():
    """Context ON used to bury the command token and demote it to prose."""
    channel = _Channel()
    bot, dispatched = _wired_bot(channel, context_enabled=True)
    asyncio.run(bot.on_message(_message(GUILD, channel, content="/compact")))
    assert [d["message"] for d in dispatched] == ["/compact"]
    # And the history call it would have discarded is never made.
    assert channel.history_calls == 0


def test_on_message_uses_the_username_when_there_is_no_nickname():
    channel = _Channel()
    bot, dispatched = _wired_bot(channel)
    message = _message(GUILD, channel, display_name="")
    message.author.name = "alice_1"
    asyncio.run(bot.on_message(message))
    assert dispatched[0]["message"] == "[Message from alice_1]\nhello there"


def test_on_message_joins_a_real_fetched_context_block_ahead_of_the_label():
    """The whole seam, with the REAL fetch: block first, then who is asking.

    The context string carries its own trailing blank line, which is the
    only thing separating it from the label, so this pins the join that
    a rewrite of fetch_channel_context could otherwise break unnoticed.
    """
    channel = _Channel(
        history=[  # Discord yields newest first; the fetcher reverses.
            _prior("Bob", "checking now", 33),
            _prior("Alice", "has anyone seen the deploy logs?", 32),
        ]
    )
    bot, dispatched = _wired_bot(channel, context_enabled=True)
    asyncio.run(bot.on_message(_message(GUILD, channel, content="any luck?")))
    assert dispatched[0]["message"] == (
        "[Discord Channel Context: last 2 messages]\n"
        "[14:32] Alice: has anyone seen the deploy logs?\n"
        "[14:33] Bob: checking now\n"
        "[End of channel context]\n"
        "\n"
        "[Message from Alice]\n"
        "any luck?"
    )


def test_on_message_strips_the_bot_mention_before_judging_a_command():
    """In respond_mode=all the mention is not the trigger, but it still
    leads the text, and a leading mention would hide the command token."""
    channel = _Channel()
    bot, dispatched = _wired_bot(channel, context_enabled=True)
    message = _message(GUILD, channel, content="<@1> /compact")
    asyncio.run(bot.on_message(message))
    assert [d["message"] for d in dispatched] == ["/compact"]
    assert channel.history_calls == 0


def test_on_message_strips_the_bot_mention_from_an_ordinary_question():
    channel = _Channel()
    bot, dispatched = _wired_bot(channel)
    asyncio.run(
        bot.on_message(_message(GUILD, channel, content="<@1> what's up?"))
    )
    assert [d["message"] for d in dispatched] == [
        "[Message from Alice]\nwhat's up?"
    ]


# =============================================================================
# /ask, end to end
# =============================================================================


class _FakeResponse:
    async def defer(self, **kwargs: Any) -> None:
        return None


class _FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str, **kwargs: Any) -> SimpleNamespace:
        self.sent.append(content)
        return SimpleNamespace(content=content)


class _FakeInteraction:
    def __init__(self, guild_id: Optional[int], channel: _Channel) -> None:
        self.user = SimpleNamespace(id=987, display_name="Alice")
        self.guild_id = guild_id
        self.channel_id = channel.id
        self.channel = channel
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def _ask_bot(
    channel: _Channel, *, context_enabled: bool = False
) -> tuple[NymeriaDiscordBot, list[dict]]:
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot._context_enabled = {channel.id: context_enabled}
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=1))
    dispatched: list[dict] = []

    async def _resolve_or_reject_interaction(interaction, **kwargs):
        return "mates"

    async def _stream_to_channel(**kwargs):
        dispatched.append(kwargs)

    bot._resolve_or_reject_interaction = _resolve_or_reject_interaction
    bot._stream_to_channel = _stream_to_channel
    return bot, dispatched


def _run_ask(bot: NymeriaDiscordBot, interaction: Any, text: str) -> None:
    asyncio.run(ChatCog.cmd_ask.callback(ChatCog(bot), interaction, text))


def test_ask_in_a_guild_names_the_asker():
    """An /ask is an interaction, so channel history can never name it."""
    channel = _Channel()
    bot, dispatched = _ask_bot(channel)
    _run_ask(bot, _FakeInteraction(GUILD, channel), "what's the deploy status?")
    assert [d["message"] for d in dispatched] == [
        "[Message from Alice]\nwhat's the deploy status?"
    ]


def test_ask_joins_a_real_fetched_context_block_ahead_of_the_label():
    channel = _Channel(history=[_prior("Bob", "checking now", 33)])
    bot, dispatched = _ask_bot(channel, context_enabled=True)
    _run_ask(bot, _FakeInteraction(GUILD, channel), "any luck?")
    assert dispatched[0]["message"] == (
        "[Discord Channel Context: last 1 messages]\n"
        "[14:33] Bob: checking now\n"
        "[End of channel context]\n"
        "\n"
        "[Message from Alice]\n"
        "any luck?"
    )


def test_ask_in_a_dm_is_not_prefixed():
    channel = _Channel()
    bot, dispatched = _ask_bot(channel)
    _run_ask(bot, _FakeInteraction(None, channel), "hey")
    assert [d["message"] for d in dispatched] == ["hey"]


def test_ask_forwards_a_command_verbatim():
    channel = _Channel()
    bot, dispatched = _ask_bot(channel, context_enabled=True)
    _run_ask(bot, _FakeInteraction(GUILD, channel), "/compact")
    assert [d["message"] for d in dispatched] == ["/compact"]
    assert channel.history_calls == 0
