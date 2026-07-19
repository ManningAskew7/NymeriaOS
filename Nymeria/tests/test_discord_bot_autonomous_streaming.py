"""Regression tests for Discord autonomous SSE delivery."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from nymeria.triggers.discord_bot import NymeriaDiscordBot


class _FakeMessage:
    def __init__(self, content: Optional[str] = None, embed: Any = None):
        self.content = content or ""
        self.embeds = [embed] if embed is not None else []
        self.edits: list[str] = []

    async def edit(self, content: Optional[str] = None, embed: Any = None) -> None:
        if content is not None:
            self.content = content
            self.edits.append(content)
        if embed is not None:
            self.embeds = [embed]


class _FakeChannel:
    def __init__(self, channel_id: int = 456):
        self.id = channel_id
        self.messages: list[_FakeMessage] = []
        self.attachments: list[str] = []
        self.typing_count = 0

    async def send(self, content: Optional[str] = None, **kwargs) -> _FakeMessage:
        msg = _FakeMessage(content=content, embed=kwargs.get("embed"))
        self.messages.append(msg)
        return msg

    async def trigger_typing(self) -> None:
        self.typing_count += 1


def _bot_for(channel: _FakeChannel, *, show_tools: bool = False) -> NymeriaDiscordBot:
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot._autonomous_state = {}
    bot._show_tool_calls = {channel.id: show_tools}
    bot.get_channel = lambda channel_id: channel if channel_id == channel.id else None

    async def _fetch_channel(channel_id: int) -> Optional[_FakeChannel]:
        return channel if channel_id == channel.id else None

    async def _send_workspace_attachment(channel_arg: _FakeChannel, path: str) -> bool:
        channel_arg.attachments.append(path)
        return True

    bot.fetch_channel = _fetch_channel
    bot._send_workspace_attachment = _send_workspace_attachment
    return bot


async def _deliver(bot: NymeriaDiscordBot, events: list[dict]) -> None:
    for event in events:
        await bot._handle_sse_event(event)


def _event(event_type: str, **data: Any) -> dict:
    return {
        "type": event_type,
        "thread_id": "discord_123_456",
        **data,
    }


def test_discord_autonomous_streams_response_without_resending_completion():
    channel = _FakeChannel()
    bot = _bot_for(channel)

    asyncio.run(_deliver(bot, [
        _event("task_started", prompt="Do work"),
        _event("response", content="Hello "),
        _event("response", content="world"),
        _event("task_completed", content="Hello world"),
    ]))

    assert [msg.content for msg in channel.messages] == ["Hello world"]
    assert bot._autonomous_state == {}


def test_discord_autonomous_falls_back_to_completion_content_without_chunks():
    channel = _FakeChannel()
    bot = _bot_for(channel)

    asyncio.run(_deliver(bot, [
        _event("task_started", prompt="Do work"),
        _event("task_completed", content="Finished from aggregate content."),
    ]))

    assert [msg.content for msg in channel.messages] == [
        "Finished from aggregate content."
    ]
    assert bot._autonomous_state == {}


def test_discord_autonomous_tool_events_hidden_insert_separator_and_footer():
    channel = _FakeChannel()
    bot = _bot_for(channel, show_tools=False)

    asyncio.run(_deliver(bot, [
        _event("task_started", prompt="Do work"),
        _event("response", content="Before tool."),
        _event("tool_call", id="call-1", name="lookup", args={"q": "x"}),
        _event("tool_result", id="call-1", name="lookup", result="ok"),
        _event("response", content="After tool."),
        _event("task_completed", content="Before tool.After tool."),
    ]))

    assert len(channel.messages) == 1
    assert "Before tool." in channel.messages[0].content
    assert "After tool." in channel.messages[0].content
    assert ("\u2500" * 30) in channel.messages[0].content
    assert "Tool calls: 1" in channel.messages[0].content


def test_discord_autonomous_tool_events_visible_update_tool_embed():
    channel = _FakeChannel()
    bot = _bot_for(channel, show_tools=True)

    asyncio.run(_deliver(bot, [
        _event("task_started", prompt="Do work"),
        _event("response", content="Before tool."),
        _event("tool_call", id="call-1", name="lookup", args={"q": "x"}),
        _event("tool_result", id="call-1", name="lookup", result="ok"),
        _event("response", content="After tool."),
        _event("task_completed", content="Before tool.After tool."),
    ]))

    tool_messages = [msg for msg in channel.messages if msg.embeds]
    assert len(tool_messages) == 1
    assert tool_messages[0].embeds[0].title.endswith("lookup")
    assert tool_messages[0].embeds[0].fields[0].name == "Result"
    assert [msg.content for msg in channel.messages if msg.content] == [
        "Before tool.",
        "After tool.\n\n-# Tool calls: 1",
    ]


def test_discord_autonomous_deduplicates_workspace_artifacts():
    channel = _FakeChannel()
    bot = _bot_for(channel)
    path = "/workspace/report.csv"

    asyncio.run(_deliver(bot, [
        _event("task_started", prompt="Do work"),
        _event("tool_result", id="call-1", name="file_write", result=f"[attach:{path}]"),
        _event("workspace_artifact", path=path),
        _event("task_completed", content="Done."),
    ]))

    assert channel.attachments == [path]


def test_discord_autonomous_error_completion_clears_state():
    channel = _FakeChannel()
    bot = _bot_for(channel)

    asyncio.run(_deliver(bot, [
        _event("task_started", prompt="Do work"),
        _event("response", content="Partial"),
        _event("task_completed", error=True, content="boom"),
    ]))

    assert [msg.content for msg in channel.messages] == [
        "Partial",
        "Autonomous task error: boom",
    ]
    assert bot._autonomous_state == {}


def test_discord_autonomous_drops_fanout_mirror_events():
    """A queuer's mirror of the holder turn (stream_bridge fanout marker)
    must not render or re-deliver via the completion fallback; only the
    holder's own task delivers."""
    channel = _FakeChannel()
    bot = _bot_for(channel)

    briefing = "Morning briefing: all quiet."
    asyncio.run(_deliver(bot, [
        _event("task_started", task_id="todo-1", prompt="Do work"),
        _event("response", task_id="handoff-1", content=briefing, fanout=True),
        _event("response", task_id="todo-1", content=briefing),
        _event("task_completed", task_id="handoff-1", content=briefing, fanout=True),
        _event("task_completed", task_id="todo-1", content=briefing),
    ]))

    assert [msg.content for msg in channel.messages] == [briefing]
    assert bot._autonomous_state == {}
