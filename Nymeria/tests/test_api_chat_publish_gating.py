"""Verify that ``publish_autonomous_events=False`` suppresses API-side mirroring.

The Docker worker relays its TODO and trigger turns to the API but
publishes ``task_started`` / agent stream chunks / ``task_completed``
itself with stable task IDs (``todo.id``, ``trigger-<id>``). The API
must not double-publish for those calls. This test pins that contract
directly at the chat-router layer so a regression here can't slip past
the gating.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nymeria.api.routers.chat import create_chat_router


class _PublishRecorder:
    def __init__(self) -> None:
        self.autonomous_events: list[tuple[str, dict[str, Any]]] = []
        self.stream_chunks: list[dict[str, Any]] = []
        self.sync_events: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []

    def publish_sync_event(self, **kwargs: Any) -> None:
        self.sync_events.append(kwargs)

    def publish_agent_stream_chunk(self, chunk: dict[str, Any], **kwargs: Any) -> None:
        self.stream_chunks.append({**chunk, **kwargs})

    def publish_autonomous_event(
        self,
        *,
        event_type: str,
        thread_id: str,
        user_id: str,
        task_id: str = "",
        data: dict[str, Any] | None = None,
    ) -> None:
        self.autonomous_events.append(
            (event_type, {"thread_id": thread_id, "task_id": task_id, **(data or {})})
        )

    def create_autonomous_notification(self, **kwargs: Any) -> None:
        self.notifications.append(kwargs)

    def should_notify_autonomous(self, thread_id: str, _config_mgr: Any) -> bool:
        return True


class _FakeAgent:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(llm_model="m")
        self.thread_metadata_manager = SimpleNamespace(
            auto_title=lambda *_args, **_kwargs: None
        )
        self.thread_config_manager = object()
        self.accounts_repo = object()

    async def astream(self, message: str, **_kwargs: Any):
        yield {"type": "response", "content": "ok"}

    def get_context_stats(self, _thread_id: str) -> dict[str, Any]:
        return {}

    def _get_llm_config_for_thread(self, _thread_id: str):
        return SimpleNamespace(model=None)


class _AuthUser:
    """Stand-in for AuthenticatedUser; only ``id`` is read."""

    def __init__(self, user_id: str = "alice") -> None:
        self.id = user_id


def _build_client(
    recorder: _PublishRecorder, settings: Any | None = None
) -> TestClient:
    agent = _FakeAgent()

    def verify_api_key() -> _AuthUser:
        return _AuthUser()

    def require_thread_access(_user: _AuthUser, _thread_id: str) -> None:
        return None

    router = create_chat_router(
        verify_api_key=verify_api_key,
        get_agent_fn=lambda: agent,
        get_settings_fn=lambda: settings if settings is not None else SimpleNamespace(),
        require_thread_access_fn=require_thread_access,
        publish_sync_event_fn=recorder.publish_sync_event,
        publish_agent_stream_chunk_fn=recorder.publish_agent_stream_chunk,
        publish_autonomous_event_fn=recorder.publish_autonomous_event,
        create_autonomous_notification_fn=recorder.create_autonomous_notification,
        should_notify_autonomous_fn=recorder.should_notify_autonomous,
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _sse_events(body: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in body.splitlines()
        if line.startswith("data: ")
    ]


def test_default_self_invoke_publishes_autonomous_bookends_and_chunks():
    recorder = _PublishRecorder()
    client = _build_client(recorder)

    with client.stream(
        "POST",
        "/chat",
        json={
            "message": "watchdog nudge",
            "thread_id": "thread-a",
            "is_self_invoke": True,
            "trigger_override": "watchdog",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    events = _sse_events(body)
    assert any(evt.get("type") == "response" for evt in events)

    event_types = [evt[0] for evt in recorder.autonomous_events]
    assert "task_started" in event_types
    assert "task_completed" in event_types

    assert recorder.stream_chunks, "expected API to mirror chunks to autonomous bus"
    assert recorder.notifications, "expected autonomous notification to fire"


def test_publish_autonomous_events_false_suppresses_all_mirroring():
    recorder = _PublishRecorder()
    client = _build_client(recorder)

    with client.stream(
        "POST",
        "/chat",
        json={
            "message": "TODO nudge",
            "thread_id": "thread-b",
            "is_self_invoke": True,
            "trigger_override": "ticker",
            "publish_autonomous_events": False,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    events = _sse_events(body)
    response_chunks = [evt for evt in events if evt.get("type") == "response"]
    assert response_chunks, (
        "HTTP stream must still deliver chunks back to the worker "
        "even when API-side mirroring is suppressed"
    )

    assert recorder.autonomous_events == [], (
        "API must not publish autonomous bookends when "
        "publish_autonomous_events=False"
    )
    assert recorder.stream_chunks == [], (
        "API must not mirror agent stream chunks when "
        "publish_autonomous_events=False"
    )
    assert recorder.notifications == [], (
        "API must not create an autonomous notification when "
        "publish_autonomous_events=False"
    )


def test_non_self_invoke_never_publishes_autonomous_events():
    """Sanity check: a normal user chat still doesn't touch the autonomous bus."""
    recorder = _PublishRecorder()
    client = _build_client(recorder)

    with client.stream(
        "POST",
        "/chat",
        json={
            "message": "hi",
            "thread_id": "thread-c",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    _sse_events(body)
    assert recorder.autonomous_events == []
    assert recorder.stream_chunks == []
    assert recorder.notifications == []


def test_capacity_shed_publishes_no_message_added():
    """A 429 shed happens BEFORE the message_added publish (backlog #83).

    Frontends echo user bubbles from the ``message_added`` sync event; a
    shed turn was never started, so no ghost bubble may be broadcast.
    """
    from nymeria.core.interactive_admission import (
        CAPACITY_DETAIL,
        get_interactive_turn_gate,
        reset_interactive_turn_gate_for_tests,
    )

    reset_interactive_turn_gate_for_tests()
    try:
        recorder = _PublishRecorder()
        client = _build_client(
            recorder,
            settings=SimpleNamespace(
                max_concurrent_interactive=1,
                interactive_admission_wait_seconds=0,
            ),
        )
        held = get_interactive_turn_gate().try_acquire(1)
        assert held is not None

        response = client.post(
            "/chat",
            json={"message": "hi", "thread_id": "thread-shed"},
        )

        assert response.status_code == 429
        assert response.json() == {"detail": CAPACITY_DETAIL}
        assert recorder.sync_events == []
        assert recorder.autonomous_events == []
        held.release()
    finally:
        reset_interactive_turn_gate_for_tests()
