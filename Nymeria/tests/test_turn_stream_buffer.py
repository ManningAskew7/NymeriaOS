"""Turn stream buffer + chat-route holder tee (re-attachable turns, slice 1).

Covers the buffer/registry unit behavior (seq stamping, bounds, retention,
replay-then-live streaming) and the previously untested chat-route disconnect
branch: a disconnected holder turn keeps teeing events (including the
suppressed ``done``) into the buffer, auto-titles its thread, and abnormal
generator ends mark the buffer aborted. Queued-prompt (non-holder) streams
never create a buffer.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nymeria.api.routers.chat import create_chat_router
from nymeria.api.schemas.chat import ChatRequest
from nymeria.core.turn_stream_buffer import (
    MAX_BYTES_PER_TURN,
    MAX_EVENTS_PER_TURN,
    STATE_ABORTED,
    STATE_DONE,
    STATE_ERROR,
    STATE_LIVE,
    TurnReplayGapError,
    TurnStreamBuffer,
    TurnStreamRegistry,
    get_turn_stream_registry,
    reset_turn_stream_registry,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_turn_stream_registry()
    yield
    reset_turn_stream_registry()


# ---------------------------------------------------------------------------
# Buffer unit behavior
# ---------------------------------------------------------------------------


def test_append_stamps_monotonic_seq_and_returns_payload():
    buf = TurnStreamBuffer("t1", "alice")
    seq1, payload1 = buf.append({"type": "response", "content": "a"})
    seq2, payload2 = buf.append({"type": "response", "content": "b"})
    assert (seq1, seq2) == (1, 2)
    assert json.loads(payload1)["seq"] == 1
    assert json.loads(payload2)["seq"] == 2
    assert buf.last_seq == 2
    assert buf.first_available_seq == 1
    assert not buf.has_replay_gap(0)


def test_event_count_overflow_truncates_from_front():
    buf = TurnStreamBuffer("t1", "alice")
    for i in range(MAX_EVENTS_PER_TURN + 5):
        buf.append({"type": "response", "content": str(i)})
    assert buf.truncated
    assert buf.first_available_seq == 6
    assert buf.has_replay_gap(0)
    assert not buf.has_replay_gap(5)


def test_byte_overflow_truncates_from_front():
    buf = TurnStreamBuffer("t1", "alice")
    big = "x" * (MAX_BYTES_PER_TURN // 3)
    for _ in range(4):
        buf.append({"type": "response", "content": big})
    assert buf.truncated
    assert buf.first_available_seq is not None
    assert buf.first_available_seq > 1


def test_overflow_logs_a_warning_once_per_turn(caplog):
    """The truncation latch emits exactly one warning (backlog #90 slice 3).

    ``truncated`` is in-memory only and clients that hit it silently fall
    back to history, so this log line is the only operator-visible evidence
    that a turn outgrew the buffer. One line per turn, not per evicted event.
    """
    buf = TurnStreamBuffer("t-overflow", "alice")
    with caplog.at_level("WARNING", logger="nymeria.core.turn_stream_buffer"):
        for i in range(MAX_EVENTS_PER_TURN + 50):
            buf.append({"type": "response", "content": str(i)})
    overflow_records = [
        r for r in caplog.records if "overflow" in r.getMessage()
    ]
    assert len(overflow_records) == 1
    message = overflow_records[0].getMessage()
    assert "t-overflow" in message
    assert buf.turn_id in message


def test_finish_is_terminal_and_idempotent():
    buf = TurnStreamBuffer("t1", "alice")
    assert buf.state == STATE_LIVE
    buf.finish(STATE_DONE)
    assert buf.state == STATE_DONE
    assert buf.finished_at is not None
    buf.finish(STATE_ABORTED)
    assert buf.state == STATE_DONE


def test_begin_turn_stores_user_message_id_in_snapshot():
    # The turn's initiating HumanMessage id (live-attach viewers anchor their
    # hydrated history to it). Message-less turns (/resume) carry None.
    registry = TurnStreamRegistry()
    anchored = registry.begin_turn("t1", "alice", user_message_id="msg-abc")
    assert anchored.user_message_id == "msg-abc"
    assert anchored.snapshot()["user_message_id"] == "msg-abc"
    bare = registry.begin_turn("t2", "alice")
    assert bare.snapshot()["user_message_id"] is None


def test_begin_turn_stores_holder_metadata_in_snapshot():
    """Holder kind/label/anchor-internal flag (backlog #90) ride the snapshot,
    so the status ``turn`` block and ``turn_attach`` preamble expose them."""
    registry = TurnStreamRegistry()
    auto = registry.begin_turn(
        "t1",
        "alice",
        user_message_id="msg-1",
        holder_kind="autonomous",
        source_label="daily report",
        user_message_internal=True,
    )
    snap = auto.snapshot()
    assert snap["holder_kind"] == "autonomous"
    assert snap["source_label"] == "daily report"
    assert snap["user_message_internal"] is True

    default = registry.begin_turn("t2", "alice")
    snap = default.snapshot()
    assert snap["holder_kind"] == "user"
    assert snap["source_label"] is None
    assert snap["user_message_internal"] is False


def test_off_loop_append_wakes_reader_on_registered_loop():
    """Autonomous turns write from sync worker threads; with the reader loop
    registered, their appends must wake an awaiting reader promptly (via
    call_soon_threadsafe) instead of relying on the 15s poll ceiling."""
    import threading
    import time

    from nymeria.core.turn_stream_buffer import set_reader_loop

    buf = TurnStreamBuffer("t-offloop", "alice")

    async def read_two():
        set_reader_loop(asyncio.get_running_loop())
        started = time.monotonic()
        got = []
        async for payload in buf.stream_payloads():
            got.append(json.loads(payload)["type"])
        return got, time.monotonic() - started

    def writer():
        time.sleep(0.05)
        buf.append({"type": "response", "content": "from worker"})
        buf.append({"type": "done"})
        buf.finish(STATE_DONE)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        got, elapsed = asyncio.run(read_two())
    finally:
        thread.join()
        set_reader_loop(None)

    assert got == ["response", "done"]
    # Well under the 15s reader poll ceiling: the wakeups were real.
    assert elapsed < 5.0


def test_registry_begin_turn_replaces_and_aborts_live_predecessor():
    registry = TurnStreamRegistry()
    first = registry.begin_turn("t1", "alice")
    second = registry.begin_turn("t1", "alice")
    assert first.state == STATE_ABORTED
    assert registry.get("t1") is second
    assert first.turn_id != second.turn_id


def test_registry_sweep_drops_only_expired_finished_buffers():
    registry = TurnStreamRegistry()
    live = registry.begin_turn("t-live", "alice")
    fresh = registry.begin_turn("t-fresh", "alice")
    fresh.finish(STATE_DONE)
    stale = registry.begin_turn("t-stale", "alice")
    stale.finish(STATE_DONE)
    stale.finished_at = stale.finished_at - 10_000  # type: ignore[operator]

    assert registry.sweep_expired() == 1
    assert registry.get("t-live") is live
    assert registry.get("t-fresh") is fresh
    assert registry.get("t-stale") is None


def test_registry_get_lazily_evicts_expired_buffer():
    registry = TurnStreamRegistry()
    buf = registry.begin_turn("t1", "alice")
    buf.finish(STATE_DONE)
    buf.finished_at = buf.finished_at - 10_000  # type: ignore[operator]
    assert registry.get("t1") is None


def test_registry_drop_thread():
    registry = TurnStreamRegistry()
    registry.begin_turn("t1", "alice")
    registry.drop_thread("t1")
    assert registry.get("t1") is None


@pytest.mark.asyncio
async def test_stream_payloads_replays_then_tails_live_until_finish():
    buf = TurnStreamBuffer("t1", "alice")
    buf.append({"type": "response", "content": "a"})
    buf.append({"type": "response", "content": "b"})

    received: list[dict[str, Any]] = []

    async def _reader() -> None:
        async for payload in buf.stream_payloads(from_seq=0):
            received.append(json.loads(payload))

    reader = asyncio.create_task(_reader())
    await asyncio.sleep(0.05)
    assert [e["content"] for e in received] == ["a", "b"]

    buf.append({"type": "response", "content": "c"})
    await asyncio.sleep(0.05)
    assert [e["content"] for e in received] == ["a", "b", "c"]

    buf.append({"type": "done"})
    buf.finish(STATE_DONE)
    await asyncio.wait_for(reader, timeout=2)
    assert [e["type"] for e in received] == ["response", "response", "response", "done"]
    assert [e["seq"] for e in received] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_stream_payloads_from_seq_skips_replayed_prefix():
    buf = TurnStreamBuffer("t1", "alice")
    for content in ("a", "b", "c"):
        buf.append({"type": "response", "content": content})
    buf.finish(STATE_DONE)

    received = [
        json.loads(p)["content"] async for p in buf.stream_payloads(from_seq=2)
    ]
    assert received == ["c"]


@pytest.mark.asyncio
async def test_stream_payloads_raises_when_eviction_outruns_reader(monkeypatch):
    """A mid-tail overflow gap must fail loudly, never silently skip events."""
    monkeypatch.setattr("nymeria.core.turn_stream_buffer.MAX_EVENTS_PER_TURN", 3)
    buf = TurnStreamBuffer("t1", "alice")
    buf.append({"type": "response", "content": "a"})

    stream = buf.stream_payloads(from_seq=0)
    first = json.loads(await stream.__anext__())
    assert first["seq"] == 1

    # The writer outruns the paused (backpressured) reader far enough that
    # seq 2 is evicted before the reader consumes it.
    for content in ("b", "c", "d", "e"):
        buf.append({"type": "response", "content": content})
    assert buf.first_available_seq == 3

    with pytest.raises(TurnReplayGapError):
        await stream.__anext__()


# ---------------------------------------------------------------------------
# Chat-route holder tee
# ---------------------------------------------------------------------------


class _PublishRecorder:
    def __init__(self) -> None:
        self.sync_events: list[dict[str, Any]] = []

    def publish_sync_event(self, **kwargs: Any) -> None:
        self.sync_events.append(kwargs)

    def publish_agent_stream_chunk(self, chunk: dict[str, Any], **kwargs: Any) -> None:
        pass

    def publish_autonomous_event(self, **kwargs: Any) -> None:
        pass

    def create_autonomous_notification(self, **kwargs: Any) -> None:
        pass

    def should_notify_autonomous(self, thread_id: str, _config_mgr: Any) -> bool:
        return True


class _FakeAgent:
    """Holder-turn fake: fires _on_turn_started like the real astream."""

    def __init__(
        self,
        chunks: Optional[list[dict[str, Any]]] = None,
        *,
        fire_turn_started: bool = True,
        raise_after: Optional[int] = None,
        hang_after: Optional[int] = None,
        title: Optional[str] = None,
    ) -> None:
        self.chunks = chunks or [
            {"type": "response", "content": "Hello "},
            {"type": "response", "content": "world"},
        ]
        self.fire_turn_started = fire_turn_started
        self.raise_after = raise_after
        self.hang_after = hang_after
        self.titled: list[tuple[str, str]] = []
        self.astream_kwargs: dict[str, Any] = {}
        self.settings = SimpleNamespace(llm_model="m", llm_provider="p")
        self.thread_metadata_manager = SimpleNamespace(
            auto_title=self._auto_title,
        )
        self._title = title
        self.thread_config_manager = object()
        self.accounts_repo = object()

    def _auto_title(self, user_id: str, thread_id: str, _message: str):
        self.titled.append((user_id, thread_id))
        return self._title

    async def astream(self, message: str, **kwargs: Any):
        self.astream_kwargs = dict(kwargs)
        on_turn_started = kwargs.get("_on_turn_started")
        if self.fire_turn_started and on_turn_started is not None:
            on_turn_started()
        for i, chunk in enumerate(self.chunks):
            if self.raise_after is not None and i >= self.raise_after:
                raise RuntimeError("provider exploded")
            yield dict(chunk)
            if self.hang_after is not None and i + 1 >= self.hang_after:
                await asyncio.sleep(3600)

    def get_context_stats(self, _thread_id: str) -> dict[str, Any]:
        return {"context_tokens": 1}

    def _get_llm_config_for_thread(self, _thread_id: str):
        return SimpleNamespace(model=None, provider=None)


class _AuthUser:
    def __init__(self, user_id: str = "alice") -> None:
        self.id = user_id


class _FakeRequest:
    """Request stand-in whose is_disconnected flips after N polls."""

    def __init__(self, disconnect_after: Optional[int] = None) -> None:
        self.polls = 0
        self.disconnect_after = disconnect_after
        self.headers: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        self.polls += 1
        if self.disconnect_after is None:
            return False
        return self.polls > self.disconnect_after


def _build_router(agent: _FakeAgent, recorder: _PublishRecorder):
    return create_chat_router(
        verify_api_key=lambda: _AuthUser(),
        get_agent_fn=lambda: agent,
        get_settings_fn=lambda: SimpleNamespace(),
        require_thread_access_fn=lambda _user, _thread_id: None,
        publish_sync_event_fn=recorder.publish_sync_event,
        publish_agent_stream_chunk_fn=recorder.publish_agent_stream_chunk,
        publish_autonomous_event_fn=recorder.publish_autonomous_event,
        create_autonomous_notification_fn=recorder.create_autonomous_notification,
        should_notify_autonomous_fn=recorder.should_notify_autonomous,
    )


def _chat_endpoint(router):
    route = next(r for r in router.routes if getattr(r, "path", "") == "/chat")
    return route.endpoint


def _sse_events(frames: list[str]) -> list[dict[str, Any]]:
    events = []
    for frame in frames:
        for line in frame.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line.removeprefix("data: ")))
    return events


async def _drain(response) -> list[str]:
    return [frame async for frame in response.body_iterator]


def test_connected_holder_turn_streams_turn_started_and_buffers_wire_payloads():
    agent = _FakeAgent()
    client = TestClient(
        _app_for(_build_router(agent, _PublishRecorder()))
    )
    with client.stream(
        "POST", "/chat", json={"message": "hi", "thread_id": "t-happy"}
    ) as response:
        body = response.read().decode()

    events = _sse_events(body.splitlines())
    types = [e["type"] for e in events]
    assert types == ["turn_started", "response", "response", "done"]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    turn_id = events[0]["turn_id"]
    assert turn_id

    buffer = get_turn_stream_registry().get("t-happy")
    assert buffer is not None
    assert buffer.turn_id == turn_id
    assert buffer.state == STATE_DONE
    # Wire and replay are byte-identical.
    replayed = [payload for _, payload in buffer._entries]
    wire = [
        line.removeprefix("data: ")
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert replayed == wire


def test_holder_turn_buffer_carries_minted_user_message_id():
    """The route mints ONE id and threads it to both the buffer and astream.

    The buffer copy anchors live-attach viewers; the astream copy is stamped
    on the persisted HumanMessage, so history's ``message_id`` matches.
    """
    agent = _FakeAgent()
    client = TestClient(
        _app_for(_build_router(agent, _PublishRecorder()))
    )
    with client.stream(
        "POST", "/chat", json={"message": "hi", "thread_id": "t-anchor"}
    ) as response:
        response.read()

    minted = agent.astream_kwargs.get("_turn_user_message_id")
    assert isinstance(minted, str) and minted
    buffer = get_turn_stream_registry().get("t-anchor")
    assert buffer is not None
    assert buffer.user_message_id == minted
    assert buffer.snapshot()["user_message_id"] == minted


def test_self_invoke_holder_turn_buffers_autonomous_metadata():
    """Relay turns (the Docker worker's APIClientExecutor posts /chat with
    is_self_invoke) get a first-class autonomous buffer: holder_kind,
    source label, and the internal-anchor flag ride the snapshot (backlog
    #90; previously this buffer existed but carried user-turn metadata)."""
    agent = _FakeAgent()
    client = TestClient(
        _app_for(_build_router(agent, _PublishRecorder()))
    )
    with client.stream(
        "POST",
        "/chat",
        json={
            "message": "wake",
            "thread_id": "t-relay",
            "is_self_invoke": True,
            "source": "ticker",
            "source_label": "daily report",
        },
    ) as response:
        response.read()

    buffer = get_turn_stream_registry().get("t-relay")
    assert buffer is not None
    snap = buffer.snapshot()
    assert snap["holder_kind"] == "autonomous"
    assert snap["source_label"] == "daily report"
    assert snap["user_message_internal"] is True
    # Interactive turns keep user-holder metadata.
    with client.stream(
        "POST", "/chat", json={"message": "hi", "thread_id": "t-user"}
    ) as response:
        response.read()
    snap = get_turn_stream_registry().get("t-user").snapshot()
    assert snap["holder_kind"] == "user"
    assert snap["source_label"] is None
    assert snap["user_message_internal"] is False


@pytest.mark.asyncio
async def test_disconnected_holder_turn_buffers_done_and_auto_titles():
    agent = _FakeAgent(title="Named by turn")
    recorder = _PublishRecorder()
    endpoint = _chat_endpoint(_build_router(agent, recorder))
    fake_request = _FakeRequest(disconnect_after=1)

    response = await endpoint(
        http_request=fake_request,
        request=ChatRequest(message="hi", thread_id="t-drop"),
        user=_AuthUser(),
    )
    events = _sse_events(await _drain(response))

    # The wire saw at most the first chunk; done was suppressed.
    assert "done" not in [e["type"] for e in events]

    buffer = get_turn_stream_registry().get("t-drop")
    assert buffer is not None
    assert buffer.state == STATE_DONE
    buffered_types = [json.loads(p)["type"] for _, p in buffer._entries]
    assert buffered_types == ["turn_started", "response", "response", "done"]
    done_event = json.loads(buffer._entries[-1][1])
    assert done_event["title"] == "Named by turn"

    # Auto-title ran despite the disconnect and published the sync event.
    assert agent.titled == [("alice", "t-drop")]
    assert any(
        e.get("event_type") == "thread_updated" for e in recorder.sync_events
    )


@pytest.mark.asyncio
async def test_error_turn_finishes_buffer_as_error():
    agent = _FakeAgent(raise_after=1)
    endpoint = _chat_endpoint(_build_router(agent, _PublishRecorder()))

    response = await endpoint(
        http_request=_FakeRequest(),
        request=ChatRequest(message="hi", thread_id="t-err"),
        user=_AuthUser(),
    )
    events = _sse_events(await _drain(response))
    assert events[-1]["type"] == "error"

    buffer = get_turn_stream_registry().get("t-err")
    assert buffer is not None
    assert buffer.state == STATE_ERROR
    assert json.loads(buffer._entries[-1][1])["type"] == "error"


@pytest.mark.asyncio
async def test_abandoned_generator_marks_buffer_aborted():
    agent = _FakeAgent(hang_after=1)
    endpoint = _chat_endpoint(_build_router(agent, _PublishRecorder()))

    response = await endpoint(
        http_request=_FakeRequest(),
        request=ChatRequest(message="hi", thread_id="t-gone"),
        user=_AuthUser(),
    )
    iterator = response.body_iterator
    first = await iterator.__anext__()
    assert "turn_started" in first
    await iterator.aclose()

    buffer = get_turn_stream_registry().get("t-gone")
    assert buffer is not None
    assert buffer.state == STATE_ABORTED


def test_non_holder_stream_creates_no_buffer():
    agent = _FakeAgent(
        chunks=[
            {"type": "queued", "content": "Waiting..."},
            {"type": "prompt_queued", "position": 1},
            {"type": "prompt_absorbed"},
        ],
        fire_turn_started=False,
    )
    client = TestClient(_app_for(_build_router(agent, _PublishRecorder())))
    with client.stream(
        "POST", "/chat", json={"message": "hi", "thread_id": "t-queued"}
    ) as response:
        body = response.read().decode()

    events = _sse_events(body.splitlines())
    assert all(e["type"] != "turn_started" for e in events)
    assert all("seq" not in e for e in events)
    assert get_turn_stream_registry().get("t-queued") is None


def test_self_invoke_turn_buffers_turn_started_but_keeps_it_off_the_wire():
    """Worker-relay turns keep their pre-existing first-chunk wire shape.

    The Docker worker relays autonomous turns through POST /chat
    (APIClientExecutor); the trigger manager's task_started gate and the
    autonomous event-bus mirror key off the FIRST wire chunk, so the
    synthesized turn_started stays out of the relay wire while still leading
    the replay buffer.
    """
    agent = _FakeAgent()
    client = TestClient(_app_for(_build_router(agent, _PublishRecorder())))
    with client.stream(
        "POST",
        "/chat",
        json={"message": "hi", "thread_id": "t-relay", "is_self_invoke": True},
    ) as response:
        body = response.read().decode()

    types = [e["type"] for e in _sse_events(body.splitlines())]
    assert "turn_started" not in types
    assert types == ["response", "response", "done"]

    buffer = get_turn_stream_registry().get("t-relay")
    assert buffer is not None
    buffered_types = [json.loads(p)["type"] for _, p in buffer._entries]
    assert buffered_types[0] == "turn_started"


def _app_for(router) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app
