"""Reusable test fixtures for the CLI/TUI refactor.

These helpers intentionally avoid importing the in-progress CLI transport,
event, reducer, or renderer modules. They provide small contracts that later
CLI tests can consume without a real API server, database, or LLM.
"""

from __future__ import annotations

import asyncio
import copy
import io
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, cast

import pytest

EventDict = dict[str, Any]


def run(coro):
    """Drive a coroutine to completion in a fresh event loop.

    Shared one-liner for the CLI/command test suite so the byte-identical
    ``asyncio.run`` wrapper is defined once instead of per file.
    """

    return asyncio.run(coro)


@dataclass(frozen=True, slots=True)
class DelayedEvent:
    """A stream event that becomes available after ``delay_seconds``."""

    delay_seconds: float
    event: EventDict


StreamItem = EventDict | DelayedEvent


@dataclass(slots=True)
class FakeClock:
    """Deterministic clock for animation and delayed-stream tests."""

    current_time: float = 0.0
    sleep_calls: list[float] = field(default_factory=list)

    def time(self) -> float:
        return self.current_time

    def monotonic(self) -> float:
        return self.current_time

    def advance(self, seconds: float = 0.0, *, milliseconds: float = 0.0) -> float:
        delta = seconds + (milliseconds / 1000)
        if delta < 0:
            raise ValueError("FakeClock cannot move backwards")
        self.current_time += delta
        return self.current_time

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.advance(seconds)


@dataclass(frozen=True, slots=True)
class FakeTerminalCapabilities:
    """Minimal terminal capability object for renderer and selector tests."""

    stdin_isatty: bool = True
    stdout_isatty: bool = True
    stderr_isatty: bool = True
    term: str = "xterm-256color"
    ci: bool = False
    no_color: bool = False
    force_color: bool = False
    supports_color: bool = True
    color_depth: int = 256
    supports_unicode: bool = True
    supports_alt_screen: bool = True
    supports_animation: bool = True
    supports_mouse: bool = True
    width: int = 80
    height: int = 24
    renderer: str = "rich"

    @property
    def is_interactive(self) -> bool:
        return self.stdin_isatty and self.stdout_isatty and self.term != "dumb"

    @property
    def prefers_plain_renderer(self) -> bool:
        return not self.is_interactive or self.ci

    @property
    def color_enabled(self) -> bool:
        if self.force_color:
            return True
        return self.supports_color and not self.no_color

    @property
    def unicode_enabled(self) -> bool:
        return self.supports_unicode

    @property
    def alt_screen_enabled(self) -> bool:
        return self.is_interactive and self.supports_alt_screen

    @property
    def animation_enabled(self) -> bool:
        return self.is_interactive and self.supports_animation

    @property
    def mouse_enabled(self) -> bool:
        return self.is_interactive and self.supports_mouse

    def with_overrides(self, **overrides: Any) -> "FakeTerminalCapabilities":
        return replace(self, **overrides)


@dataclass(slots=True)
class CapturedRenderOutput:
    """Simple stdout/stderr capture sink for renderer tests."""

    stdout: io.StringIO = field(default_factory=io.StringIO)
    stderr: io.StringIO = field(default_factory=io.StringIO)

    @property
    def stdout_text(self) -> str:
        return self.stdout.getvalue()

    @property
    def stderr_text(self) -> str:
        return self.stderr.getvalue()

    def write_stdout(self, text: str = "", *, end: str = "") -> None:
        self.stdout.write(f"{text}{end}")

    def write_stderr(self, text: str = "", *, end: str = "") -> None:
        self.stderr.write(f"{text}{end}")

    def clear(self) -> None:
        self.stdout.seek(0)
        self.stdout.truncate(0)
        self.stderr.seek(0)
        self.stderr.truncate(0)


@dataclass(frozen=True, slots=True)
class ChatRequest:
    message: str
    thread_id: str
    user_id: str
    attachments: tuple[Any, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StopRequest:
    thread_id: str
    user_id: str | None = None


def _clone_event(event: EventDict) -> EventDict:
    return copy.deepcopy(event)


def _clone_item(item: StreamItem) -> StreamItem:
    if isinstance(item, DelayedEvent):
        return DelayedEvent(item.delay_seconds, _clone_event(item.event))
    return _clone_event(item)


def strip_delays(items: Sequence[StreamItem]) -> list[EventDict]:
    """Return only event dictionaries from a stream script."""

    events: list[EventDict] = []
    for item in items:
        events.append(_clone_event(item.event if isinstance(item, DelayedEvent) else item))
    return events


async def async_event_stream(
    items: Sequence[StreamItem],
    *,
    clock: FakeClock | None = None,
    real_sleep: bool = False,
) -> AsyncIterator[EventDict]:
    """Yield cloned events and honor ``DelayedEvent`` timing deterministically."""

    for item in items:
        event: EventDict
        if isinstance(item, DelayedEvent):
            if item.delay_seconds < 0:
                raise ValueError("DelayedEvent delay_seconds must be non-negative")
            if clock is not None:
                await clock.sleep(item.delay_seconds)
            elif real_sleep and item.delay_seconds:
                await asyncio.sleep(item.delay_seconds)
            event = item.event
        else:
            event = item
        yield _clone_event(event)


def sync_event_stream(
    items: Sequence[StreamItem],
    *,
    clock: FakeClock | None = None,
) -> Iterator[EventDict]:
    """Synchronous stream adapter for legacy renderer tests."""

    for item in items:
        event: EventDict
        if isinstance(item, DelayedEvent):
            if item.delay_seconds < 0:
                raise ValueError("DelayedEvent delay_seconds must be non-negative")
            if clock is not None:
                clock.advance(item.delay_seconds)
            event = item.event
        else:
            event = item
        yield _clone_event(event)


def simple_response_events(
    chunks: Sequence[str] = ("Hello", " from Nymeria."),
    *,
    thread_id: str = "thread-1",
) -> list[EventDict]:
    return [
        *[
            {"type": "response", "content": chunk, "thread_id": thread_id}
            for chunk in chunks
        ],
        {
            "type": "done",
            "thread_id": thread_id,
            "tool_call_count": 0,
            "model": "test-model",
            "context_stats": {"used_tokens": 42, "max_tokens": 1000},
        },
    ]


def thinking_silence_events(
    *,
    delay_seconds: float = 1.2,
    thread_id: str = "thread-1",
) -> list[StreamItem]:
    return [
        {
            "type": "thinking",
            "content": "checking context",
            "thread_id": thread_id,
        },
        DelayedEvent(
            delay_seconds,
            {"type": "response", "content": "Ready.", "thread_id": thread_id},
        ),
        {"type": "done", "thread_id": thread_id, "tool_call_count": 0},
    ]


def tool_call_result_events(*, thread_id: str = "thread-1") -> list[EventDict]:
    return [
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "search_memory",
            "args": {"query": "project status"},
            "thread_id": thread_id,
        },
        {
            "type": "tool_result",
            "id": "call-1",
            "name": "search_memory",
            "result": "Found 2 matching notes.",
            "thread_id": thread_id,
        },
        {"type": "response", "content": "I found the notes.", "thread_id": thread_id},
        {"type": "done", "thread_id": thread_id, "tool_call_count": 1},
    ]


def queued_response_events(*, thread_id: str = "thread-1") -> list[EventDict]:
    return [
        {"type": "queued", "holder": "worker-1", "thread_id": thread_id},
        {"type": "response", "content": "Thanks for waiting.", "thread_id": thread_id},
        {"type": "done", "thread_id": thread_id, "tool_call_count": 0},
    ]


def error_events(*, thread_id: str = "thread-1") -> list[EventDict]:
    return [
        {
            "type": "error",
            "content": "Backend unavailable",
            "code": "connection_failed",
            "thread_id": thread_id,
        }
    ]


def cancelled_turn_events(*, thread_id: str = "thread-1") -> list[EventDict]:
    return [
        {
            "type": "error",
            "content": "Cancelled.",
            "code": "cancelled",
            "thread_id": thread_id,
        },
        {"type": "done", "thread_id": thread_id, "status": "cancelled"},
    ]


def compacted_turn_events(*, thread_id: str = "thread-1") -> list[EventDict]:
    return [
        {"type": "compacting", "message": "Compacting context...", "thread_id": thread_id},
        {
            "type": "compacted",
            "summary": "Earlier context was summarized.",
            "messages_removed": 12,
            "thread_id": thread_id,
        },
        {
            "type": "response",
            "content": "Continuing with the compacted context.",
            "thread_id": thread_id,
        },
        {"type": "done", "thread_id": thread_id, "tool_call_count": 0},
    ]


EVENT_SEQUENCE_BUILDERS = {
    "simple_response": simple_response_events,
    "thinking_silence": thinking_silence_events,
    "tool_call_result": tool_call_result_events,
    "queued_response": queued_response_events,
    "error": error_events,
    "cancelled_turn": cancelled_turn_events,
    "compacted_turn": compacted_turn_events,
}


def available_event_sequences() -> tuple[str, ...]:
    return tuple(EVENT_SEQUENCE_BUILDERS)


def event_sequence(name: str, **kwargs: Any) -> list[StreamItem]:
    try:
        builder = EVENT_SEQUENCE_BUILDERS[name]
    except KeyError as exc:
        known = ", ".join(available_event_sequences())
        raise ValueError(f"Unknown CLI event sequence {name!r}; known: {known}") from exc
    return [_clone_item(item) for item in builder(**kwargs)]


class FakeAgentClient:
    """Deterministic stand-in for the planned CLI ``AgentClient`` protocol."""

    def __init__(
        self,
        streams: Mapping[str, Sequence[StreamItem]] | Sequence[StreamItem] | None = None,
        *,
        default_stream: Sequence[StreamItem] | None = None,
        history: Mapping[str, Any] | None = None,
        threads: Sequence[Mapping[str, Any]] | None = None,
        context_stats: Mapping[str, Any] | None = None,
        autonomous_events: Sequence[StreamItem] | None = None,
        clock: FakeClock | None = None,
        real_sleep: bool = False,
        connection_label: str = "local agent",
    ) -> None:
        self.chat_requests: list[ChatRequest] = []
        self.stop_requests: list[StopRequest] = []
        self.clock = clock
        self.real_sleep = real_sleep
        self.connection_label = connection_label
        # Mirror the production gate: real transports advertise autonomous
        # support via this flag. Deriving it from the label keeps every
        # existing test's ``can_start()`` result identical to the old
        # connection-label sniff (API-backed → on, local placeholder → off).
        self.supports_autonomous_stream = connection_label.startswith("api ")
        self.history = copy.deepcopy(dict(history or {}))
        self.threads = copy.deepcopy(list(threads or []))
        self.context_stats = copy.deepcopy(dict(context_stats or {}))
        self.autonomous_requests: list[dict[str, Any]] = []
        self._autonomous_events = [
            _clone_item(item)
            for item in (autonomous_events or [])
        ]

        if isinstance(streams, Mapping):
            self._streams_by_message = {
                message: [_clone_item(item) for item in stream]
                for message, stream in streams.items()
            }
            selected_default = default_stream
        else:
            self._streams_by_message = {}
            selected_default = streams if streams is not None else default_stream

        self._default_stream = [
            _clone_item(item)
            for item in (selected_default or simple_response_events())
        ]

    @property
    def stop_count(self) -> int:
        return len(self.stop_requests)

    def queue_stream(self, message: str, events: Sequence[StreamItem]) -> None:
        self._streams_by_message[message] = [_clone_item(item) for item in events]

    async def stream_chat(
        self,
        message: str,
        thread_id: str,
        user_id: str = "default",
        attachments: Sequence[Mapping[str, Any]] | None = None,
        **options: Any,
    ) -> AsyncIterator[EventDict]:
        self.chat_requests.append(
            ChatRequest(
                message=message,
                thread_id=thread_id,
                user_id=user_id,
                attachments=tuple(copy.deepcopy(list(attachments or []))),
                options=copy.deepcopy(options),
            )
        )
        script = self._streams_by_message.get(message, self._default_stream)
        async for event in async_event_stream(
            script,
            clock=self.clock,
            real_sleep=self.real_sleep,
        ):
            yield event

    async def stream_autonomous(
        self,
        user_id: str = "default",
        *,
        client_id: str | None = None,
    ) -> AsyncIterator[EventDict]:
        self.autonomous_requests.append(
            {"user_id": user_id, "client_id": client_id}
        )
        async for event in async_event_stream(
            self._autonomous_events,
            clock=self.clock,
            real_sleep=self.real_sleep,
        ):
            yield event

    async def stop(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.stop_requests.append(StopRequest(thread_id=thread_id, user_id=user_id))
        return {"ok": True, "thread_id": thread_id}

    async def get_history(
        self,
        thread_id: str,
        user_id: str | None = None,
        **_options: Any,
    ) -> Any:
        key = f"{user_id}:{thread_id}" if user_id else thread_id
        return copy.deepcopy(
            self.history.get(key, self.history.get(thread_id, {"messages": []}))
        )

    async def list_threads(self, user_id: str = "default") -> list[dict[str, Any]]:
        return cast(
            "list[dict[str, Any]]",
            copy.deepcopy(
                self.threads
                or [
                    {
                        "id": "thread-1",
                        "title": "Fixture thread",
                        "user_id": user_id,
                    }
                ]
            ),
        )

    async def get_context_stats(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        key = f"{user_id}:{thread_id}" if user_id else thread_id
        return cast(
            "dict[str, Any]",
            copy.deepcopy(
                self.context_stats.get(
                    key,
                    self.context_stats.get(
                        thread_id,
                        {"used_tokens": 42, "max_tokens": 1000},
                    ),
                )
            ),
        )


@pytest.fixture
def cli_fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cli_terminal_capabilities() -> FakeTerminalCapabilities:
    return FakeTerminalCapabilities()


@pytest.fixture
def cli_output_capture() -> CapturedRenderOutput:
    return CapturedRenderOutput()


@pytest.fixture
def cli_agent_client() -> FakeAgentClient:
    return FakeAgentClient()
