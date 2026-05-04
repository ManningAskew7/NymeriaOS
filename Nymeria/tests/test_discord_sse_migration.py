"""Regression tests for Discord SSE consumer migration (TRIG-008).

Verifies that both interactive and autonomous Discord handlers implement
the ``SSEEventHandler`` protocol and that the bot routes standard event
types through the shared ``sse_consumer.dispatch_event`` instead of
duplicating dispatch logic.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Optional

from nymeria.triggers.discord_bot import NymeriaDiscordBot
from nymeria.triggers.sse_consumer import SSEEventHandler


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_interactive_handler_implements_sse_protocol():
    """_InteractiveChatHandler must satisfy the SSEEventHandler protocol."""
    handler_cls = NymeriaDiscordBot._InteractiveChatHandler
    assert issubclass(handler_cls, SSEEventHandler) or isinstance(
        handler_cls, type
    )
    for name in (
        "flush_text",
        "on_thinking",
        "on_response_chunk",
        "on_compacting",
        "on_compacted",
        "on_tool_call",
        "on_tool_result",
        "on_tool_reload",
        "on_workspace_artifact",
        "on_error",
        "on_iteration_limit",
        "on_done",
        "on_stream_end",
    ):
        method = getattr(handler_cls, name, None)
        assert method is not None, f"Missing {name}"
        assert callable(method), f"{name} is not callable"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_autonomous_handler_implements_sse_protocol():
    """_AutonomousSSEHandler must satisfy the SSEEventHandler protocol."""
    handler_cls = NymeriaDiscordBot._AutonomousSSEHandler
    for name in (
        "flush_text",
        "on_thinking",
        "on_response_chunk",
        "on_compacting",
        "on_compacted",
        "on_tool_call",
        "on_tool_result",
        "on_tool_reload",
        "on_workspace_artifact",
        "on_error",
        "on_iteration_limit",
        "on_done",
        "on_stream_end",
    ):
        method = getattr(handler_cls, name, None)
        assert method is not None, f"Missing {name}"
        assert callable(method), f"{name} is not callable"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


# ---------------------------------------------------------------------------
# Static guard: _stream_to_channel uses consume_sse_stream
# ---------------------------------------------------------------------------


def test_stream_to_channel_uses_consume_sse_stream():
    """_stream_to_channel must delegate to consume_sse_stream, not inline dispatch."""
    src = inspect.getsource(NymeriaDiscordBot._stream_to_channel)
    assert "consume_sse_stream" in src, (
        "_stream_to_channel should call consume_sse_stream()"
    )
    assert 'event.get("type"' not in src, (
        "_stream_to_channel should not manually inspect event types"
    )


def test_handle_sse_event_uses_dispatch_event():
    """_handle_sse_event must delegate standard events to dispatch_event."""
    src = inspect.getsource(NymeriaDiscordBot._handle_sse_event)
    assert "dispatch_event" in src, (
        "_handle_sse_event should call dispatch_event()"
    )


# ---------------------------------------------------------------------------
# No inline event type dispatch in handlers
# ---------------------------------------------------------------------------


def test_no_event_type_dispatch_in_stream_to_channel():
    """The streaming method must not have inline event-type if/elif chains."""
    src = inspect.getsource(NymeriaDiscordBot._stream_to_channel)
    for event_type in ("thinking", "compacting", "compacted", "tool_call",
                       "tool_result", "tool_reload", "workspace_artifact"):
        assert f'== "{event_type}"' not in src, (
            f"_stream_to_channel should not check for '{event_type}' events"
        )


# ---------------------------------------------------------------------------
# Functional: Interactive handler buffers and flushes
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, content: Optional[str] = None, embed: Any = None):
        self.content = content or ""
        self.embeds = [embed] if embed is not None else []

    async def edit(self, content: Optional[str] = None, embed: Any = None) -> None:
        if content is not None:
            self.content = content
        if embed is not None:
            self.embeds = [embed]


class _FakeChannel:
    def __init__(self, channel_id: int = 456):
        self.id = channel_id
        self.messages: list[_FakeMessage] = []
        self.typing_count = 0

    async def send(self, content: Optional[str] = None, **kwargs) -> _FakeMessage:
        msg = _FakeMessage(content=content, embed=kwargs.get("embed"))
        self.messages.append(msg)
        return msg

    async def trigger_typing(self) -> None:
        self.typing_count += 1


def _make_interactive_handler(channel: _FakeChannel):
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot._show_tool_calls = {channel.id: False}

    async def _send_workspace_attachment(ch: Any, path: str) -> bool:
        return True

    bot._send_workspace_attachment = _send_workspace_attachment

    async def first_send(content: str) -> _FakeMessage:
        msg = _FakeMessage(content=content)
        channel.messages.append(msg)
        return msg

    return NymeriaDiscordBot._InteractiveChatHandler(bot, channel, first_send)


def test_interactive_handler_buffers_response():
    channel = _FakeChannel()
    handler = _make_interactive_handler(channel)

    async def run():
        await handler.on_response_chunk("Hello ")
        await handler.on_response_chunk("world")
        await handler.on_done(0)

    asyncio.run(run())
    assert len(channel.messages) == 1
    assert channel.messages[0].content == "Hello world"


def test_interactive_handler_tool_separator_when_hidden():
    channel = _FakeChannel()
    handler = _make_interactive_handler(channel)

    async def run():
        await handler.on_response_chunk("Before.")
        await handler.on_tool_result("call-1", "ok", [])
        await handler.on_response_chunk("After.")
        await handler.on_done(1)

    asyncio.run(run())
    assert len(channel.messages) == 1
    content = channel.messages[0].content
    assert "Before." in content
    assert "After." in content
    assert "──────" in content
    assert "Tool calls: 1" in content


def test_interactive_handler_stream_end_flushes():
    channel = _FakeChannel()
    handler = _make_interactive_handler(channel)

    async def run():
        await handler.on_response_chunk("Partial")
        await handler.on_stream_end(0)

    asyncio.run(run())
    assert len(channel.messages) == 1
    assert channel.messages[0].content == "Partial"


# ---------------------------------------------------------------------------
# Functional: Autonomous handler with dispatch_event
# ---------------------------------------------------------------------------


def _make_autonomous_setup(*, show_tools: bool = False):
    channel = _FakeChannel()
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot._autonomous_state = {}
    bot._show_tool_calls = {channel.id: show_tools}
    bot.get_channel = lambda cid: channel if cid == channel.id else None

    async def _fetch(cid: int):
        return channel if cid == channel.id else None

    async def _send_ws(ch: Any, path: str) -> bool:
        ch.messages.append(_FakeMessage(content=f"[file:{path}]"))
        return True

    bot.fetch_channel = _fetch
    bot._send_workspace_attachment = _send_ws
    return bot, channel


def _event(event_type: str, **data: Any) -> dict:
    return {"type": event_type, "thread_id": "discord_123_456", **data}


def test_autonomous_dispatch_event_routes_response():
    bot, channel = _make_autonomous_setup()

    async def run():
        for e in [
            _event("task_started"),
            _event("response", content="Hello "),
            _event("response", content="world"),
            _event("task_completed", content="Hello world"),
        ]:
            await bot._handle_sse_event(e)

    asyncio.run(run())
    assert [m.content for m in channel.messages] == ["Hello world"]
    assert bot._autonomous_state == {}


def test_autonomous_dispatch_event_routes_tool_calls():
    bot, channel = _make_autonomous_setup(show_tools=True)

    async def run():
        for e in [
            _event("task_started"),
            _event("tool_call", id="c1", name="web_search", args={"q": "test"}),
            _event("tool_result", id="c1", result="found"),
            _event("response", content="Done."),
            _event("task_completed"),
        ]:
            await bot._handle_sse_event(e)

    asyncio.run(run())
    tool_msgs = [m for m in channel.messages if m.embeds]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].embeds[0].title.endswith("web_search")


def test_autonomous_tool_count_synced_through_dispatch():
    """dispatch_event return value must update handler._tool_count."""
    bot, channel = _make_autonomous_setup()

    async def run():
        for e in [
            _event("task_started"),
            _event("tool_call", id="c1", name="a", args={}),
            _event("tool_call", id="c2", name="b", args={}),
            _event("response", content="Result"),
            _event("task_completed"),
        ]:
            await bot._handle_sse_event(e)

    asyncio.run(run())
    assert "Tool calls: 2" in channel.messages[0].content
