"""Shared SSE event emitter for autonomous agent turns.

Spawned threads (``tools/spawn_thread.py``) and background-bash completion
turns (``tools/bash_background.py``) both run a prompt as an autonomous agent
turn and publish the same ``task_started`` / streamed-chunk / ``task_completed``
SSE lifecycle. Historically each reimplemented that lifecycle inline, so a new
terminal-event field (or a fix to the publish-once latch) had to be made twice
and could drift.

``AutonomousTurnEmitter`` owns exactly the drift-prone shared surface: the
``task_started`` / ``task_completed`` event shape, the
``publish-task_started-once`` latch, and the per-chunk forwarding (with the
meta-event gating that decides which chunks may fire ``task_started``). Each
caller keeps its own turn orchestration inline, because it genuinely differs:
the stream call, completion-data shaping, iteration-limit handling, activity
logging, error-publish guarding, and return value are all caller-specific.

The event-bus publishers are imported function-locally inside the methods so
tests that monkeypatch them on their source module
(``nymeria.core.event_bus``) keep working, matching the existing convention in
both tool modules.
"""

from __future__ import annotations

import logging
from typing import Any, Collection, Mapping

from .pending_prompt_queue import PENDING_QUEUE_META_EVENT_TYPES

logger = logging.getLogger(__name__)


class AutonomousTurnEmitter:
    """Publish the task_started / chunk / task_completed events for one turn.

    Construct one per autonomous turn, bound to its ``(thread_id, user_id,
    task_id)`` and the ``task_started`` payload. Pass :meth:`handle_chunk` as
    the ``on_chunk`` callback to ``stream_and_collect``; call
    :meth:`publish_completed` with the caller's terminal payload on success and
    error. :meth:`publish_started` is idempotent (the once-latch), so callers
    that want to guarantee a ``task_started`` precedes ``task_completed`` (even
    when the stream produced no eligible chunk) may call it directly.
    """

    def __init__(
        self,
        *,
        thread_id: str,
        user_id: str,
        task_id: str,
        started_data: Mapping[str, Any],
        meta_event_types: Collection[str] = PENDING_QUEUE_META_EVENT_TYPES,
    ) -> None:
        self._thread_id = thread_id
        self._user_id = user_id
        self._task_id = task_id
        self._started_data = dict(started_data)
        self._meta_event_types = frozenset(meta_event_types)
        self._started_published = False

    def publish_started(self) -> None:
        """Publish ``task_started`` once; subsequent calls are no-ops."""
        if self._started_published:
            return
        from .event_bus import publish_autonomous_event

        publish_autonomous_event(
            event_type="task_started",
            thread_id=self._thread_id,
            user_id=self._user_id,
            task_id=self._task_id,
            data=dict(self._started_data),
        )
        self._started_published = True

    def handle_chunk(self, chunk: dict[str, Any], _collection: Any = None) -> None:
        """Fire ``task_started`` (gated by meta-event type) then forward a chunk.

        Suitable as the ``on_chunk`` callback for
        ``stream_bridge.stream_and_collect``. Queue-meta events (queue state
        transitions, not real model work) do not trigger ``task_started`` but
        are still forwarded to the frontend.
        """
        from .event_bus import publish_agent_stream_chunk

        if chunk.get("type") not in self._meta_event_types:
            self.publish_started()
        publish_agent_stream_chunk(
            chunk,
            thread_id=self._thread_id,
            user_id=self._user_id,
            task_id=self._task_id,
        )

    def publish_completed(self, data: Mapping[str, Any]) -> None:
        """Publish ``task_completed`` with the caller's terminal payload."""
        from .event_bus import publish_autonomous_event

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=self._thread_id,
            user_id=self._user_id,
            task_id=self._task_id,
            data=dict(data),
        )


__all__ = ["AutonomousTurnEmitter"]
