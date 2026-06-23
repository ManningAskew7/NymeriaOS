"""Unit tests for the shared autonomous-turn SSE emitter.

Covers the drift-prone surface extracted from ``tools/spawn_thread`` and
``tools/bash_background`` (slice 18 F3): the publish-task_started-once latch, the
meta-event gating in ``handle_chunk``, and the ``task_completed`` shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from nymeria.core.autonomous_turn import AutonomousTurnEmitter


@pytest.fixture
def captured(monkeypatch):
    """Capture event-bus publishes the emitter makes via its source module."""
    events: list[tuple[str, str, str, str, dict]] = []
    chunks: list[tuple[dict, str, str, str]] = []

    def fake_publish_autonomous_event(
        event_type: str,
        thread_id: str,
        user_id: str,
        task_id: str,
        data: dict,
    ) -> None:
        events.append((event_type, thread_id, user_id, task_id, data))

    def fake_publish_agent_stream_chunk(
        chunk: dict,
        *,
        thread_id: str,
        user_id: str,
        task_id: str,
    ) -> bool:
        chunks.append((chunk, thread_id, user_id, task_id))
        return True

    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_autonomous_event",
        fake_publish_autonomous_event,
    )
    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_agent_stream_chunk",
        fake_publish_agent_stream_chunk,
    )
    return events, chunks


def _emitter(**overrides: Any) -> AutonomousTurnEmitter:
    kwargs: dict[str, Any] = dict(
        thread_id="thread-1",
        user_id="user-1",
        task_id="task-1",
        started_data={"prompt": "hi"},
    )
    kwargs.update(overrides)
    return AutonomousTurnEmitter(**kwargs)


def test_publish_started_fires_once(captured):
    events, _ = captured
    emitter = _emitter()

    emitter.publish_started()
    emitter.publish_started()

    assert len(events) == 1  # idempotent: the second call is a no-op
    event_type, thread_id, user_id, task_id, data = events[0]
    assert event_type == "task_started"
    assert (thread_id, user_id, task_id) == ("thread-1", "user-1", "task-1")
    assert data == {"prompt": "hi"}


def test_handle_chunk_non_meta_fires_started_then_forwards(captured):
    events, chunks = captured
    emitter = _emitter()

    chunk = {"type": "response", "content": "ack"}
    emitter.handle_chunk(chunk, None)

    assert [e[0] for e in events] == ["task_started"]
    assert chunks == [(chunk, "thread-1", "user-1", "task-1")]


def test_handle_chunk_meta_event_suppresses_started_but_forwards(captured):
    events, chunks = captured
    emitter = _emitter(meta_event_types=("queued", "prompt_queued"))

    emitter.handle_chunk({"type": "prompt_queued"}, None)

    assert events == []  # task_started suppressed for meta events
    assert len(chunks) == 1  # ...but the chunk is still forwarded
    assert chunks[0][0] == {"type": "prompt_queued"}


def test_handle_chunk_publishes_started_only_once_across_chunks(captured):
    events, chunks = captured
    emitter = _emitter()

    emitter.handle_chunk({"type": "thinking", "content": "..."}, None)
    emitter.handle_chunk({"type": "response", "content": "done"}, None)

    assert [e[0] for e in events] == ["task_started"]
    assert len(chunks) == 2


def test_default_meta_event_types_match_pending_queue_set(captured):
    """Without an explicit set, queue-meta events suppress task_started."""
    from nymeria.core.pending_prompt_queue import PENDING_QUEUE_META_EVENT_TYPES

    events, chunks = captured
    emitter = _emitter()  # uses the default meta set

    for meta_type in PENDING_QUEUE_META_EVENT_TYPES:
        emitter.handle_chunk({"type": meta_type}, None)

    assert events == []
    assert len(chunks) == len(PENDING_QUEUE_META_EVENT_TYPES)


def test_publish_completed_shape(captured):
    events, _ = captured
    emitter = _emitter()

    emitter.publish_completed({"content": "result", "extra": 1})

    assert len(events) == 1
    event_type, thread_id, user_id, task_id, data = events[0]
    assert event_type == "task_completed"
    assert (thread_id, user_id, task_id) == ("thread-1", "user-1", "task-1")
    assert data == {"content": "result", "extra": 1}


def test_started_data_is_copied_defensively(captured):
    events, _ = captured
    started = {"prompt": "hi"}
    emitter = _emitter(started_data=started)

    started["prompt"] = "mutated"  # must not affect the published payload
    emitter.publish_started()

    assert events[0][4] == {"prompt": "hi"}
