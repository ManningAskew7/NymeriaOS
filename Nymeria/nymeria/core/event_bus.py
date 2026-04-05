"""Simple in-memory event bus for streaming autonomous outputs to frontend."""

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncGenerator, Dict, List, Optional, TYPE_CHECKING
from queue import Queue, Empty

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)


@dataclass
class AutonomousEvent:
    """Event for autonomous task output and cross-client sync."""

    event_type: str  # Autonomous: "task_started", "thinking", "tool_call", "tool_result", "response", "task_completed"
                     # Interactive sync: "interactive_thinking", "interactive_tool_call", "interactive_tool_result",
                     #   "interactive_response", "interactive_done", "message_added"
                     # Metadata sync: "thread_updated", "thread_created", "thread_deleted"
    thread_id: str
    user_id: str
    task_id: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)


class EventBus:
    """
    Simple event bus for streaming autonomous outputs.

    Supports multiple subscribers (one per connected frontend client).
    Events are published by the ticker and consumed by SSE endpoints.
    """

    def __init__(self):
        self._subscribers: Dict[str, Queue] = {}
        self._lock = threading.Lock()
        logger.info("EventBus initialized")

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

    def publish(self, event: AutonomousEvent) -> None:
        """
        Publish an event to all subscribers.

        Args:
            event: The event to publish
        """
        with self._lock:
            subscriber_count = len(self._subscribers)
            if subscriber_count == 0:
                # Log when important events are dropped due to no subscribers
                if event.event_type in ("task_started", "task_completed", "response",
                                        "interactive_done", "thread_updated", "thread_deleted"):
                    logger.warning(
                        f"[EVENT BUS] No subscribers! Dropping {event.event_type} event "
                        f"(thread={event.thread_id}). "
                        f"Frontend may not be connected to /autonomous/stream"
                    )
                return

            for sub_id, queue in list(self._subscribers.items()):
                try:
                    # Non-blocking put, drop if queue is full
                    queue.put_nowait(event)
                except Exception:
                    # Queue full, skip this subscriber
                    logger.warning(f"Queue full for subscriber {sub_id}, dropping event")

        logger.info(f"[EVENT BUS] Published {event.event_type} to {subscriber_count} subscriber(s) (thread={event.thread_id})")

    def get_subscriber_count(self) -> int:
        """Get the number of active subscribers."""
        with self._lock:
            return len(self._subscribers)


# Global event bus instance
_event_bus: Optional[EventBus] = None


def create_event_bus(settings: "Settings") -> EventBus:
    """
    Create an event bus based on settings.

    Args:
        settings: Application settings

    Returns:
        RedisEventBus if Redis is enabled and configured, otherwise EventBus
    """
    if settings.redis_enabled and settings.redis_url:
        from .event_bus_redis import RedisEventBus
        logger.info(f"Creating Redis event bus with URL: {settings.redis_url}")
        return RedisEventBus(settings.redis_url)
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
