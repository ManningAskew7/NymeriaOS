"""Tests for sync consumption of the async agent stream."""

from __future__ import annotations

from nymeria.core.stream_bridge import iter_agent_astream


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
