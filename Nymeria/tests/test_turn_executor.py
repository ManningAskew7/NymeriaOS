"""Tests for the TurnExecutor abstraction.

LocalAgentExecutor must pass kwargs through unchanged (slim should behave
exactly like the old direct-agent code). APIClientExecutor must translate
agent-style kwargs into chat_stream kwargs and inject
publish_autonomous_events.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nymeria.core.turn_executor import (
    APIClientExecutor,
    LocalAgentExecutor,
    TurnExecutor,
    wrap_for_stream,
)


class _RecordingAgent:
    """Captures astream kwargs and yields a canned chunk sequence."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.chunks: list[dict[str, Any]] = [
            {"type": "response", "content": "hi"},
            {"type": "done"},
        ]

    async def astream(self, **kwargs: Any):
        self.calls.append(dict(kwargs))
        for chunk in self.chunks:
            yield chunk


class _RecordingAPIClient:
    """Captures chat_stream kwargs and yields a canned chunk sequence."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.chunks: list[dict[str, Any]] = [
            {"type": "response", "content": "remote"},
            {"type": "done"},
        ]
        self.aclose_count = 0

    async def chat_stream(self, **kwargs: Any):
        self.calls.append(dict(kwargs))
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.aclose_count += 1


# ---------------------------------------------------------------------------
# LocalAgentExecutor
# ---------------------------------------------------------------------------


def test_local_executor_marks_local():
    agent = _RecordingAgent()
    executor = LocalAgentExecutor(agent)
    assert executor.is_remote is False
    assert isinstance(executor, TurnExecutor)


def test_local_executor_passes_kwargs_through_unchanged():
    agent = _RecordingAgent()
    executor = LocalAgentExecutor(agent)

    async def drive():
        chunks = []
        async for chunk in executor.astream(
            message="hello",
            thread_id="t1",
            user_id="u1",
            _is_self_invoke=True,
            _trigger_override="ticker",
            source="ticker",
            source_id="todo-42",
            source_label="Check inbox",
            attachments=[{"file_type": "image", "data_url": "data:image/png;base64,x"}],
            force_unsupported_attachments=True,
            images=[{"data_url": "data:image/png;base64,y", "mime_type": "image/png"}],
        ):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(drive())

    assert [c["type"] for c in chunks] == ["response", "done"]
    assert agent.calls == [
        {
            "message": "hello",
            "thread_id": "t1",
            "user_id": "u1",
            "_is_self_invoke": True,
            "_trigger_override": "ticker",
            "source": "ticker",
            "source_id": "todo-42",
            "source_label": "Check inbox",
            "attachments": [
                {"file_type": "image", "data_url": "data:image/png;base64,x"}
            ],
            "force_unsupported_attachments": True,
            "images": [
                {"data_url": "data:image/png;base64,y", "mime_type": "image/png"}
            ],
        }
    ]


# ---------------------------------------------------------------------------
# APIClientExecutor
# ---------------------------------------------------------------------------


def test_api_executor_marks_remote():
    client = _RecordingAPIClient()
    executor = APIClientExecutor(client)
    assert executor.is_remote is True
    assert isinstance(executor, TurnExecutor)


def test_api_executor_translates_underscore_kwargs_and_injects_flag():
    client = _RecordingAPIClient()
    executor = APIClientExecutor(client, publish_autonomous_events=False)

    async def drive():
        async for _ in executor.astream(
            message="trigger fired",
            thread_id="t-trig",
            user_id="owner",
            _is_self_invoke=True,
            _trigger_override="trigger",
            source="trigger",
            source_id="trig-9",
            source_label="Email Trigger",
            attachments=None,
        ):
            pass

    asyncio.run(drive())

    assert client.calls == [
        {
            "message": "trigger fired",
            "thread_id": "t-trig",
            "user_id": "owner",
            "is_self_invoke": True,
            "trigger_override": "trigger",
            "source": "trigger",
            "source_id": "trig-9",
            "source_label": "Email Trigger",
            "attachments": None,
            "publish_autonomous_events": False,
        }
    ]


def test_api_executor_defaults_publish_flag_false_for_worker_relay():
    client = _RecordingAPIClient()
    executor = APIClientExecutor(client)

    async def drive():
        async for _ in executor.astream(message="m", thread_id="t", user_id="u"):
            pass

    asyncio.run(drive())

    assert client.calls[0]["publish_autonomous_events"] is False


def test_api_executor_publish_flag_can_be_set_true():
    """Webhook-fire-style callers can opt in to API-side mirroring."""
    client = _RecordingAPIClient()
    executor = APIClientExecutor(client, publish_autonomous_events=True)

    async def drive():
        async for _ in executor.astream(message="m", thread_id="t", user_id="u"):
            pass

    asyncio.run(drive())

    assert client.calls[0]["publish_autonomous_events"] is True


def test_api_executor_passes_chunks_through_unchanged():
    client = _RecordingAPIClient()
    executor = APIClientExecutor(client)

    async def drive():
        chunks = []
        async for chunk in executor.astream(message="m", thread_id="t", user_id="u"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(drive())

    assert chunks == client.chunks


def test_api_executor_drops_images_kwarg():
    """Legacy 'images' isn't on chat_stream; APIClientExecutor must drop it
    rather than send a kwarg the server would 422 on."""
    client = _RecordingAPIClient()
    executor = APIClientExecutor(client)

    async def drive():
        async for _ in executor.astream(
            message="m",
            thread_id="t",
            user_id="u",
            images=[{"data_url": "data:image/png;base64,y", "mime_type": "image/png"}],
        ):
            pass

    asyncio.run(drive())

    assert "images" not in client.calls[0]


def test_api_executor_aclose_closes_client():
    client = _RecordingAPIClient()
    executor = APIClientExecutor(client)

    asyncio.run(executor.aclose())

    assert client.aclose_count == 1


# ---------------------------------------------------------------------------
# wrap_for_stream
# ---------------------------------------------------------------------------


def test_wrap_for_stream_wraps_agent():
    agent = _RecordingAgent()
    wrapped = wrap_for_stream(agent)
    assert isinstance(wrapped, LocalAgentExecutor)


def test_wrap_for_stream_passes_executor_through():
    client = _RecordingAPIClient()
    executor = APIClientExecutor(client)
    assert wrap_for_stream(executor) is executor


def test_wrap_for_stream_passes_local_executor_through():
    agent = _RecordingAgent()
    executor = LocalAgentExecutor(agent)
    assert wrap_for_stream(executor) is executor
