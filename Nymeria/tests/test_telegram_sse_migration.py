"""Regression tests for the Telegram autonomous SSE-dispatch migration.

Slice 21 F2: the Telegram autonomous handler (`_handle_sse_event`) now routes
standard SSE event types through the shared ``sse_consumer.dispatch_event``
(the same dispatcher the interactive ``_ChatSSEHandler`` and the Discord bot
use) into a per-thread ``_AutonomousSSEHandler``, instead of a hand-rolled
if/elif chain. These tests lock the protocol compliance, the structural
delegation, and the per-event behavior (including the two intended consistency
deltas: mid-stream ``error`` and ``dispatched`` events are now surfaced).
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

from nymeria.triggers.telegram_bot import NymeriaTelegramBot


# ---------------------------------------------------------------------------
# Protocol compliance + structural guards
# ---------------------------------------------------------------------------


def test_autonomous_handler_implements_sse_protocol():
    """_AutonomousSSEHandler must satisfy the SSEEventHandler protocol."""
    handler_cls = NymeriaTelegramBot._AutonomousSSEHandler
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
        # Telegram-specific optional callback dispatch_event routes auth_prompt to.
        "on_auth_prompt",
    ):
        method = getattr(handler_cls, name, None)
        assert method is not None, f"Missing {name}"
        assert callable(method), f"{name} is not callable"
        assert inspect.iscoroutinefunction(method), f"{name} must be async"


def test_handle_sse_event_uses_dispatch_event():
    """_handle_sse_event must delegate standard events to dispatch_event."""
    src = inspect.getsource(NymeriaTelegramBot._handle_sse_event)
    assert "dispatch_event" in src, "_handle_sse_event should call dispatch_event()"


def test_handle_sse_event_has_no_inline_standard_dispatch():
    """Standard SSE types must no longer be string-matched inline.

    Only the autonomous-only types (notification, task_started, task_completed)
    may be matched in `_handle_sse_event`; everything else goes through
    dispatch_event.
    """
    src = inspect.getsource(NymeriaTelegramBot._handle_sse_event)
    for event_type in (
        "response",
        "thinking",
        "compacting",
        "compacted",
        "context_attached",
        "tool_call",
        "tool_result",
        "tool_reload",
        "workspace_artifact",
        "iteration_limit",
        "auth_prompt",
    ):
        assert f'== "{event_type}"' not in src, (
            f"_handle_sse_event should not inline-check '{event_type}' events"
        )


# ---------------------------------------------------------------------------
# Functional scaffolding
# ---------------------------------------------------------------------------


class _FakeTelegramBot:
    """Stand-in for the python-telegram-bot `Bot` object."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: Any = None, text: str = "", **kwargs) -> Any:
        self.sent.append(text)
        return SimpleNamespace(chat_id=chat_id, text=text)


class _FakeAPI:
    def __init__(self, mode: str = "full") -> None:
        self.mode = mode

    async def get_thread_config(self, thread_id: str) -> dict:
        return {"telegram_autonomous_delivery": self.mode}


def _make_bot(
    *, mode: str = "full", show_tools: bool = False, chat_id: int = 123
) -> tuple[NymeriaTelegramBot, _FakeTelegramBot]:
    bot = NymeriaTelegramBot(api=_FakeAPI(mode), bot_token="test-token")
    fake = _FakeTelegramBot()
    bot._application = SimpleNamespace(bot=fake)
    bot._show_tool_calls = {chat_id: show_tools}
    return bot, fake


def _event(event_type: str, *, thread_id: str = "telegram_123", **data: Any) -> dict:
    return {"type": event_type, "thread_id": thread_id, **data}


async def _deliver(bot: NymeriaTelegramBot, events: list[dict]) -> None:
    for event in events:
        await bot._handle_sse_event(event)


# ---------------------------------------------------------------------------
# Functional: standard event routing via dispatch_event
# ---------------------------------------------------------------------------


def test_autonomous_routes_response_through_dispatch():
    bot, fake = _make_bot()

    asyncio.run(_deliver(bot, [
        _event("task_started"),
        _event("response", content="Hello "),
        _event("response", content="world"),
        _event("task_completed", content="Hello world"),
    ]))

    assert len(fake.sent) == 1
    assert "Hello world" in fake.sent[0]
    # No response duplication from the task_completed fallback.
    assert fake.sent[0].count("Hello world") == 1
    assert bot._autonomous_state == {}


def test_autonomous_tool_call_rendered_when_shown():
    bot, fake = _make_bot(show_tools=True)

    asyncio.run(_deliver(bot, [
        _event("task_started"),
        _event("tool_call", id="c1", name="web_search", args={"q": "test"}),
        _event("tool_result", id="c1", result="found"),
        _event("response", content="Done."),
        _event("task_completed"),
    ]))

    joined = "\n".join(fake.sent)
    assert "web_search" in joined
    assert "Done." in joined
    assert "Tool calls: 1" in fake.sent[-1]
    assert bot._autonomous_state == {}


def test_autonomous_tool_count_synced_through_dispatch():
    """dispatch_event's return value must thread back into handler._tool_count."""
    bot, fake = _make_bot(show_tools=False)

    asyncio.run(_deliver(bot, [
        _event("task_started"),
        _event("tool_call", id="c1", name="a", args={}),
        _event("tool_call", id="c2", name="b", args={}),
        _event("response", content="Result"),
        _event("task_completed"),
    ]))

    assert "Tool calls: 2" in fake.sent[-1]


def test_autonomous_compaction_titles_preserved():
    bot, fake = _make_bot()

    asyncio.run(_deliver(bot, [
        _event("task_started"),
        _event("compacted", messages_removed=7, summary="Prior task state."),
        _event("context_attached", summary="Attached summary."),
        _event("iteration_limit", content="Stopped after too many tool calls."),
        _event("task_completed", content=""),
    ]))

    joined = "\n".join(fake.sent)
    assert "Context compacted" in joined and "Prior task state." in joined
    assert "7 messages summarized" in joined
    assert "Context summary attached" in joined and "Attached summary." in joined
    assert "Stopped after too many tool calls." in joined


def test_autonomous_auth_prompt_uses_custom_telegram_rendering():
    """auth_prompt must route to the handler's on_auth_prompt (escape + <br>),

    not the dispatcher's default buffered/markdown path.
    """
    bot, fake = _make_bot()

    asyncio.run(_deliver(bot, [
        _event("task_started"),
        _event(
            "auth_prompt",
            display_name="GitHub",
            connect_url="https://example.test/connect",
        ),
        _event("task_completed", content=""),
    ]))

    auth_msgs = [m for m in fake.sent if "example.test/connect" in m]
    assert auth_msgs, "auth prompt was not delivered"
    msg = auth_msgs[0]
    assert "<br>" in msg  # newline -> <br> substitution from on_auth_prompt
    assert "\n" not in msg  # the custom render replaces newlines, never buffers them
    assert "GitHub" in msg


def test_autonomous_footer_only_flush_emits_tool_call_footer():
    """No response text + N tool calls must still emit the footer bubble."""
    bot, fake = _make_bot(show_tools=False)

    asyncio.run(_deliver(bot, [
        _event("task_started"),
        _event("tool_call", id="c1", name="a", args={}),
        _event("task_completed", content=""),
    ]))

    assert len(fake.sent) == 1
    assert "Tool calls: 1" in fake.sent[0]


# ---------------------------------------------------------------------------
# Intended consistency deltas: error / dispatched now surface
# ---------------------------------------------------------------------------


def test_autonomous_midstream_error_now_surfaces():
    """Intended delta: a mid-stream `error` event is now rendered (was dropped)."""
    bot, fake = _make_bot()

    asyncio.run(_deliver(bot, [
        _event("task_started"),
        _event("error", content="boom"),
        _event("task_completed", content=""),
    ]))

    assert any("boom" in m for m in fake.sent)


def test_autonomous_dispatched_reference_now_surfaces():
    """Intended delta: a `dispatched` event now renders a thread reference."""
    bot, fake = _make_bot()

    asyncio.run(_deliver(bot, [
        _event("task_started"),
        _event("dispatched", title="Research"),
        _event("task_completed", content=""),
    ]))

    assert any("Response from Research" in m for m in fake.sent)


# ---------------------------------------------------------------------------
# Delivery-mode gates (preserved inline)
# ---------------------------------------------------------------------------


def test_autonomous_delivery_off_suppresses_and_pops_state():
    bot, fake = _make_bot(mode="off")

    asyncio.run(_deliver(bot, [
        _event("task_started"),
        _event("response", content="should not appear"),
        _event("task_completed", content="done"),
    ]))

    assert fake.sent == []
    assert bot._autonomous_state == {}


def test_autonomous_notify_only_delivers_notification_not_stream():
    bot, fake = _make_bot(mode="notify_only")

    asyncio.run(_deliver(bot, [
        _event("notification", message="Task finished summary"),
        _event("response", content="streamed body text"),
        _event("task_completed", content="done"),
    ]))

    joined = "\n".join(fake.sent)
    assert "Task finished summary" in joined
    assert "streamed body text" not in joined


def test_autonomous_notify_only_surfaces_completion_error():
    bot, fake = _make_bot(mode="notify_only")

    asyncio.run(_deliver(bot, [
        _event("task_completed", error=True, content="it failed"),
    ]))

    assert any("it failed" in m for m in fake.sent)
    assert bot._autonomous_state == {}


# ---------------------------------------------------------------------------
# Routing: foreign / unresolved threads are dropped
# ---------------------------------------------------------------------------


def test_autonomous_foreign_thread_is_dropped():
    bot, fake = _make_bot()

    asyncio.run(_deliver(bot, [
        _event("response", thread_id="discord_999_111", content="not ours"),
    ]))

    assert fake.sent == []
    assert bot._autonomous_state == {}
