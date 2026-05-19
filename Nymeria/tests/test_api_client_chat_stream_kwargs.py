"""Verify chat_stream forwards new gating + trigger metadata kwargs.

The Docker worker relies on ``publish_autonomous_events=False`` plus the
``trigger_id`` / ``trigger_name`` body fields to keep its task IDs stable
across worker-published and API-published events. This test pins the
request body shape so a regression in the client can't silently break that.
"""

from __future__ import annotations

import asyncio
from typing import Any

from nymeria.triggers.api_client import NymeriaAPIClient


class _RecordingStreamResponse:
    """Stand-in for an httpx streaming Response."""

    def __init__(self) -> None:
        self.status_code = 200
        # Two SSE lines so the consumer parses at least one event.
        self._lines = [
            'data: {"type": "response", "content": "ok"}',
            'data: {"type": "done"}',
            "",
        ]

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _RecordingStreamContext:
    def __init__(self, owner: "_RecordingClient", method: str, url: str, kwargs: dict[str, Any]):
        self._owner = owner
        owner.requests.append(
            {
                "method": method,
                "url": url,
                "json": kwargs.get("json"),
                "timeout": kwargs.get("timeout"),
                "headers": kwargs.get("headers"),
            }
        )

    async def __aenter__(self) -> _RecordingStreamResponse:
        return _RecordingStreamResponse()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _RecordingClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, **kwargs: Any) -> _RecordingStreamContext:
        return _RecordingStreamContext(self, method, url, kwargs)


def _drive(client: NymeriaAPIClient, **kwargs: Any) -> list[dict[str, Any]]:
    async def _run() -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        async for chunk in client.chat_stream(**kwargs):
            chunks.append(chunk)
        return chunks

    return asyncio.run(_run())


def _make_client() -> tuple[NymeriaAPIClient, _RecordingClient]:
    client = NymeriaAPIClient("http://api.test", "tok")
    recording = _RecordingClient()
    client._client_for_loop = lambda: recording  # type: ignore[method-assign]
    return client, recording


def test_chat_stream_includes_publish_flag_and_trigger_metadata_when_set():
    client, recording = _make_client()

    _drive(
        client,
        message="trigger fired",
        thread_id="t",
        user_id="u",
        is_self_invoke=True,
        trigger_override="trigger",
        publish_autonomous_events=False,
        trigger_id="trig-9",
        trigger_name="Email Trigger",
        source="trigger",
        source_id="trig-9",
        source_label="Email Trigger",
    )

    assert recording.requests, "expected a stream request"
    body = recording.requests[0]["json"]
    assert body["is_self_invoke"] is True
    assert body["trigger_override"] == "trigger"
    assert body["publish_autonomous_events"] is False
    assert body["trigger_id"] == "trig-9"
    assert body["trigger_name"] == "Email Trigger"
    assert body["source"] == "trigger"
    assert body["source_label"] == "Email Trigger"


def test_chat_stream_omits_publish_flag_when_unset():
    """Existing callers that don't pass the flag must not change behavior."""
    client, recording = _make_client()

    _drive(
        client,
        message="hi",
        thread_id="t",
        user_id="u",
        is_self_invoke=True,
        trigger_override="watchdog",
    )

    body = recording.requests[0]["json"]
    assert "publish_autonomous_events" not in body, (
        "client must not inject publish_autonomous_events when caller "
        "leaves it None — preserves API default (True)"
    )


def test_chat_stream_uses_no_read_timeout_for_self_invoke():
    """Autonomous calls may queue behind a busy thread for >5min; the
    client must use the no-read-timeout pool so the worker doesn't time
    out and double-fire the TODO."""
    client, recording = _make_client()

    _drive(
        client,
        message="ticker fired",
        thread_id="t",
        user_id="u",
        is_self_invoke=True,
    )

    timeout = recording.requests[0]["timeout"]
    # httpx.Timeout exposes a ``read`` attribute; SSE-style is read=None.
    assert timeout.read is None


def test_chat_stream_uses_bounded_timeout_for_user_chat():
    """Regular user chat keeps the existing 5-minute read timeout."""
    client, recording = _make_client()

    _drive(client, message="hi", thread_id="t", user_id="u")

    timeout = recording.requests[0]["timeout"]
    assert timeout.read == 300


def test_chat_stream_returns_parsed_chunks():
    """Sanity check the parser still works with the new code paths."""
    client, recording = _make_client()

    chunks = _drive(client, message="hi", thread_id="t", user_id="u")

    assert chunks == [
        {"type": "response", "content": "ok"},
        {"type": "done"},
    ]
    # Body must include the basics every call sends.
    body = recording.requests[0]["json"]
    assert body["message"] == "hi"
    assert body["thread_id"] == "t"
    assert body["user_id"] == "u"
    # Implicit: no surprising extras.
    extra = set(body) - {"message", "thread_id", "user_id"}
    assert extra == set(), f"unexpected fields: {extra}"
