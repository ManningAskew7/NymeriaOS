"""CLI transport turn recovery (re-attachable turns, slice 4).

Pins APIAgentClient.stream_chat's re-attach behavior: a dropped POST /chat
connection resumes from the last seen seq via GET /threads/{id}/turn/stream
(suffix-only replay, no duplicated output), survives repeated drops, and
degrades to an honest ``turn_lost`` error event when the turn is gone.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from cli_fixtures import run
from nymeria.triggers.cli.events import DoneEvent, ErrorEvent, ResponseEvent
from nymeria.triggers.cli.transport import api as transport_api
from nymeria.triggers.cli.transport.api import APIAgentClient


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://api/threads/t/turn/stream")
    return httpx.HTTPStatusError(
        f"HTTP {status}",
        request=request,
        response=httpx.Response(status, request=request),
    )


class RecoveryFakeAPI:
    """Scripted NymeriaAPIClient stand-in for the recovery paths."""

    def __init__(self) -> None:
        self.base_url = "http://api"
        self.api_key = "secret"
        # Items are event dicts, or an Exception to raise at that point.
        self.chat_script: list[Any] = []
        # One dict per get_thread_status call; the last repeats.
        self.status_script: list[dict[str, Any]] = []
        # One script per reattach call, same item convention as chat_script.
        self.reattach_scripts: list[list[Any]] = []
        self.reattach_calls: list[dict[str, Any]] = []
        self.status_calls = 0

    async def chat_stream(self, **_kwargs: Any):
        for item in self.chat_script:
            if isinstance(item, Exception):
                raise item
            yield item

    async def get_thread_status(
        self, thread_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        self.status_calls += 1
        if len(self.status_script) > 1:
            return self.status_script.pop(0)
        return self.status_script[0]

    async def reattach_turn_stream(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        turn_id: str | None = None,
        from_seq: int = 0,
    ):
        self.reattach_calls.append({"turn_id": turn_id, "from_seq": from_seq})
        script = self.reattach_scripts.pop(0)
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield item


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(transport_api, "RECOVERY_BASE_DELAY_SECONDS", 0.001)
    monkeypatch.setattr(transport_api, "RECOVERY_MAX_DELAY_SECONDS", 0.002)


def _collect(client: APIAgentClient) -> list[Any]:
    async def _run() -> list[Any]:
        return [
            event
            async for event in client.stream_chat("hi", "t-1", user_id="default")
        ]

    return run(_run())


def _live_status(turn_id: str = "turn-1") -> dict[str, Any]:
    return {
        "thread_id": "t-1",
        "processing": True,
        "turn": {"turn_id": turn_id, "state": "live", "last_seq": 2, "truncated": False},
    }


def test_drop_reattaches_from_last_seq_and_finishes() -> None:
    fake = RecoveryFakeAPI()
    fake.chat_script = [
        {"type": "turn_started", "turn_id": "turn-1", "seq": 1, "thread_id": "t-1"},
        {"type": "response", "content": "Hello ", "seq": 2, "thread_id": "t-1"},
        httpx.ReadError("connection dropped"),
    ]
    fake.status_script = [_live_status()]
    fake.reattach_scripts = [
        [
            {"type": "turn_attach", "turn_id": "turn-1", "state": "live", "last_seq": 3},
            {"type": "response", "content": "world", "seq": 3, "thread_id": "t-1"},
            {"type": "done", "seq": 4, "thread_id": "t-1"},
        ]
    ]
    client = APIAgentClient(fake)  # type: ignore[arg-type]

    events = _collect(client)

    assert [type(e) for e in events] == [ResponseEvent, ResponseEvent, DoneEvent]
    assert [e.content for e in events[:2]] == ["Hello ", "world"]
    assert fake.reattach_calls == [{"turn_id": "turn-1", "from_seq": 2}]


def test_second_drop_resumes_with_advanced_cursor() -> None:
    fake = RecoveryFakeAPI()
    fake.chat_script = [
        {"type": "turn_started", "turn_id": "turn-1", "seq": 1, "thread_id": "t-1"},
        {"type": "response", "content": "a", "seq": 2, "thread_id": "t-1"},
        httpx.ReadError("drop one"),
    ]
    fake.status_script = [_live_status()]
    fake.reattach_scripts = [
        [
            {"type": "turn_attach", "turn_id": "turn-1", "state": "live", "last_seq": 3},
            {"type": "response", "content": "b", "seq": 3, "thread_id": "t-1"},
            httpx.ReadError("drop two"),
        ],
        [
            {"type": "turn_attach", "turn_id": "turn-1", "state": "live", "last_seq": 4},
            {"type": "response", "content": "c", "seq": 4, "thread_id": "t-1"},
            {"type": "done", "seq": 5, "thread_id": "t-1"},
        ],
    ]
    client = APIAgentClient(fake)  # type: ignore[arg-type]

    events = _collect(client)

    contents = [e.content for e in events if isinstance(e, ResponseEvent)]
    assert contents == ["a", "b", "c"]
    assert isinstance(events[-1], DoneEvent)
    assert [c["from_seq"] for c in fake.reattach_calls] == [2, 3]


def test_turn_gone_yields_honest_turn_lost_error() -> None:
    fake = RecoveryFakeAPI()
    fake.chat_script = [
        {"type": "turn_started", "turn_id": "turn-1", "seq": 1, "thread_id": "t-1"},
        {"type": "response", "content": "partial", "seq": 2, "thread_id": "t-1"},
        httpx.ConnectError("backend restarted"),
    ]
    # After the restart the thread is idle with no attachable turn.
    fake.status_script = [{"thread_id": "t-1", "processing": False, "turn": None}]
    client = APIAgentClient(fake)  # type: ignore[arg-type]

    events = _collect(client)

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == "turn_lost"
    assert fake.reattach_calls == []


def test_mid_stream_replay_gap_yields_turn_lost() -> None:
    """A turn_replay_gap frame mid-replay ends recovery honestly, unrendered."""
    fake = RecoveryFakeAPI()
    fake.chat_script = [
        {"type": "turn_started", "turn_id": "turn-1", "seq": 1, "thread_id": "t-1"},
        {"type": "response", "content": "partial", "seq": 2, "thread_id": "t-1"},
        httpx.ReadError("dropped"),
    ]
    fake.status_script = [_live_status()]
    fake.reattach_scripts = [
        [
            {"type": "turn_attach", "turn_id": "turn-1", "state": "live", "last_seq": 9},
            {"type": "turn_replay_gap", "thread_id": "t-1", "turn_id": "turn-1"},
        ]
    ]
    client = APIAgentClient(fake)  # type: ignore[arg-type]

    events = _collect(client)

    assert [type(e) for e in events] == [ResponseEvent, ErrorEvent]
    assert events[-1].code == "turn_lost"


def test_reattach_404_yields_turn_lost_error() -> None:
    fake = RecoveryFakeAPI()
    fake.chat_script = [
        {"type": "turn_started", "turn_id": "turn-1", "seq": 1, "thread_id": "t-1"},
        httpx.ReadError("dropped"),
    ]
    fake.status_script = [_live_status()]
    fake.reattach_scripts = [[_http_error(404)]]
    client = APIAgentClient(fake)  # type: ignore[arg-type]

    events = _collect(client)

    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == "turn_lost"


def test_turn_started_marker_is_not_rendered() -> None:
    fake = RecoveryFakeAPI()
    fake.chat_script = [
        {"type": "turn_started", "turn_id": "turn-1", "seq": 1, "thread_id": "t-1"},
        {"type": "response", "content": "hi", "seq": 2, "thread_id": "t-1"},
        {"type": "done", "seq": 3, "thread_id": "t-1"},
    ]
    client = APIAgentClient(fake)  # type: ignore[arg-type]

    events = _collect(client)

    assert [type(e) for e in events] == [ResponseEvent, DoneEvent]


def test_http_status_errors_keep_plain_error_path() -> None:
    fake = RecoveryFakeAPI()
    fake.chat_script = [_http_error(500)]
    client = APIAgentClient(fake)  # type: ignore[arg-type]

    events = _collect(client)

    assert len(events) == 1
    assert isinstance(events[0], ErrorEvent)
    assert events[0].code == "api_transport_error"
    assert fake.status_calls == 0
