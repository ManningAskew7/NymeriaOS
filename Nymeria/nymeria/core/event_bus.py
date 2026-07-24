"""Simple in-memory event bus for streaming autonomous outputs to frontend."""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, TYPE_CHECKING
from queue import Queue, Full
from urllib.parse import urlsplit, urlunsplit

from .time_utils import utc_now

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

HIGH_VOLUME_EVENT_TYPES = frozenset({"response", "thinking", "tool_call_delta"})


def should_log_stream_event_sample(event_type: str, count: int) -> bool:
    """Return True for first events and sampled high-volume stream chunks."""
    if count <= 3:
        return True
    if event_type in HIGH_VOLUME_EVENT_TYPES:
        return count % 100 == 0
    return count % 10 == 0


def redact_url_credentials(url: str) -> str:
    """Return a URL with any username/password credentials redacted for logs."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url

    if parsed.username is None and parsed.password is None:
        return url

    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return urlunsplit((parsed.scheme, "***@", parsed.path, parsed.query, parsed.fragment))

    username = parsed.username or ""
    userinfo = f"{username}:***@" if username else ":***@"
    netloc = f"{userinfo}{host}{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


@dataclass
class AutonomousEvent:
    """Event for autonomous task output and cross-client sync."""

    event_type: str  # Autonomous: "task_started", "thinking", "tool_call", "tool_result", "response", "task_completed"
                     # Cross-client sync: "message_added", "thread_updated", "thread_created", "thread_deleted"
    thread_id: str
    user_id: str
    task_id: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=utc_now)


class EventBus:
    """
    Simple event bus for streaming autonomous outputs.

    Supports multiple subscribers (one per connected frontend client).
    Events are published by the ticker and consumed by SSE endpoints.
    """

    def __init__(self):
        self._subscribers: Dict[str, Queue] = {}
        self._lock = threading.Lock()
        self._publish_counts: Dict[str, int] = {}
        self._enqueue_counts: Dict[str, int] = {}
        self._drop_counts: Dict[str, int] = {}
        logger.info("EventBus initialized")

    def _bump_counter(self, counter: Dict[str, int], key: str) -> int:
        counter[key] = counter.get(key, 0) + 1
        return counter[key]

    def _prune_counter_keys(self, prefix: str) -> None:
        """Drop per-subscriber diagnostic counter keys with ``prefix``.

        The enqueue/drop counters are keyed ``f"{sub_id}:{event_type}"`` (and,
        in the Redis subclass, ``f"redis:{sub_id}:{event_type}"``), so without
        pruning on unsubscribe they would grow without bound over the lifetime
        of a long-running process as short-lived SSE subscribers come and go.
        Caller must hold ``self._lock``. The publish counters are keyed by
        event type only (bounded), so they are left alone.
        """
        for counter in (self._enqueue_counts, self._drop_counts):
            for key in [k for k in counter if k.startswith(prefix)]:
                del counter[key]

    def subscribe(self, subscriber_id: str) -> Queue:
        """
        Subscribe to autonomous events.

        Args:
            subscriber_id: Unique ID for this subscriber

        Returns:
            Queue that will receive events
        """
        with self._lock:
            if subscriber_id in self._subscribers:
                # Already subscribed, return existing queue
                return self._subscribers[subscriber_id]

            queue: Queue = Queue(maxsize=100)
            self._subscribers[subscriber_id] = queue
            logger.info(f"[EVENT BUS] Subscriber connected: {subscriber_id[:8]}..., total: {len(self._subscribers)}")
            return queue

    def unsubscribe(self, subscriber_id: str) -> None:
        """
        Unsubscribe from autonomous events.

        Args:
            subscriber_id: ID of subscriber to remove
        """
        with self._lock:
            if subscriber_id in self._subscribers:
                del self._subscribers[subscriber_id]
                logger.info(f"[EVENT BUS] Subscriber disconnected: {subscriber_id[:8]}..., total: {len(self._subscribers)}")
            # Always prune this subscriber's diagnostic counters, even if the
            # queue was already gone, so the counter dicts can't leak.
            self._prune_counter_keys(f"{subscriber_id}:")

    def publish(self, event: AutonomousEvent) -> None:
        """
        Publish an event to all subscribers.

        Args:
            event: The event to publish
        """
        with self._lock:
            subscriber_count = len(self._subscribers)
            publish_count = self._bump_counter(self._publish_counts, event.event_type)
            if subscriber_count == 0:
                # Log when important events are dropped due to no subscribers
                if should_log_stream_event_sample(event.event_type, publish_count):
                    logger.warning(
                        "[EVENT BUS] publish_drop_no_subscribers type=%s count=%d "
                        "thread=%s user=%s task=%s. Frontend may not be connected "
                        "to /autonomous/stream",
                        event.event_type,
                        publish_count,
                        event.thread_id,
                        event.user_id,
                        event.task_id,
                    )
                return

            if should_log_stream_event_sample(event.event_type, publish_count):
                logger.info(
                    "[EVENT BUS] publish type=%s count=%d local_subscribers=%d "
                    "thread=%s user=%s task=%s",
                    event.event_type,
                    publish_count,
                    subscriber_count,
                    event.thread_id,
                    event.user_id,
                    event.task_id,
                )

            for sub_id, queue in list(self._subscribers.items()):
                try:
                    # Non-blocking put, drop if queue is full
                    queue.put_nowait(event)
                    enqueue_key = f"{sub_id}:{event.event_type}"
                    enqueue_count = self._bump_counter(self._enqueue_counts, enqueue_key)
                    if should_log_stream_event_sample(event.event_type, enqueue_count):
                        logger.info(
                            "[EVENT BUS] queue_enqueue subscriber=%s type=%s count=%d "
                            "queue_size=%d thread=%s task=%s",
                            sub_id[:8],
                            event.event_type,
                            enqueue_count,
                            queue.qsize(),
                            event.thread_id,
                            event.task_id,
                        )
                except Full:
                    # Queue full, skip this subscriber
                    drop_key = f"{sub_id}:{event.event_type}"
                    drop_count = self._bump_counter(self._drop_counts, drop_key)
                    logger.warning(
                        "[EVENT BUS] queue_drop_full subscriber=%s type=%s drop_count=%d "
                        "thread=%s task=%s",
                        sub_id[:8],
                        event.event_type,
                        drop_count,
                        event.thread_id,
                        event.task_id,
                    )

    def get_subscriber_count(self) -> int:
        """Get the number of active subscribers."""
        with self._lock:
            return len(self._subscribers)


# Global event bus instance
_event_bus: Optional[EventBus] = None


def create_event_bus(
    settings: "Settings",
    *,
    enable_subscriber: bool = True,
) -> EventBus:
    """
    Create an event bus based on settings.

    Args:
        settings: Application settings
        enable_subscriber: Only meaningful when the Redis bus is selected.
            Pass False from publisher-only processes (e.g. the Docker
            worker) so they do not spin up a no-op pub/sub subscriber
            thread. Defaults to True for the API and other processes
            that consume cross-process events.

    Returns:
        RedisEventBus if Redis is enabled and configured, otherwise EventBus
    """
    if settings.redis_enabled and settings.redis_url:
        from .event_bus_redis import RedisEventBus
        logger.info(
            "Creating Redis event bus with URL: %s (subscriber=%s)",
            redact_url_credentials(settings.redis_url),
            enable_subscriber,
        )
        return RedisEventBus(
            settings.redis_url,
            enable_subscriber=enable_subscriber,
        )
    else:
        logger.info("Creating in-memory event bus")
        return EventBus()


def get_event_bus() -> EventBus:
    """Get or create the global event bus."""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def set_event_bus(bus: EventBus) -> None:
    """
    Set the global event bus instance.

    Args:
        bus: The event bus to use globally
    """
    global _event_bus
    _event_bus = bus
    logger.info(f"Global event bus set to {type(bus).__name__}")


def autonomous_event_to_payload(event: AutonomousEvent) -> Dict[str, Any]:
    """Flatten an autonomous event into the canonical wire dict.

    Shared by the API SSE router (which JSON-encodes the result) and the CLI's
    in-process transport (which consumes the dict directly), so the autonomous
    wire shape cannot drift between the two paths. ``event.event_type`` is
    canonical, so interactive sync events keep their ``interactive_`` prefix if
    present. Internal ``_``-prefixed keys and the reserved top-level keys are
    stripped from ``event.data``.
    """
    payload = {
        k: v
        for k, v in event.data.items()
        if not k.startswith("_") and k not in ("type", "thread_id", "task_id", "timestamp")
    }
    return {
        "type": event.event_type,
        "thread_id": event.thread_id,
        "task_id": event.task_id,
        "timestamp": event.timestamp.isoformat(),
        **payload,
    }


def publish_autonomous_event(
    event_type: str,
    thread_id: str,
    user_id: str,
    task_id: str,
    data: Dict[str, Any],
) -> None:
    """
    Convenience function to publish an autonomous event.

    Args:
        event_type: Type of event
        thread_id: Thread ID
        user_id: User ID
        task_id: Task ID
        data: Event data
    """
    event = AutonomousEvent(
        event_type=event_type,
        thread_id=thread_id,
        user_id=user_id,
        task_id=task_id,
        data=data,
    )
    get_event_bus().publish(event)


AGENT_STREAM_AUTONOMOUS_EVENT_TYPES = frozenset({
    "tool_call_delta",
    "tool_call",
    "tool_result",
    "thinking",
    "response",
    "workspace_artifact",
    "compacting",
    "compacted",
    "context_attached",
    "iteration_limit",
    # A /resume of a graceful iteration-cap halt started re-driving the
    # halted turn; mirrored so other clients watching the thread can flip
    # their pause card to its resumed state.
    "turn_resumed",
    "tool_reload",
    # The react tool asked for reply suppression; mirrored so bot autonomous
    # handlers drop the reply text exactly like interactive consumers do.
    "reply_suppressed",
    "provider_retry",
    "provider_fallback",
    # A model rejected image/PDF input; the backend stripped the attachment and
    # retried once. Mirrored so autonomous/relayed turns surface the drop too.
    "image_input_unsupported",
    # A turn hit its output cap. Mirrored because the autonomous turns are
    # exactly the ones nobody is watching: a briefing that dies inside its
    # thinking block delivers silence, and dropping this event here is what
    # made that silence a mystery rather than a report.
    "output_truncated",
    # The provider ended a response with a refusal (Anthropic
    # stop_reason="refusal" / OpenAI finish_reason="content_filter"). Same
    # fatal silent shape as output_truncated when it fires before any text
    # or tool call, so it is mirrored for the same reason.
    "response_refused",
    "auth_prompt",
    "auth_prompt_resolved",
    "auth_prompt_cancelled",
    "browser_command",
    "browser_command_result",
    "ui_prompt",
    "ui_prompt_result",
    # Sub-turn prompt-queue events. Mirroring these onto the
    # autonomous bus lets ``/autonomous/stream`` subscribers see when a
    # busy thread halted to absorb a queued prompt, even if they
    # weren't the queuer.
    "prompt_queued",
    "prompt_injected",
    "prompt_absorbed",
    "turn_halted",
    "fanout_dropped",
})


def agent_stream_chunk_to_autonomous_event_data(
    chunk: Dict[str, Any],
) -> Optional[tuple[str, Dict[str, Any]]]:
    """Convert an agent stream chunk into an autonomous event payload."""
    chunk_type = chunk.get("type")
    if not isinstance(chunk_type, str):
        return None
    if chunk_type not in AGENT_STREAM_AUTONOMOUS_EVENT_TYPES:
        return None
    return chunk_type, {k: v for k, v in chunk.items() if k != "type"}


def publish_agent_stream_chunk(
    chunk: Dict[str, Any],
    *,
    thread_id: str,
    user_id: str,
    task_id: str,
) -> bool:
    """Forward a supported agent stream chunk to ``/autonomous/stream``."""
    converted = agent_stream_chunk_to_autonomous_event_data(chunk)
    if converted is None:
        return False
    event_type, data = converted
    publish_autonomous_event(
        event_type=event_type,
        thread_id=thread_id,
        user_id=user_id,
        task_id=task_id,
        data=data,
    )
    return True


def publish_sync_event(
    event_type: str,
    thread_id: str,
    user_id: str,
    data: Dict[str, Any],
    origin_client_id: str = "",
) -> None:
    """
    Publish a sync event for cross-client state synchronization.

    Used for interactive chat events, thread metadata changes, etc.
    The origin_client_id is included so the autonomous SSE generator
    can skip events that originated from the same client.

    Args:
        event_type: Type of sync event (e.g. "interactive_response", "thread_updated")
        thread_id: Thread ID
        user_id: User ID
        data: Event data payload
        origin_client_id: Client ID of the originating frontend (for dedup)
    """
    if origin_client_id:
        data = {**data, "_origin_client_id": origin_client_id}
    event = AutonomousEvent(
        event_type=event_type,
        thread_id=thread_id,
        user_id=user_id,
        data=data,
    )
    get_event_bus().publish(event)
