"""Tests for honest autonomous delivery + the delivery report (backlog #247).

Pre-#247 both native bots swallowed send failures per-chunk and then logged
"Streamed autonomous result ..." unconditionally, so a totally undelivered
scheduled turn looked like a success everywhere. These tests pin the honest
completion lines and the fire-and-forget ``report_todo_delivery`` call that
feeds the backend's delivery accounting.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Optional, cast

from telegram.error import BadRequest

from nymeria.triggers.telegram_bot import (
    NymeriaTelegramBot,
    describe_telegram_send_error,
)

from tests.test_bot_turn_attach import _AttachAPI, _attach_events, _THREAD
from tests.test_discord_bot_autonomous_streaming import (  # shared fakes
    _FakeChannel,
    _bot_for,
)
from tests.test_telegram_bot_streaming import _FakeBot  # shared fakes

TELEGRAM_LOGGER = "nymeria.triggers.telegram_bot"
DISCORD_LOGGER = "nymeria.triggers.discord_bot"


class _ReportRecorder:
    """Minimal bot API exposing only the delivery-report call."""

    def __init__(self) -> None:
        self.reports: list[dict] = []
        self.report_seen = asyncio.Event()

    async def report_todo_delivery(
        self,
        todo_id: str,
        *,
        outcome: str,
        platform: str,
        target: str = "",
        error: Optional[str] = None,
        thread_id: str = "",
    ) -> dict:
        self.reports.append(
            {
                "todo_id": todo_id,
                "outcome": outcome,
                "platform": platform,
                "target": target,
                "error": error,
                "thread_id": thread_id,
            }
        )
        self.report_seen.set()
        return {}


class _ReportingAttachAPI(_AttachAPI, _ReportRecorder):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _AttachAPI.__init__(self, *args, **kwargs)
        _ReportRecorder.__init__(self)


class _UnreachableChatBot(_FakeBot):
    """Every send fails the way an un-started Telegram chat does."""

    async def send_message(self, chat_id: int, text: str, **kwargs):
        raise BadRequest("Chat not found")


class _FlakyChatBot(_FakeBot):
    """First bubble fails, later bubbles land (a partial delivery).

    ``_send_html`` retries a BadRequest once as plain text, so one failed
    BUBBLE is two failed ``send_message`` calls.
    """

    def __init__(self) -> None:
        super().__init__()
        self._failures_left = 2

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if self._failures_left > 0:
            self._failures_left -= 1
            raise BadRequest("Chat not found")
        return await super().send_message(chat_id, text, **kwargs)


def _telegram_bot(api: Any, fake_bot: _FakeBot) -> NymeriaTelegramBot:
    bot = NymeriaTelegramBot(api=api, bot_token="test-token")
    bot._application = SimpleNamespace(bot=fake_bot)
    return bot


def _todo_events(*, todo_id: Optional[str] = "todo-1", error: bool = False) -> list[dict]:
    started: dict = {"type": "task_started", "thread_id": _THREAD, "task_id": "todo-1"}
    completed: dict = {
        "type": "task_completed",
        "thread_id": _THREAD,
        "task_id": "todo-1",
        "content": "Daily check-in text.",
    }
    if todo_id:
        started["todo_id"] = todo_id
        completed["todo_id"] = todo_id
    if error:
        completed["error"] = True
        completed["content"] = "boom"
    return [
        started,
        {
            "type": "response",
            "thread_id": _THREAD,
            "task_id": "todo-1",
            "content": "Daily check-in text.",
        },
        completed,
    ]


async def _deliver(bot: Any, events: list[dict]) -> None:
    for event in events:
        await bot._handle_sse_event(event)


def _log_messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def test_telegram_total_failure_logs_failed_and_reports(caplog):
    """#247 behaviors 1 + 12: no false success line, a FAILED line with the
    actionable "never started" copy, and a failed report with the todo id."""
    caplog.set_level(logging.INFO, logger=TELEGRAM_LOGGER)
    api = _ReportRecorder()
    bot = _telegram_bot(api, _UnreachableChatBot())

    async def scenario() -> None:
        await _deliver(bot, _todo_events())
        await asyncio.wait_for(api.report_seen.wait(), timeout=2)

    asyncio.run(scenario())

    messages = _log_messages(caplog)
    assert not any(m.startswith("Streamed autonomous result") for m in messages)
    assert any("FAILED" in m and "Telegram chat 123" in m for m in messages)
    assert api.reports == [
        {
            "todo_id": "todo-1",
            "outcome": "failed",
            "platform": "telegram",
            "target": "chat 123",
            "error": describe_telegram_send_error(BadRequest("Chat not found")),
            "thread_id": _THREAD,
        }
    ]
    assert "never started this bot" in api.reports[0]["error"]


def test_telegram_delivered_reports_delivered_and_keeps_success_line(caplog):
    caplog.set_level(logging.INFO, logger=TELEGRAM_LOGGER)
    api = _ReportRecorder()
    fake_bot = _FakeBot()
    bot = _telegram_bot(api, fake_bot)

    async def scenario() -> None:
        await _deliver(bot, _todo_events())
        await asyncio.wait_for(api.report_seen.wait(), timeout=2)

    asyncio.run(scenario())

    assert [m.text for m in fake_bot.messages] == ["Daily check-in text."]
    assert any(
        m.startswith("Streamed autonomous result to Telegram chat 123")
        for m in _log_messages(caplog)
    )
    assert api.reports[0]["outcome"] == "delivered"
    assert api.reports[0]["error"] is None


def test_telegram_partial_failure_reports_partial(caplog):
    """#247 behavior 9: some bubbles landed, some did not."""
    caplog.set_level(logging.INFO, logger=TELEGRAM_LOGGER)
    api = _ReportRecorder()
    bot = _telegram_bot(api, _FlakyChatBot())
    # Two flush cycles: the tool_call boundary flushes the first (failing)
    # bubble, completion flushes the second (landing) one.
    events = _todo_events()
    events.insert(
        2,
        {
            "type": "tool_call",
            "thread_id": _THREAD,
            "task_id": "todo-1",
            "id": "call-1",
            "name": "lookup",
            "args": {},
        },
    )
    events.insert(
        3,
        {
            "type": "response",
            "thread_id": _THREAD,
            "task_id": "todo-1",
            "content": "After the tool.",
        },
    )

    async def scenario() -> None:
        await _deliver(bot, events)
        await asyncio.wait_for(api.report_seen.wait(), timeout=2)

    asyncio.run(scenario())

    assert api.reports[0]["outcome"] == "partial"
    assert "Chat not found" in api.reports[0]["error"]
    assert any(
        "Partially delivered" in m for m in _log_messages(caplog)
    )


def test_telegram_non_todo_turn_logs_honestly_but_files_no_report(caplog):
    """#247 behavior 11: trigger-driven turns carry no todo_id."""
    caplog.set_level(logging.INFO, logger=TELEGRAM_LOGGER)
    api = _ReportRecorder()
    bot = _telegram_bot(api, _UnreachableChatBot())

    async def scenario() -> None:
        await _deliver(bot, _todo_events(todo_id=None))
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert any("FAILED" in m for m in _log_messages(caplog))
    assert api.reports == []


def test_telegram_errored_turn_is_not_reported_as_delivery_failure(caplog):
    """An errored turn already joined the #154 execution accounting; its
    send outcome is logged but must not also feed the delivery streak."""
    caplog.set_level(logging.INFO, logger=TELEGRAM_LOGGER)
    api = _ReportRecorder()
    bot = _telegram_bot(api, _UnreachableChatBot())

    async def scenario() -> None:
        await _deliver(bot, _todo_events(error=True))
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    messages = _log_messages(caplog)
    assert not any(m.startswith("Streamed autonomous result") for m in messages)
    assert api.reports == []


def test_telegram_attach_path_total_failure_reports_failed(caplog):
    """The attach (#91) delivery path reports through the same accounting."""
    caplog.set_level(logging.INFO, logger=TELEGRAM_LOGGER)
    api = _ReportingAttachAPI(_attach_events("Attached briefing."))
    bot = _telegram_bot(api, _UnreachableChatBot())

    async def scenario() -> None:
        await bot._handle_sse_event(
            {
                "type": "task_started",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "todo_id": "todo-1",
            }
        )
        await bot._autonomous_state[_THREAD]["attach_task"]
        await asyncio.wait_for(api.report_seen.wait(), timeout=2)

    asyncio.run(scenario())

    messages = _log_messages(caplog)
    assert any("FAILED" in m and "(turn attach)" in m for m in messages)
    assert not any(m.startswith("Streamed autonomous result") for m in messages)
    assert api.reports[0]["outcome"] == "failed"
    assert api.reports[0]["todo_id"] == "todo-1"


def test_telegram_attach_errored_turn_is_not_reported(caplog):
    """S4 guard: on the attach path there is no completed event to read
    `error` from, so the handler's on_error flag must suppress the report
    (the occurrence already fed the #154 execution accounting)."""
    caplog.set_level(logging.INFO, logger=TELEGRAM_LOGGER)
    events = [
        {"type": "turn_attach", "thread_id": _THREAD, "state": "live"},
        {"type": "response", "content": "partial", "thread_id": _THREAD, "seq": 1},
        {"type": "error", "content": "boom", "thread_id": _THREAD, "seq": 2},
        {"type": "done", "thread_id": _THREAD, "seq": 3},
    ]
    api = _ReportingAttachAPI(events)
    bot = _telegram_bot(api, _UnreachableChatBot())

    async def scenario() -> None:
        await bot._handle_sse_event(
            {
                "type": "task_started",
                "thread_id": _THREAD,
                "task_id": "todo-1",
                "todo_id": "todo-1",
            }
        )
        await bot._autonomous_state[_THREAD]["attach_task"]
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    messages = _log_messages(caplog)
    assert any("FAILED" in m for m in messages)  # still honest in the log
    assert api.reports == []  # but never double-accounted


class _MidTurnDropAPI(_ReportRecorder):
    """Interactive chat API: drops mid-turn, supports re-attach, records
    any sync ``chat`` re-POST (which must never happen post-turn)."""

    def __init__(self, *, self_invoke_shape: bool = False) -> None:
        super().__init__()
        self.chat_calls = 0
        self._self_invoke_shape = self_invoke_shape

    async def chat_stream(self, message, thread_id, user_id, **kwargs):
        if not self._self_invoke_shape:
            yield {"type": "turn_started", "turn_id": "t1", "seq": 1}
        yield {"type": "response", "content": "Hello ", "seq": 2}
        import httpx

        raise httpx.ReadError("dropped")

    async def get_thread_status(self, thread_id, user_id=None):
        return {"processing": True, "turn": {"turn_id": "t1"}}

    async def reattach_turn_stream(
        self, thread_id, user_id=None, *, turn_id=None, from_seq=0
    ):
        for event in [
            {"type": "response", "content": "world", "seq": 3},
            {"type": "done", "seq": 4},
        ]:
            if isinstance(event.get("seq"), int) and event["seq"] <= from_seq:
                continue
            yield event

    async def chat(self, *args, **kwargs):
        self.chat_calls += 1
        return {"response": "DUPLICATE TURN", "tool_call_count": 0}


def test_telegram_mid_turn_drop_recovers_without_repost():
    """Bot-level wiring for #88: the tail arrives exactly once and the
    legacy sync fallback (a duplicate turn) is never invoked."""
    api = _MidTurnDropAPI()
    fake_bot = _FakeBot()
    bot = _telegram_bot(api, fake_bot)
    context = SimpleNamespace(bot=fake_bot)

    asyncio.run(
        bot._stream_to_chat(
            chat_id=123,
            message="prompt",
            thread_id=_THREAD,
            user_id="user-1",
            context=context,
        )
    )

    assert api.chat_calls == 0
    sent = "".join(m.text for m in fake_bot.messages)
    assert "Hello world" in sent
    assert "DUPLICATE TURN" not in sent


def test_telegram_self_invoke_drop_never_reposts():
    """S1 guard: self-invoke (reaction) turns carry no wire turn identity,
    so a mid-turn drop raises to the caller, which must NOT re-POST (the
    duplicate would run as a user turn)."""
    api = _MidTurnDropAPI(self_invoke_shape=True)
    fake_bot = _FakeBot()
    bot = _telegram_bot(api, fake_bot)
    context = SimpleNamespace(bot=fake_bot)

    asyncio.run(
        bot._stream_to_chat(
            chat_id=123,
            message="prompt",
            thread_id=_THREAD,
            user_id="user-1",
            context=context,
            is_self_invoke=True,
        )
    )

    assert api.chat_calls == 0


class _UnreachableChannel(_FakeChannel):
    """Every send fails the way a permission-lost Discord channel does."""

    async def send(self, content: Optional[str] = None, **kwargs):
        import discord

        raise discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Access"
        )


class _FlakyChannel(_FakeChannel):
    """First bubble fails, later bubbles land (a partial delivery).

    Discord's autonomous ``flush_text`` retries a failed primary send via
    ``_send_text``, so one failed BUBBLE is two failed ``send`` calls.
    """

    def __init__(self, channel_id: int = 456) -> None:
        super().__init__(channel_id)
        self._failures_left = 2

    async def send(self, content: Optional[str] = None, **kwargs):
        if self._failures_left > 0:
            self._failures_left -= 1
            import discord

            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "Missing Access"
            )
        return await super().send(content, **kwargs)


def test_discord_total_failure_logs_failed_and_reports(caplog):
    """#247 behavior 10: Discord mirrors the honesty + report."""
    caplog.set_level(logging.INFO, logger=DISCORD_LOGGER)
    channel = _UnreachableChannel()
    bot = _bot_for(channel)
    api = _ReportRecorder()
    bot.api = cast(Any, api)

    events = [
        {
            "type": "task_started",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "todo_id": "todo-1",
        },
        {
            "type": "response",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "content": "Daily check-in text.",
        },
        {
            "type": "task_completed",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "todo_id": "todo-1",
            "content": "Daily check-in text.",
        },
    ]

    async def scenario() -> None:
        await _deliver(bot, events)
        await asyncio.wait_for(api.report_seen.wait(), timeout=2)

    asyncio.run(scenario())

    messages = _log_messages(caplog)
    assert not any(m.startswith("Streamed autonomous result") for m in messages)
    assert any("FAILED" in m and "Discord channel 456" in m for m in messages)
    assert api.reports[0]["outcome"] == "failed"
    assert api.reports[0]["platform"] == "discord"
    assert api.reports[0]["target"] == "channel 456"
    assert "Forbidden" in api.reports[0]["error"]


def test_discord_delivered_reports_delivered(caplog):
    caplog.set_level(logging.INFO, logger=DISCORD_LOGGER)
    channel = _FakeChannel()
    bot = _bot_for(channel)
    api = _ReportRecorder()
    bot.api = cast(Any, api)

    events = [
        {
            "type": "task_started",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "todo_id": "todo-1",
        },
        {
            "type": "response",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "content": "Daily check-in text.",
        },
        {
            "type": "task_completed",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "todo_id": "todo-1",
            "content": "Daily check-in text.",
        },
    ]

    async def scenario() -> None:
        await _deliver(bot, events)
        await asyncio.wait_for(api.report_seen.wait(), timeout=2)

    asyncio.run(scenario())

    assert [m.content for m in channel.messages] == ["Daily check-in text."]
    assert any(
        m.startswith("Streamed autonomous result to Discord channel 456")
        for m in _log_messages(caplog)
    )
    assert api.reports[0]["outcome"] == "delivered"


def test_discord_partial_failure_reports_partial(caplog):
    """#247 behavior 10/9: some bubbles landed, some did not (Discord)."""
    caplog.set_level(logging.INFO, logger=DISCORD_LOGGER)
    channel = _FlakyChannel()
    bot = _bot_for(channel)
    api = _ReportRecorder()
    bot.api = cast(Any, api)

    events = [
        {
            "type": "task_started",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "todo_id": "todo-1",
        },
        {
            "type": "response",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "content": "Bubble one.",
        },
        # A status event forces a flush of bubble one (which fails), then
        # the completion flush of bubble two lands.
        {
            "type": "compacting",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "message": "Compacting...",
        },
        {
            "type": "response",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "content": "Bubble two.",
        },
        {
            "type": "task_completed",
            "thread_id": "discord_123_456",
            "task_id": "todo-1",
            "todo_id": "todo-1",
            "content": "Bubble one.Bubble two.",
        },
    ]

    async def scenario() -> None:
        await _deliver(bot, events)
        await asyncio.wait_for(api.report_seen.wait(), timeout=2)

    asyncio.run(scenario())

    assert api.reports[0]["outcome"] == "partial"
    assert "Forbidden" in api.reports[0]["error"]
    assert any("Partially delivered" in m for m in _log_messages(caplog))


def test_discord_attach_path_total_failure_reports_failed(caplog):
    """#247 behavior 10: the attach path (finalize-override shape) reports
    through the same accounting on Discord."""
    caplog.set_level(logging.INFO, logger=DISCORD_LOGGER)
    channel = _UnreachableChannel()
    bot = _bot_for(channel)
    api = _ReportingAttachAPI(
        [
            {"type": "turn_attach", "thread_id": "discord_123_456", "state": "live"},
            {"type": "turn_started", "thread_id": "discord_123_456", "seq": 1},
            {
                "type": "response",
                "content": "Attached briefing.",
                "thread_id": "discord_123_456",
                "seq": 2,
            },
            {"type": "done", "thread_id": "discord_123_456", "seq": 3},
        ]
    )
    bot.api = cast(Any, api)

    async def scenario() -> None:
        await bot._handle_sse_event(
            {
                "type": "task_started",
                "thread_id": "discord_123_456",
                "task_id": "todo-1",
                "todo_id": "todo-1",
            }
        )
        await bot._autonomous_state["discord_123_456"]["attach_task"]
        await asyncio.wait_for(api.report_seen.wait(), timeout=2)

    asyncio.run(scenario())

    messages = _log_messages(caplog)
    assert any("FAILED" in m and "(turn attach)" in m for m in messages)
    assert not any(m.startswith("Streamed autonomous result") for m in messages)
    assert api.reports[0]["outcome"] == "failed"
    assert api.reports[0]["platform"] == "discord"
