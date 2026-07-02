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


# ---------------------------------------------------------------------------
# run_workflow (phase 4): the headless-workflow relay seam
# ---------------------------------------------------------------------------


def test_local_executor_run_workflow_returns_envelope(monkeypatch):
    from types import SimpleNamespace

    async def fake_run(loader, workflow_id, params, *, user_id, thread_id, depth=0):
        run = SimpleNamespace(
            envelope=SimpleNamespace(
                to_dict=lambda: {"ok": True, "status": "ok", "output": params}
            )
        )
        return None, run

    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.run_workflow_by_id", fake_run
    )
    monkeypatch.setattr(
        "nymeria.core.custom_tools.get_custom_tool_loader", lambda: None
    )
    executor = LocalAgentExecutor(_RecordingAgent())
    envelope = asyncio.run(
        executor.run_workflow("wf_x", {"a": 1}, user_id="u1", thread_id="t1")
    )
    assert envelope == {"ok": True, "status": "ok", "output": {"a": 1}}


def test_local_executor_run_workflow_raises_on_refusal(monkeypatch):
    async def fake_run(loader, workflow_id, params, *, user_id, thread_id, depth=0):
        return "execution requires an admin-approved revision", None

    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.run_workflow_by_id", fake_run
    )
    monkeypatch.setattr(
        "nymeria.core.custom_tools.get_custom_tool_loader", lambda: None
    )
    executor = LocalAgentExecutor(_RecordingAgent())
    try:
        asyncio.run(
            executor.run_workflow("wf_x", {}, user_id="u1", thread_id="t1")
        )
    except RuntimeError as exc:
        assert "admin-approved" in str(exc)
    else:
        raise AssertionError("refusal did not raise")


class _WorkflowAPIClient:
    """API-client stand-in for the run_workflow relay."""

    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def run_workflow(self, workflow_id, params=None, *, user_id, thread_id=None):
        self.calls.append(
            {
                "workflow_id": workflow_id,
                "params": params,
                "user_id": user_id,
                "thread_id": thread_id,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result

    async def aclose(self) -> None:
        pass


def test_api_executor_run_workflow_unwraps_envelope():
    client = _WorkflowAPIClient(
        result={"workflow_id": "wf_x", "run_id": "r1", "envelope": {"ok": True}}
    )
    executor = APIClientExecutor(client)
    envelope = asyncio.run(
        executor.run_workflow("wf_x", {"a": 1}, user_id="u1", thread_id="t1")
    )
    assert envelope == {"ok": True}
    assert client.calls[0]["thread_id"] == "t1"


def test_api_executor_run_workflow_maps_http_error_to_runtime_error():
    import httpx

    response = httpx.Response(
        403,
        json={"detail": "execution requires an admin-approved revision"},
        request=httpx.Request("POST", "http://api/workflows/wf_x/execute"),
    )
    error = httpx.HTTPStatusError("403", request=response.request, response=response)
    executor = APIClientExecutor(_WorkflowAPIClient(error=error))
    try:
        asyncio.run(executor.run_workflow("wf_x", {}, user_id="u1", thread_id="t1"))
    except RuntimeError as exc:
        assert "admin-approved" in str(exc)
    else:
        raise AssertionError("HTTP refusal did not raise")


def test_api_executor_run_workflow_requires_envelope():
    executor = APIClientExecutor(_WorkflowAPIClient(result={"nope": True}))
    try:
        asyncio.run(executor.run_workflow("wf_x", {}, user_id="u1", thread_id="t1"))
    except RuntimeError as exc:
        assert "no envelope" in str(exc)
    else:
        raise AssertionError("missing envelope did not raise")
