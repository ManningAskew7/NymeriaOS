"""Discord emoji-reaction tests (backlog #45): inbound trigger, outbound
reaction_request execution, and reply suppression in both SSE handlers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from nymeria.triggers.discord_bot import NymeriaDiscordBot

BOT_USER_ID = 999


class _FakeMessage:
    def __init__(self, author_id: int, content: str = "", message_id: int = 777):
        self.id = message_id
        self.author = SimpleNamespace(id=author_id)
        self.content = content
        self.reactions_added: List[str] = []

    async def add_reaction(self, emoji: str) -> None:
        self.reactions_added.append(emoji)


class _FakeChannel:
    def __init__(self, channel_id: int, message: Optional[_FakeMessage] = None):
        self.id = channel_id
        self.message = message
        self.sent: List[str] = []

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        if self.message is None or self.message.id != message_id:
            raise RuntimeError("message not found")
        return self.message

    async def send(self, content: str, **kwargs) -> Any:
        self.sent.append(content)
        return SimpleNamespace(content=content)


def _reaction_bot(
    channel: _FakeChannel,
    *,
    linked_user: Optional[str] = "u1",
) -> tuple[NymeriaDiscordBot, List[Dict[str, Any]]]:
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    # Client.user is a read-only property over the gateway connection state.
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=BOT_USER_ID))
    bot.get_channel = lambda channel_id: channel if channel_id == channel.id else None
    bot.get_user = lambda uid: SimpleNamespace(display_name="Alice")

    async def _fetch_channel(channel_id: int):
        return channel if channel_id == channel.id else None

    async def _resolve_user_id(discord_user_id: int) -> Optional[str]:
        return linked_user

    dispatched: List[Dict[str, Any]] = []

    async def _stream_to_channel(**kwargs):
        dispatched.append(kwargs)

    bot.fetch_channel = _fetch_channel
    bot.resolve_user_id = _resolve_user_id
    bot._stream_to_channel = _stream_to_channel
    return bot, dispatched


def _payload(
    *,
    user_id: int = 5,
    channel_id: int = 456,
    message_id: int = 777,
    guild_id: Optional[int] = 123,
    emoji: Any = "👍",
    member: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user_id,
        channel_id=channel_id,
        message_id=message_id,
        guild_id=guild_id,
        emoji=emoji,
        member=member,
    )


def _enable_toggle(monkeypatch, enabled: bool = True) -> None:
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(discord_reaction_trigger_enabled=enabled),
    )


# ---------------------------------------------------------------------------
# Inbound trigger
# ---------------------------------------------------------------------------


def test_reaction_on_bot_message_fires_turn(monkeypatch):
    _enable_toggle(monkeypatch)
    channel = _FakeChannel(456, _FakeMessage(BOT_USER_ID, "Deploy finished. All good."))
    bot, dispatched = _reaction_bot(channel)
    member = SimpleNamespace(bot=False, display_name="Alice")

    asyncio.run(bot.on_raw_reaction_add(_payload(member=member)))

    assert len(dispatched) == 1
    call = dispatched[0]
    assert call["thread_id"] == "discord_123_456"
    assert call["user_id"] == "u1"
    assert call["is_self_invoke"] is True
    assert call["trigger_override"] == "reaction"
    assert call["source"] == "trigger"
    assert call["publish_autonomous_events"] is False
    assert call["platform_origin"] == {
        "platform": "discord",
        "channel_id": "456",
        "message_id": "777",
        "kind": "reaction",
    }
    prompt = call["message"]
    assert prompt.startswith("[Reaction] Alice reacted with 👍")
    assert "Deploy finished. All good." in prompt


def test_reaction_dm_thread_id_and_fallback_name(monkeypatch):
    _enable_toggle(monkeypatch)
    channel = _FakeChannel(456, _FakeMessage(BOT_USER_ID, "hi"))
    bot, dispatched = _reaction_bot(channel)

    asyncio.run(bot.on_raw_reaction_add(_payload(guild_id=None, member=None)))

    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == "discord_dm_456"
    assert dispatched[0]["message"].startswith("[Reaction] Alice reacted with 👍")


def test_toggle_off_drops_reaction(monkeypatch):
    _enable_toggle(monkeypatch, enabled=False)
    channel = _FakeChannel(456, _FakeMessage(BOT_USER_ID, "hi"))
    bot, dispatched = _reaction_bot(channel)

    asyncio.run(bot.on_raw_reaction_add(_payload()))
    assert dispatched == []


def test_loop_guard_drops_bots_own_reaction(monkeypatch):
    _enable_toggle(monkeypatch)
    channel = _FakeChannel(456, _FakeMessage(BOT_USER_ID, "hi"))
    bot, dispatched = _reaction_bot(channel)

    asyncio.run(bot.on_raw_reaction_add(_payload(user_id=BOT_USER_ID)))
    assert dispatched == []


def test_loop_guard_drops_other_bot_member(monkeypatch):
    _enable_toggle(monkeypatch)
    channel = _FakeChannel(456, _FakeMessage(BOT_USER_ID, "hi"))
    bot, dispatched = _reaction_bot(channel)
    other_bot = SimpleNamespace(bot=True, display_name="OtherBot")

    asyncio.run(bot.on_raw_reaction_add(_payload(member=other_bot)))
    assert dispatched == []


def test_reaction_on_non_bot_message_is_ignored(monkeypatch):
    _enable_toggle(monkeypatch)
    channel = _FakeChannel(456, _FakeMessage(author_id=5, content="a user message"))
    bot, dispatched = _reaction_bot(channel)

    asyncio.run(bot.on_raw_reaction_add(_payload()))
    assert dispatched == []


def test_unlinked_reactor_dropped_silently(monkeypatch):
    _enable_toggle(monkeypatch)
    channel = _FakeChannel(456, _FakeMessage(BOT_USER_ID, "hi"))
    bot, dispatched = _reaction_bot(channel, linked_user=None)

    asyncio.run(bot.on_raw_reaction_add(_payload()))
    assert dispatched == []
    assert channel.sent == []  # no onboarding spam in the channel


def test_custom_emoji_renders_as_name(monkeypatch):
    _enable_toggle(monkeypatch)
    channel = _FakeChannel(456, _FakeMessage(BOT_USER_ID, "hi"))
    bot, dispatched = _reaction_bot(channel)
    custom = SimpleNamespace(id=42, name="pepe")

    asyncio.run(bot.on_raw_reaction_add(_payload(emoji=custom)))
    assert len(dispatched) == 1
    assert ":pepe:" in dispatched[0]["message"]


def test_reaction_excerpt_truncates_and_collapses():
    excerpt = NymeriaDiscordBot._reaction_excerpt("line one\nline   two\t" + "x" * 300)
    assert "\n" not in excerpt
    assert len(excerpt) <= 200
    assert excerpt.endswith("...")


# ---------------------------------------------------------------------------
# Outbound reaction_request execution
# ---------------------------------------------------------------------------


def test_reaction_request_adds_reaction():
    message = _FakeMessage(BOT_USER_ID, "hi")
    channel = _FakeChannel(456, message)
    bot, _ = _reaction_bot(channel)

    asyncio.run(bot._handle_sse_event({
        "type": "reaction_request",
        "thread_id": "discord_123_456",
        "platform": "discord",
        "channel_id": "456",
        "message_id": "777",
        "emoji": "👍",
    }))
    assert message.reactions_added == ["👍"]


def test_reaction_request_other_platform_ignored():
    message = _FakeMessage(BOT_USER_ID, "hi")
    channel = _FakeChannel(456, message)
    bot, _ = _reaction_bot(channel)

    asyncio.run(bot._handle_sse_event({
        "type": "reaction_request",
        "thread_id": "telegram_555",
        "platform": "telegram",
        "channel_id": "555",
        "message_id": "10",
        "emoji": "👍",
    }))
    assert message.reactions_added == []


def test_reaction_request_missing_message_is_best_effort():
    channel = _FakeChannel(456, None)
    bot, _ = _reaction_bot(channel)

    # Must not raise: delivery is best-effort like notifications.
    asyncio.run(bot._handle_sse_event({
        "type": "reaction_request",
        "thread_id": "discord_123_456",
        "platform": "discord",
        "channel_id": "456",
        "message_id": "777",
        "emoji": "👍",
    }))


# ---------------------------------------------------------------------------
# Reply suppression: interactive handler
# ---------------------------------------------------------------------------


class _SinkChannel:
    def __init__(self, channel_id: int = 456):
        self.id = channel_id
        self.sent: List[str] = []

    async def send(self, content: str = "", **kwargs) -> Any:
        self.sent.append(content)
        msg = SimpleNamespace(content=content)

        async def _edit(content: str = "", **kw):
            msg.content = content

        msg.edit = _edit
        return msg

    async def trigger_typing(self) -> None:
        return None


def _interactive_handler(channel: _SinkChannel):
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot._show_tool_calls = {}

    async def _first_send(content: str):
        return await channel.send(content)

    return NymeriaDiscordBot._InteractiveChatHandler(bot, channel, _first_send)


def test_interactive_suppression_drops_pending_and_later_text():
    from nymeria.triggers.sse_consumer import consume_sse_stream

    channel = _SinkChannel()
    handler = _interactive_handler(channel)

    async def _events():
        yield {"type": "response", "content": "Before."}
        yield {"type": "reply_suppressed"}
        yield {"type": "response", "content": "This must never post."}
        yield {"type": "done"}

    asyncio.run(consume_sse_stream(_events(), handler))
    # Pre-suppression text may have flushed (forward-looking suppression);
    # nothing after the marker ever posts.
    assert all("never post" not in text for text in channel.sent)
    assert handler._reply_suppressed is True
    assert handler._text_buffer == ""


def test_interactive_suppression_before_any_text_posts_nothing():
    from nymeria.triggers.sse_consumer import consume_sse_stream

    channel = _SinkChannel()
    handler = _interactive_handler(channel)

    async def _events():
        yield {"type": "tool_call", "id": "c1", "name": "react", "args": {}}
        yield {"type": "tool_result", "id": "c1", "result": "Reacted with 👍."}
        yield {"type": "reply_suppressed"}
        yield {"type": "response", "content": "Hidden reply."}
        yield {"type": "done"}

    asyncio.run(consume_sse_stream(_events(), handler))
    assert channel.sent == []


# ---------------------------------------------------------------------------
# Reply suppression: autonomous handler
# ---------------------------------------------------------------------------


def _autonomous_bot(channel: _SinkChannel) -> NymeriaDiscordBot:
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot._autonomous_state = {}
    bot._show_tool_calls = {}
    bot.get_channel = lambda channel_id: channel if channel_id == channel.id else None

    async def _fetch_channel(channel_id: int):
        return channel if channel_id == channel.id else None

    bot.fetch_channel = _fetch_channel
    return bot


def test_autonomous_suppression_drops_reply_and_fallback():
    channel = _SinkChannel()
    bot = _autonomous_bot(channel)

    events = [
        {"type": "task_started", "thread_id": "discord_123_456", "prompt": "p"},
        {"type": "tool_call", "thread_id": "discord_123_456", "id": "c1",
         "name": "react", "args": {}},
        {"type": "tool_result", "thread_id": "discord_123_456", "id": "c1",
         "result": "Reacted."},
        {"type": "reply_suppressed", "thread_id": "discord_123_456"},
        {"type": "response", "thread_id": "discord_123_456", "content": "Hidden."},
        {"type": "task_completed", "thread_id": "discord_123_456",
         "content": "Hidden."},
    ]

    async def _run():
        for event in events:
            await bot._handle_sse_event(event)

    asyncio.run(_run())
    assert channel.sent == []
    assert bot._autonomous_state == {}
