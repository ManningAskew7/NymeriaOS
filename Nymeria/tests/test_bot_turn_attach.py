"""Tests for attach-preferred autonomous bot delivery (backlog #91 phase 1).

The shared ``AutonomousTurnAttach`` renders a holder turn from
``GET /threads/{id}/turn/stream`` through the bot's per-thread handler,
suppressing the firehose transcript path while it owns the thread and
falling back to the legacy path when the turn is not attachable.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Optional, cast

from nymeria.triggers.telegram_bot import NymeriaTelegramBot

from tests.test_discord_bot_autonomous_streaming import (  # shared fakes
    _FakeChannel,
    _bot_for,
)
from tests.test_telegram_bot_streaming import _FakeBot  # shared fakes


class _AttachError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"http {status_code}")
        self.response = SimpleNamespace(status_code=status_code)


class _AttachAPI:
    """Fake bot API exposing only the attach stream (plus call recording).

    Mirrors the server's replay contract: events with a ``seq`` at or below
    ``from_seq`` are not replayed (exclusive resume). ``queues`` serves one
    event list per call, modelling consecutive turns on one thread.
    """

    def __init__(
        self,
        events: Optional[list[dict]] = None,
        *,
        fail: Optional[Exception] = None,
        gate: Optional[asyncio.Event] = None,
        queues: Optional[list[list[dict]]] = None,
    ) -> None:
        self.events = events or []
        self.fail = fail
        self.gate = gate
        self._queues = [list(q) for q in queues] if queues is not None else None
        self.calls: list[int] = []

    async def reattach_turn_stream(
        self,
        thread_id: str,
        user_id: Optional[str] = None,
        *,
        turn_id: Optional[str] = None,
        from_seq: int = 0,
    ):
        self.calls.append(from_seq)
        if self.fail is not None:
            raise self.fail
        events = self.events
        if self._queues is not None:
            events = self._queues.pop(0) if self._queues else []
        for event in events:
            if event.get("_gate") and self.gate is not None:
                await self.gate.wait()
                continue
            seq = event.get("seq")
            if isinstance(seq, int) and seq <= from_seq:
                continue
            yield event


def _telegram_bot(api: Any) -> tuple[NymeriaTelegramBot, _FakeBot]:
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    bot._application = SimpleNamespace(bot=fake_bot)
    return bot, fake_bot


_THREAD = "telegram_123"


def _attach_events(text: str) -> list[dict]:
    return [
        {"type": "turn_attach", "thread_id": _THREAD, "state": "live"},
        {"type": "turn_started", "thread_id": _THREAD, "seq": 1},
        {"type": "response", "content": text, "thread_id": _THREAD, "seq": 2},
        {"type": "done", "thread_id": _THREAD, "seq": 3},
    ]


def test_telegram_attach_renders_turn_and_suppresses_firehose():
    """The attach stream is the single renderer: firehose transcript events
    and the task_completed content fallback must not double-deliver."""
    briefing = "Morning briefing: all quiet."
    api = _AttachAPI(_attach_events(briefing))
    bot, fake_bot = _telegram_bot(api)

    async def scenario() -> None:
        await bot._handle_sse_event(
            {"type": "task_started", "thread_id": _THREAD, "task_id": "todo-1"}
        )
        state = bot._autonomous_state[_THREAD]
        await state["attach_task"]
        # Firehose mirror of the same turn arrives after delivery.
        await bot._handle_sse_event(
            {
                "type": "response",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "content": briefing,
            }
        )
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "content": briefing,
            }
        )

    asyncio.run(scenario())

    assert [msg.text for msg in fake_bot.messages] == [briefing]
    assert bot._autonomous_state == {}
    assert api.calls == [0]


def test_telegram_attach_completed_before_stream_end_pops_after_delivery():
    """task_completed can beat the attach's terminal event; the state pops
    only once the attach has delivered."""
    briefing = "Held briefing."
    gate = asyncio.Event()
    events = [
        {"type": "turn_attach", "thread_id": _THREAD, "state": "live"},
        {"type": "response", "content": briefing, "thread_id": _THREAD, "seq": 1},
        {"_gate": True},
        {"type": "done", "thread_id": _THREAD, "seq": 2},
    ]

    async def scenario() -> tuple[list[str], dict]:
        api = _AttachAPI(events, gate=gate)
        bot, fake_bot = _telegram_bot(api)
        await bot._handle_sse_event(
            {"type": "task_started", "thread_id": _THREAD, "task_id": "todo-1"}
        )
        state = bot._autonomous_state[_THREAD]
        await asyncio.sleep(0.05)  # let the attach render up to the gate
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "content": briefing,
            }
        )
        assert _THREAD in bot._autonomous_state  # deferred to the attach
        gate.set()
        await state["attach_task"]
        return [m.text for m in fake_bot.messages], bot._autonomous_state

    texts, remaining = asyncio.run(scenario())
    assert texts == [briefing]
    assert remaining == {}


def test_telegram_attach_404_falls_back_to_legacy_completed_delivery():
    """A non-attachable turn (404 turn_not_found, e.g. a non-buffered
    headless workflow TODO) must deliver exactly as before via the
    task_completed content fallback, including one stashed while pending."""
    api = _AttachAPI(fail=_AttachError(404))
    bot, fake_bot = _telegram_bot(api)

    async def scenario() -> None:
        await bot._handle_sse_event(
            {"type": "task_started", "thread_id": _THREAD, "task_id": "todo-1"}
        )
        state = bot._autonomous_state[_THREAD]
        # Completed arrives while the attach is still opening: it is stashed,
        # then re-delivered by the fallback transition.
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "content": "Fallback content.",
            }
        )
        await state["attach_task"]

    asyncio.run(scenario())

    assert [msg.text for msg in fake_bot.messages] == ["Fallback content."]
    assert bot._autonomous_state == {}


def test_telegram_attach_open_failure_retries_then_falls_back():
    """A transient open failure retries once; persistent failure falls back
    so later firehose events deliver via the legacy path."""
    api = _AttachAPI(fail=_AttachError(500))
    bot, fake_bot = _telegram_bot(api)

    async def scenario() -> None:
        await bot._handle_sse_event(
            {"type": "task_started", "thread_id": _THREAD, "task_id": "todo-1"}
        )
        state = bot._autonomous_state[_THREAD]
        await state["attach_task"]
        await bot._handle_sse_event(
            {
                "type": "response",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "content": "Legacy path.",
            }
        )
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "content": "Legacy path.",
            }
        )

    asyncio.run(scenario())

    assert api.calls == [0, 0]  # one retry, then fallback
    assert [msg.text for msg in fake_bot.messages] == ["Legacy path."]
    assert bot._autonomous_state == {}


def test_telegram_new_turn_replaces_stale_attach_and_drops_old_completed():
    """A task_started for a NEW task while the previous turn's entry is
    still settling (its completed never arrived) must not be swallowed by
    the stale attach: the entry is replaced, the new turn renders, and the
    old turn's late completed is dropped by the task-id guard."""
    api = _AttachAPI(
        queues=[_attach_events("Turn one."), _attach_events("Turn two.")]
    )
    bot, fake_bot = _telegram_bot(api)

    async def scenario() -> None:
        await bot._handle_sse_event(
            {"type": "task_started", "thread_id": _THREAD, "task_id": "todo-1"}
        )
        state1 = bot._autonomous_state[_THREAD]
        await state1["attach_task"]
        # todo-1's completed never arrives; the entry is stale but present.
        assert _THREAD in bot._autonomous_state
        await bot._handle_sse_event(
            {"type": "task_started", "thread_id": _THREAD, "task_id": "todo-2"}
        )
        state2 = bot._autonomous_state[_THREAD]
        assert state2 is not state1
        await state2["attach_task"]
        # todo-1's late completed: consumed and dropped (already rendered).
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "content": "Turn one.",
            }
        )
        assert _THREAD in bot._autonomous_state
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _THREAD,
                "task_id": "todo-2",
                "content": "Turn two.",
            }
        )

    asyncio.run(scenario())

    assert [m.text for m in fake_bot.messages] == ["Turn one.", "Turn two."]
    assert bot._autonomous_state == {}


def test_telegram_attach_idle_timeout_flushes_partial(monkeypatch):
    """A holder that dies without a terminal event must not wedge the
    attach: the idle bound degrades to a partial flush and the completed
    still pops the state."""
    from nymeria.triggers import sse_consumer as sse_consumer_mod

    monkeypatch.setattr(sse_consumer_mod, "ATTACH_IDLE_TIMEOUT_SECONDS", 0.05)
    gate = asyncio.Event()  # never set: the stream hangs after one chunk

    events = [
        {"type": "turn_attach", "thread_id": _THREAD, "state": "live"},
        {"type": "response", "content": "Partial.", "thread_id": _THREAD, "seq": 1},
        {"_gate": True},
        {"type": "done", "thread_id": _THREAD, "seq": 2},
    ]

    async def scenario() -> tuple[list[str], dict, list[int]]:
        api = _AttachAPI(events, gate=gate)
        bot, fake_bot = _telegram_bot(api)
        await bot._handle_sse_event(
            {"type": "task_started", "thread_id": _THREAD, "task_id": "todo-1"}
        )
        state = bot._autonomous_state[_THREAD]
        await asyncio.wait_for(state["attach_task"], timeout=5)
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "content": "Partial.",
            }
        )
        return (
            [m.text for m in fake_bot.messages],
            bot._autonomous_state,
            api.calls,
        )

    texts, remaining, calls = asyncio.run(scenario())
    assert texts == ["Partial."]
    assert remaining == {}
    assert calls == [0, 1]  # retry resumed from the last rendered seq


_DISCORD_THREAD = "discord_123_456"


def _discord_attach_events(text: str) -> list[dict]:
    return [
        {"type": "turn_attach", "thread_id": _DISCORD_THREAD, "state": "live"},
        {"type": "turn_started", "thread_id": _DISCORD_THREAD, "seq": 1},
        {"type": "response", "content": text, "thread_id": _DISCORD_THREAD, "seq": 2},
        {"type": "done", "thread_id": _DISCORD_THREAD, "seq": 3},
    ]


def test_discord_attach_renders_turn_and_suppresses_firehose():
    channel = _FakeChannel()
    bot = _bot_for(channel)
    briefing = "Discord briefing."
    api = _AttachAPI(_discord_attach_events(briefing))
    bot.api = cast(Any, api)

    async def scenario() -> None:
        await bot._handle_sse_event(
            {
                "type": "task_started",
                "thread_id": _DISCORD_THREAD,
                "task_id": "todo-1",
            }
        )
        state = bot._autonomous_state[_DISCORD_THREAD]
        await state["attach_task"]
        await bot._handle_sse_event(
            {
                "type": "response",
                "thread_id": _DISCORD_THREAD,
                "task_id": "todo-1",
                "content": briefing,
            }
        )
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _DISCORD_THREAD,
                "task_id": "todo-1",
                "content": briefing,
            }
        )

    asyncio.run(scenario())

    assert [msg.content for msg in channel.messages] == [briefing]
    assert bot._autonomous_state == {}
    assert api.calls == [0]


def test_discord_new_turn_replaces_stale_attach():
    channel = _FakeChannel()
    bot = _bot_for(channel)
    api = _AttachAPI(
        queues=[
            _discord_attach_events("Turn one."),
            _discord_attach_events("Turn two."),
        ]
    )
    bot.api = cast(Any, api)

    async def scenario() -> None:
        await bot._handle_sse_event(
            {
                "type": "task_started",
                "thread_id": _DISCORD_THREAD,
                "task_id": "todo-1",
            }
        )
        state1 = bot._autonomous_state[_DISCORD_THREAD]
        await state1["attach_task"]
        await bot._handle_sse_event(
            {
                "type": "task_started",
                "thread_id": _DISCORD_THREAD,
                "task_id": "todo-2",
            }
        )
        state2 = bot._autonomous_state[_DISCORD_THREAD]
        assert state2 is not state1
        await state2["attach_task"]
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _DISCORD_THREAD,
                "task_id": "todo-2",
                "content": "Turn two.",
            }
        )

    asyncio.run(scenario())

    assert [m.content for m in channel.messages] == ["Turn one.", "Turn two."]
    assert bot._autonomous_state == {}


def test_discord_attach_404_falls_back_to_legacy_completed_delivery():
    channel = _FakeChannel()
    bot = _bot_for(channel)
    bot.api = cast(Any, _AttachAPI(fail=_AttachError(404)))

    async def scenario() -> None:
        await bot._handle_sse_event(
            {
                "type": "task_started",
                "thread_id": _DISCORD_THREAD,
                "task_id": "todo-1",
            }
        )
        state = bot._autonomous_state[_DISCORD_THREAD]
        await bot._handle_sse_event(
            {
                "type": "task_completed",
                "thread_id": _DISCORD_THREAD,
                "task_id": "todo-1",
                "content": "Fallback content.",
            }
        )
        await state["attach_task"]

    asyncio.run(scenario())

    assert [msg.content for msg in channel.messages] == ["Fallback content."]
    assert bot._autonomous_state == {}
