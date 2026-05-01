"""Redis-backed event bus for cross-container communication."""

import asyncio
import json
import logging
import threading
from dataclasses import asdict
from datetime import datetime
from queue import Full, Queue, Empty
from typing import Any, Dict, Optional

from .event_bus import AutonomousEvent, EventBus, should_log_stream_event_sample

logger = logging.getLogger(__name__)


class RedisEventBus(EventBus):
    """
    Redis-backed event bus for distributed deployments.

    Uses Redis pub/sub for real-time cross-container communication.
    Falls back to in-memory behavior if Redis connection fails.
    """

    CHANNEL_NAME = "nymeria:autonomous_events"

    def __init__(self, redis_url: str):
        """
        Initialize Redis event bus.

        Args:
            redis_url: Redis connection URL (e.g., redis://localhost:6379)
        """
        super().__init__()
        self.redis_url = redis_url
        self._redis_client: Optional[Any] = None
        self._pubsub: Optional[Any] = None
        self._subscriber_thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._redis_publish_counts: Dict[str, int] = {}
        self._redis_receive_counts: Dict[str, int] = {}

        # Try to connect to Redis
        self._connect()

    def _connect(self) -> bool:
        """
        Connect to Redis.

        Returns:
            True if connection successful, False otherwise
        """
        try:
            import redis
            self._redis_client = redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
            # Test connection
            self._redis_client.ping()
            self._connected = True
            logger.info(f"[REDIS EVENT BUS] Connected to Redis at {self.redis_url}")

            # Start subscriber thread
            self._start_subscriber()
            return True

        except ImportError:
            logger.warning(
                "[REDIS EVENT BUS] redis package not installed. "
                "Install with: pip install redis"
            )
            return False
        except Exception as e:
            logger.warning(
                f"[REDIS EVENT BUS] Failed to connect to Redis: {e}. "
                "Falling back to in-memory event bus."
            )
            return False

    def _start_subscriber(self) -> None:
        """Start the Redis subscriber thread."""
        if self._subscriber_thread is not None:
            return

        self._running = True
        self._pubsub = self._redis_client.pubsub()
        self._pubsub.subscribe(self.CHANNEL_NAME)

        def subscriber_loop():
            """Listen for Redis pub/sub messages and dispatch to local subscribers."""
            import time as _time
            while self._running:
                try:
                    for message in self._pubsub.listen():
                        if not self._running:
                            return

                        if message["type"] != "message":
                            continue

                        try:
                            data = json.loads(message["data"])
                            event = AutonomousEvent(
                                event_type=data["event_type"],
                                thread_id=data["thread_id"],
                                user_id=data["user_id"],
                                task_id=data.get("task_id", ""),
                                data=data["data"],
                                timestamp=datetime.fromisoformat(data["timestamp"]),
                            )
                            with self._lock:
                                receive_count = self._bump_counter(
                                    self._redis_receive_counts,
                                    event.event_type,
                                )
                                local_subscribers = len(self._subscribers)
                            if should_log_stream_event_sample(event.event_type, receive_count):
                                logger.info(
                                    "[REDIS EVENT BUS] message_received type=%s count=%d "
                                    "local_subscribers=%d thread=%s user=%s task=%s",
                                    event.event_type,
                                    receive_count,
                                    local_subscribers,
                                    event.thread_id,
                                    event.user_id,
                                    event.task_id,
                                )
                            # Dispatch to local subscribers using parent class method
                            self._dispatch_local(event)
                        except (json.JSONDecodeError, KeyError) as e:
                            logger.warning(f"[REDIS EVENT BUS] Invalid message: {e}")

                except Exception as e:
                    if self._running:
                        logger.debug(f"[REDIS EVENT BUS] Subscriber reconnecting: {e}")
                        _time.sleep(0.5)

        self._subscriber_thread = threading.Thread(
            target=subscriber_loop,
            daemon=True,
            name="redis-event-subscriber",
        )
        self._subscriber_thread.start()
        logger.info("[REDIS EVENT BUS] Subscriber thread started")

    def _dispatch_local(self, event: AutonomousEvent) -> None:
        """
        Dispatch event to local subscribers only (called from Redis subscriber).

        Args:
            event: The event to dispatch
        """
        with self._lock:
            subscriber_count = len(self._subscribers)
            if subscriber_count == 0:
                no_subscriber_key = f"redis:no_subscribers:{event.event_type}"
                no_subscriber_count = self._bump_counter(self._drop_counts, no_subscriber_key)
                if should_log_stream_event_sample(event.event_type, no_subscriber_count):
                    logger.info(
                        "[REDIS EVENT BUS] no_local_subscribers type=%s count=%d "
                        "thread=%s user=%s task=%s",
                        event.event_type,
                        no_subscriber_count,
                        event.thread_id,
                        event.user_id,
                        event.task_id,
                    )
                return

            for sub_id, queue in list(self._subscribers.items()):
                try:
                    queue.put_nowait(event)
                    enqueue_key = f"redis:{sub_id}:{event.event_type}"
                    enqueue_count = self._bump_counter(self._enqueue_counts, enqueue_key)
                    if should_log_stream_event_sample(event.event_type, enqueue_count):
                        logger.info(
                            "[REDIS EVENT BUS] queue_enqueue subscriber=%s type=%s count=%d "
                            "queue_size=%d thread=%s task=%s",
                            sub_id[:8],
                            event.event_type,
                            enqueue_count,
                            queue.qsize(),
                            event.thread_id,
                            event.task_id,
                        )
                except Full:
                    drop_key = f"redis:{sub_id}:{event.event_type}"
                    drop_count = self._bump_counter(self._drop_counts, drop_key)
                    logger.warning(
                        "[REDIS EVENT BUS] queue_drop_full subscriber=%s type=%s "
                        "drop_count=%d thread=%s task=%s",
                        sub_id[:8],
                        event.event_type,
                        drop_count,
                        event.thread_id,
                        event.task_id,
                    )

    def publish(self, event: AutonomousEvent) -> None:
        """
        Publish an event to Redis (broadcasts to all containers).

        Args:
            event: The event to publish
        """
        if not self._connected or self._redis_client is None:
            # Fall back to in-memory publishing
            super().publish(event)
            return

        try:
            with self._lock:
                publish_count = self._bump_counter(
                    self._redis_publish_counts,
                    event.event_type,
                )
            # Serialize event to JSON
            event_data = {
                "event_type": event.event_type,
                "thread_id": event.thread_id,
                "user_id": event.user_id,
                "task_id": event.task_id,
                "data": event.data,
                "timestamp": event.timestamp.isoformat(),
            }
            message = json.dumps(event_data)

            # Publish to Redis
            receivers = self._redis_client.publish(self.CHANNEL_NAME, message)
            if should_log_stream_event_sample(event.event_type, publish_count):
                logger.info(
                    "[REDIS EVENT BUS] publish type=%s count=%d redis_receivers=%s "
                    "thread=%s user=%s task=%s",
                    event.event_type,
                    publish_count,
                    receivers,
                    event.thread_id,
                    event.user_id,
                    event.task_id,
                )

        except Exception as e:
            logger.error(f"[REDIS EVENT BUS] Publish failed: {e}, falling back to local")
            # Fall back to in-memory publishing
            super().publish(event)

    def close(self) -> None:
        """Close Redis connections and stop subscriber thread."""
        self._running = False

        if self._pubsub:
            try:
                self._pubsub.unsubscribe()
                self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None

        if self._redis_client:
            try:
                self._redis_client.close()
            except Exception:
                pass
            self._redis_client = None

        self._connected = False
        logger.info("[REDIS EVENT BUS] Closed")

    def is_connected(self) -> bool:
        """Check if connected to Redis."""
        return self._connected

    def __del__(self):
        """Cleanup on garbage collection."""
        self.close()
