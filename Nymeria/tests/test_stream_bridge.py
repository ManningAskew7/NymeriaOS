"""Tests for sync consumption of the async agent stream."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nymeria.vendor.react_agent import providers
from nymeria.vendor.react_agent.config import LLMConfig
from nymeria.vendor.react_agent.providers import create_llm
from nymeria.core.stream_bridge import iter_agent_astream, stream_and_collect
from nymeria.core.turn_stream_buffer import (
    get_turn_stream_registry,
    reset_turn_stream_registry,
)


class _FakeAgent:
    def __init__(self):
        self.calls = []

    async def astream(self, **kwargs):
        self.calls.append(kwargs)
        yield {"type": "thinking", "content": "working"}
        yield {"type": "tool_result", "name": "tool_create", "result": "ok"}
        yield {"type": "response", "content": "done"}


def test_iter_agent_astream_yields_async_chunks_from_sync_context():
    agent = _FakeAgent()

    chunks = list(iter_agent_astream(
        agent,
        message="build a tool",
        thread_id="thread-a",
        user_id="owner",
        _is_self_invoke=True,
    ))

    assert [chunk["type"] for chunk in chunks] == [
        "thinking",
        "tool_result",
        "response",
    ]
    assert agent.calls == [{
        "message": "build a tool",
        "thread_id": "thread-a",
        "user_id": "owner",
        "_is_self_invoke": True,
    }]


def test_iter_agent_astream_propagates_errors_after_prior_chunks():
    class ErrorAgent:
        async def astream(self, **kwargs):
            yield {"type": "thinking", "content": "started"}
            raise RuntimeError("astream failed")

    iterator = iter_agent_astream(ErrorAgent(), message="x")

    assert next(iterator) == {"type": "thinking", "content": "started"}
    try:
        next(iterator)
    except RuntimeError as exc:
        assert str(exc) == "astream failed"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected RuntimeError")


def test_iter_agent_astream_closes_async_generator_when_consumer_stops():
    released = []

    class ReleasingAgent:
        async def astream(self, **kwargs):
            try:
                yield {"type": "thinking", "content": "started"}
                yield {"type": "response", "content": "unreached"}
            finally:
                released.append(True)

    iterator = iter_agent_astream(ReleasingAgent(), message="x")

    assert next(iterator) == {"type": "thinking", "content": "started"}
    iterator.close()

    assert released == [True]


def test_iter_agent_astream_reuses_bridge_loop_for_loop_bound_clients():
    class LoopBoundAgent:
        def __init__(self):
            self.loop = None

        async def astream(self, **kwargs):
            loop = asyncio.get_running_loop()
            if self.loop is None:
                self.loop = loop
            elif loop is not self.loop:
                if self.loop.is_closed():
                    raise RuntimeError("Event loop is closed")
                raise RuntimeError("changed event loop")
            yield {"type": "response", "content": kwargs["message"]}

    agent = LoopBoundAgent()

    assert list(iter_agent_astream(agent, message="first")) == [
        {"type": "response", "content": "first"}
    ]
    assert list(iter_agent_astream(agent, message="second")) == [
        {"type": "response", "content": "second"}
    ]
    assert agent.loop is not None
    assert not agent.loop.is_closed()


def test_iter_agent_astream_uses_one_bridge_loop_for_parallel_sync_consumers():
    loop_ids = set()

    class ConcurrentAgent:
        async def astream(self, **kwargs):
            loop_ids.add(id(asyncio.get_running_loop()))
            await asyncio.sleep(0.01)
            yield {"type": "response", "content": kwargs["message"]}

    agent = ConcurrentAgent()

    def consume(index: int):
        return list(iter_agent_astream(agent, message=f"run-{index}"))

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(consume, range(3)))

    assert results == [
        [{"type": "response", "content": "run-0"}],
        [{"type": "response", "content": "run-1"}],
        [{"type": "response", "content": "run-2"}],
    ]
    assert len(loop_ids) == 1


def test_iter_agent_astream_uses_loop_local_anthropic_client_after_api_loop_touch(
    monkeypatch,
):
    monkeypatch.setattr(
        providers,
        "_should_use_cliproxy_context_management_adapter",
        lambda _chat_model_cls: (True, "test"),
    )

    llm = create_llm(
        LLMConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_key="test-key",
            base_url="http://cli-proxy-api-latest:8317",
            temperature=None,
        )
    )

    async def touch_from_api_loop():
        api_client = llm._async_client
        await providers.close_anthropic_async_http_pools_for_loop()
        return api_client

    api_loop_client = asyncio.run(touch_from_api_loop())

    class CachedGraphAgent:
        async def astream(self, **kwargs):
            bridge_client = llm._async_client
            if bridge_client is api_loop_client:
                raise RuntimeError("bound to a different event loop")
            if bridge_client._client is api_loop_client._client:
                raise RuntimeError("Event loop is closed")
            yield {"type": "response", "content": kwargs["message"]}

    assert list(iter_agent_astream(CachedGraphAgent(), message="ok")) == [
        {"type": "response", "content": "ok"}
    ]


def test_iter_agent_astream_uses_loop_scoped_openai_client_after_api_loop_touch():
    class LoopScopedOpenAIAgent:
        def __init__(self):
            self.http_clients_by_loop = {}

        async def astream(self, **kwargs):
            loop = asyncio.get_running_loop()
            if id(loop) not in self.http_clients_by_loop:
                llm = create_llm(
                    LLMConfig(
                        provider="openai",
                        model="gpt-5.5",
                        api_key="test-key",
                        base_url="http://cli-proxy-api-latest:8317/v1",
                        temperature=None,
                    )
                )
                self.http_clients_by_loop[id(loop)] = llm.root_async_client._client
            yield {"type": "response", "content": kwargs["message"]}

    agent = LoopScopedOpenAIAgent()

    async def collect_from_api_loop():
        return [chunk async for chunk in agent.astream(message="api")]

    api_loop = asyncio.new_event_loop()
    try:
        assert api_loop.run_until_complete(collect_from_api_loop()) == [
            {"type": "response", "content": "api"}
        ]
        api_http_client = next(iter(agent.http_clients_by_loop.values()))

        assert list(iter_agent_astream(agent, message="bridge")) == [
            {"type": "response", "content": "bridge"}
        ]

        bridge_clients = [
            client
            for client in agent.http_clients_by_loop.values()
            if client is not api_http_client
        ]
        assert len(bridge_clients) == 1
        assert not bridge_clients[0].is_closed
    finally:
        api_loop.run_until_complete(
            providers.close_provider_async_http_pools_for_loop(api_loop)
        )
        api_loop.close()


def test_stream_and_collect_tracks_common_stream_state():
    class CollectingAgent:
        async def astream(self, **kwargs):
            yield {"type": "thinking", "content": "working"}
            yield {"type": "tool_call", "name": "lookup"}
            yield {"type": "response", "content": "done"}
            yield {"type": "iteration_limit", "scope": "sub_agent"}
            yield {
                "type": "iteration_limit",
                "scope": "main_agent",
                "reason": "max_iterations",
            }

    seen = []

    def on_chunk(chunk, collection):
        seen.append((chunk["type"], collection.chunk_count, collection.tool_call_count))

    result = stream_and_collect(
        CollectingAgent(),
        astream_kwargs={"message": "x"},
        on_chunk=on_chunk,
        should_mark_iteration_limit=lambda chunk: chunk.get("scope") == "main_agent",
    )

    assert result.response_parts == ["done"]
    assert result.thinking_parts == ["working"]
    assert result.response_text() == "done"
    assert result.chunk_count == 5
    assert result.tool_call_count == 1
    assert result.iteration_limit_hit is True
    assert result.iteration_limit_event == {
        "type": "iteration_limit",
        "scope": "main_agent",
        "reason": "max_iterations",
    }
    assert seen == [
        ("thinking", 1, 0),
        ("tool_call", 2, 1),
        ("response", 3, 1),
        ("iteration_limit", 4, 1),
        ("iteration_limit", 5, 1),
    ]


def test_stream_and_collect_raises_error_after_chunk_callback():
    class ErrorAgent:
        async def astream(self, **kwargs):
            yield {"type": "error", "content": "", "code": "boom"}

    seen = []

    def on_chunk(chunk, collection):
        seen.append((chunk["type"], collection.chunk_count))

    with pytest.raises(RuntimeError, match="custom boom"):
        stream_and_collect(
            ErrorAgent(),
            astream_kwargs={"message": "x"},
            on_chunk=on_chunk,
            error_message_factory=lambda chunk: f"custom {chunk['code']}",
        )

    assert seen == [("error", 1)]


def test_autonomous_callers_use_stream_and_collect_for_collection_loops():
    repo_root = Path(__file__).resolve().parents[1]
    paths = [
        repo_root / "nymeria/core/ticker.py",
        repo_root / "nymeria/core/trigger_manager.py",
        repo_root / "nymeria/core/thread_agent_executor.py",
        repo_root / "nymeria/tools/spawn_thread.py",
    ]

    for path in paths:
        assert "for chunk in iter_agent_astream(" not in path.read_text()


# ---------------------------------------------------------------------------
# Turn-buffer tee (backlog #90 slice 1): local turns consumed through
# stream_and_collect feed the per-thread turn stream buffer so any client can
# attach to them; remote (relayed) turns are buffered by the chat route.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_turn_registry():
    reset_turn_stream_registry()
    yield
    reset_turn_stream_registry()


class _HolderAgent:
    """Fake agent honoring the holder-turn callback like NymeriaAgent.astream."""

    def __init__(self, chunks, mid_stream_exc: BaseException | None = None):
        self._chunks = list(chunks)
        self._mid_stream_exc = mid_stream_exc
        self.seen_kwargs: dict = {}

    async def astream(self, **kwargs):
        self.seen_kwargs = kwargs
        on_started = kwargs.get("_on_turn_started")
        if on_started is not None:
            on_started()
        for chunk in self._chunks:
            yield chunk
        if self._mid_stream_exc is not None:
            raise self._mid_stream_exc


def _buffered_events(thread_id: str) -> list[dict]:
    buffer = get_turn_stream_registry().get(thread_id)
    assert buffer is not None
    return [json.loads(payload) for _, payload in buffer._entries]


def test_stream_and_collect_tees_local_holder_turn_into_buffer():
    agent = _HolderAgent([
        {"type": "thinking", "content": "hm"},
        {"type": "response", "content": "done"},
    ])

    stream_and_collect(
        agent,
        astream_kwargs={
            "message": "wake",
            "thread_id": "t-tee",
            "user_id": "owner",
            "_is_self_invoke": True,
            "source": "ticker",
            "source_label": "daily report",
        },
    )

    buffer = get_turn_stream_registry().get("t-tee")
    assert buffer is not None
    snap = buffer.snapshot()
    assert snap["state"] == "done"
    assert snap["holder_kind"] == "autonomous"
    assert snap["source_label"] == "daily report"
    assert snap["user_message_internal"] is True
    # The minted anchor reached both the buffer and the agent kwargs (so the
    # wakeup HumanMessage gets stamped with the same id).
    minted = agent.seen_kwargs["_turn_user_message_id"]
    assert minted and snap["user_message_id"] == minted

    events = _buffered_events("t-tee")
    assert [e["type"] for e in events] == [
        "turn_started",
        "thinking",
        "response",
        "done",
    ]
    assert all(e["thread_id"] == "t-tee" for e in events)
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[0]["turn_id"] == buffer.turn_id


def test_stream_and_collect_does_not_tee_remote_executor():
    class _RemoteExecutor:
        is_remote = True

        def __init__(self):
            self.seen_kwargs: dict = {}

        async def astream(self, **kwargs):
            self.seen_kwargs = kwargs
            yield {"type": "response", "content": "relayed"}

    executor = _RemoteExecutor()
    stream_and_collect(
        executor,
        astream_kwargs={
            "message": "wake",
            "thread_id": "t-remote",
            "user_id": "owner",
            "_is_self_invoke": True,
        },
    )

    # The API-side chat route buffers relay turns; the worker side must not.
    assert get_turn_stream_registry().get("t-remote") is None
    assert "_on_turn_started" not in executor.seen_kwargs
    assert "_turn_user_message_id" not in executor.seen_kwargs


def test_stream_and_collect_error_chunk_finishes_buffer_error():
    agent = _HolderAgent([
        {"type": "thinking", "content": "hm"},
        {"type": "error", "content": "boom", "code": "provider"},
    ])

    with pytest.raises(RuntimeError, match="boom"):
        stream_and_collect(
            agent,
            astream_kwargs={
                "message": "wake",
                "thread_id": "t-err",
                "user_id": "owner",
                "_is_self_invoke": True,
            },
        )

    buffer = get_turn_stream_registry().get("t-err")
    assert buffer is not None
    assert buffer.state == "error"
    assert [e["type"] for e in _buffered_events("t-err")] == [
        "turn_started",
        "thinking",
        "error",
    ]


def test_stream_and_collect_raised_exception_buffers_error_event():
    """A raised exception mirrors the chat route: a synthesized error wire
    event followed by the error terminal state, so an attached watcher sees
    the failure instead of a bare abort."""
    agent = _HolderAgent(
        [{"type": "thinking", "content": "hm"}],
        mid_stream_exc=RuntimeError("transport died"),
    )

    with pytest.raises(RuntimeError, match="transport died"):
        stream_and_collect(
            agent,
            astream_kwargs={
                "message": "wake",
                "thread_id": "t-raise",
                "user_id": "owner",
                "_is_self_invoke": True,
            },
        )

    buffer = get_turn_stream_registry().get("t-raise")
    assert buffer is not None
    assert buffer.state == "error"
    events = _buffered_events("t-raise")
    assert [e["type"] for e in events] == ["turn_started", "thinking", "error"]
    assert events[-1]["content"] == "transport died"


def test_stream_and_collect_cancellation_marks_buffer_aborted():
    """Cancellation (a BaseException) skips the error synthesis and lands on
    the aborted backstop, matching the route's GeneratorExit handling."""
    agent = _HolderAgent(
        [{"type": "thinking", "content": "hm"}],
        mid_stream_exc=asyncio.CancelledError(),
    )

    # The bridge surfaces a cancelled future as concurrent.futures
    # CancelledError (an Exception subclass on some stdlib layouts).
    with pytest.raises((asyncio.CancelledError, concurrent.futures.CancelledError)):
        stream_and_collect(
            agent,
            astream_kwargs={
                "message": "wake",
                "thread_id": "t-abort",
                "user_id": "owner",
                "_is_self_invoke": True,
            },
        )

    buffer = get_turn_stream_registry().get("t-abort")
    assert buffer is not None
    assert buffer.state == "aborted"
    assert [e["type"] for e in _buffered_events("t-abort")] == [
        "turn_started",
        "thinking",
    ]


def test_stream_and_collect_user_holder_turn_carries_no_source_label():
    """Non-self-invoke turns (e.g. a user-initiated callable handoff) are
    user-holder turns: no source label, visible anchor (the route's rule)."""
    agent = _HolderAgent([{"type": "response", "content": "ok"}])

    stream_and_collect(
        agent,
        astream_kwargs={
            "message": "hello",
            "thread_id": "t-user-holder",
            "user_id": "owner",
            "_is_self_invoke": False,
            "source": "callable",
            "source_label": "research assistant",
        },
    )

    buffer = get_turn_stream_registry().get("t-user-holder")
    assert buffer is not None
    snap = buffer.snapshot()
    assert snap["holder_kind"] == "user"
    assert snap["source_label"] is None
    assert snap["user_message_internal"] is False


def test_stream_and_collect_no_buffer_when_turn_never_holds_lock():
    """Queued/absorbed self-invoke calls never fire the holder callback, so
    no buffer is created (the running holder's buffer owns the thread)."""

    class _QueuedAgent:
        async def astream(self, **kwargs):
            yield {"type": "prompt_absorbed", "thread_id": "t-queued"}

    stream_and_collect(
        _QueuedAgent(),
        astream_kwargs={
            "message": "wake",
            "thread_id": "t-queued",
            "user_id": "owner",
            "_is_self_invoke": True,
        },
    )

    assert get_turn_stream_registry().get("t-queued") is None


def test_stream_and_collect_buffers_each_chunk_before_on_chunk_fires():
    """Pin the tee-before-callback ordering (backlog #90 slice 3).

    Every autonomous publish site (ticker, trigger manager, thread agent
    executor, dreaming, watchdog) fires its ``task_started`` bus event from
    inside its ``on_chunk`` callback. GUI clients attach to the turn buffer
    on that signal, so by the time ``on_chunk`` runs for any chunk the
    buffer MUST already exist, be live, and contain that chunk; otherwise
    the attach could race a buffer that does not exist yet. The ordering is
    currently guaranteed by ``stream_and_collect`` recording into the tee
    before invoking ``on_chunk``: this test makes that load-bearing order
    explicit instead of emergent.
    """
    agent = _HolderAgent([
        {"type": "thinking", "content": "hm"},
        {"type": "response", "content": "done"},
    ])
    observed: list[tuple[str, str, str]] = []

    def on_chunk(chunk, collection):
        buffer = get_turn_stream_registry().get("t-order")
        assert buffer is not None, "buffer missing when on_chunk fired"
        types = [json.loads(payload)["type"] for _, payload in buffer._entries]
        assert types, "buffer empty when on_chunk fired"
        observed.append((chunk["type"], buffer.state, types[-1]))

    stream_and_collect(
        agent,
        astream_kwargs={
            "message": "wake",
            "thread_id": "t-order",
            "user_id": "owner",
            "_is_self_invoke": True,
        },
        on_chunk=on_chunk,
    )

    # Each callback saw a live buffer whose newest entry was the very chunk
    # it was invoked with (turn_started precedes the first one).
    assert observed == [
        ("thinking", "live", "thinking"),
        ("response", "live", "response"),
    ]


def test_stream_and_collect_preserves_caller_anchor_and_holder_callback():
    fired = []
    agent = _HolderAgent([{"type": "response", "content": "ok"}])

    stream_and_collect(
        agent,
        astream_kwargs={
            "message": "wake",
            "thread_id": "t-chain",
            "user_id": "owner",
            "_is_self_invoke": True,
            "_turn_user_message_id": "caller-anchor",
            "_on_turn_started": lambda: fired.append(True),
        },
    )

    buffer = get_turn_stream_registry().get("t-chain")
    assert buffer is not None
    assert buffer.user_message_id == "caller-anchor"
    assert agent.seen_kwargs["_turn_user_message_id"] == "caller-anchor"
    assert fired == [True]


def test_stream_and_collect_done_carries_context_stats_and_model():
    """The synthesized done mirrors the chat route's payload garnish when the
    local executor exposes its agent."""

    class _StatsAgent(_HolderAgent):
        def __init__(self):
            super().__init__([{"type": "response", "content": "ok"}])
            self.settings = type("S", (), {"llm_model": "global-model"})()

        def get_context_stats(self, thread_id):
            return {"total_tokens": 42, "thread": thread_id}

        def _get_llm_config_for_thread(self, thread_id):
            return type("Cfg", (), {"model": "thread-model"})()

    stream_and_collect(
        _StatsAgent(),
        astream_kwargs={
            "message": "wake",
            "thread_id": "t-done",
            "user_id": "owner",
            "_is_self_invoke": True,
        },
    )

    done = _buffered_events("t-done")[-1]
    assert done["type"] == "done"
    assert done["context_stats"] == {"total_tokens": 42, "thread": "t-done"}
    assert done["model"] == "thread-model"


class _QueuerAgent:
    """Astream shape of a consumer whose prompt queued behind a holder turn:
    queue-meta events, then the holder's output fanned in via the mailbox,
    ending at the absorption sentinel."""

    async def astream(self, **kwargs):
        yield {"type": "queued", "content": "Waiting for current turn to halt..."}
        yield {"type": "prompt_queued", "position": 1, "source": "callable"}
        yield {"type": "prompt_injected", "prompts": ["handoff text"]}
        yield {"type": "thinking", "content": "holder thinking"}
        yield {"type": "response", "content": "holder response"}
        yield {"type": "prompt_absorbed", "thread_id": "t-fan"}


def test_stream_and_collect_marks_fanout_chunks_after_prompt_queued():
    """Everything after prompt_queued is the holder's mirrored output: the
    on_chunk copies carry ``fanout``, the originals stay unmutated, the
    collection still aggregates content, and ``fanout_observed`` latches."""

    seen: list[dict] = []
    collection = stream_and_collect(
        _QueuerAgent(),
        astream_kwargs={
            "message": "handoff",
            "thread_id": "t-fan",
            "user_id": "owner",
            "_is_self_invoke": True,
        },
        on_chunk=lambda chunk, _c: seen.append(chunk),
    )

    marked = {c["type"]: bool(c.get("fanout")) for c in seen}
    assert marked == {
        "queued": False,
        "prompt_queued": False,
        "prompt_injected": True,
        "thinking": True,
        "response": True,
        "prompt_absorbed": True,
    }
    assert collection.fanout_observed is True
    # The queuer's task_completed content is the point of the fanout: the
    # collection keeps aggregating the mirrored chunks.
    assert collection.response_text() == "holder response"


def test_stream_and_collect_holder_stream_is_never_marked():
    seen: list[dict] = []
    collection = stream_and_collect(
        _FakeAgent(),
        astream_kwargs={
            "message": "wake",
            "thread_id": "t-holder-unmarked",
            "user_id": "owner",
            "_is_self_invoke": True,
        },
        on_chunk=lambda chunk, _c: seen.append(chunk),
    )

    assert collection.fanout_observed is False
    assert all("fanout" not in chunk for chunk in seen)


class _QueuerErrorAgent:
    """Queuer stream whose fanned-in holder turn ends in an error chunk."""

    async def astream(self, **kwargs):
        yield {"type": "queued", "content": "Waiting..."}
        yield {"type": "prompt_queued", "position": 1, "source": "callable"}
        yield {"type": "error", "content": "holder blew up", "code": "boom"}


def test_stream_and_collect_error_carries_fanout_latch():
    """The StreamCollection dies with the raise, so the fanout latch must
    ride the exception for publishers' error tails to stamp their
    task_completed bookends."""
    with pytest.raises(RuntimeError) as excinfo:
        stream_and_collect(
            _QueuerErrorAgent(),
            astream_kwargs={
                "message": "handoff",
                "thread_id": "t-fan-err",
                "user_id": "owner",
                "_is_self_invoke": True,
            },
        )
    assert getattr(excinfo.value, "fanout_observed", None) is True
