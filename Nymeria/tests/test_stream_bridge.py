"""Tests for sync consumption of the async agent stream."""

from __future__ import annotations

from nymeria.core.stream_bridge import iter_agent_astream


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def stream(self, *args, **kwargs):  # pragma: no cover - regression guard
        raise AssertionError("sync stream should not be used")

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
