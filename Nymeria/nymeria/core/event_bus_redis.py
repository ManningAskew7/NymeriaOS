"""Redis-backed event bus for cross-container communication."""

import json
import logging
import secrets
import threading
from datetime import datetime
from queue import Full
from typing import Any, Dict, Optional

from .event_bus import (
    AutonomousEvent,
    EventBus,
    redact_url_credentials,
    should_log_stream_event_sample,
)

logger = logging.getLogger(__name__)


class RedisEventBus(EventBus):
    """
    Redis-backed event bus for distributed deployments.

    Uses Redis pub/sub for real-time cross-container communication.
    Falls back to in-memory behavior if Redis connection fails.

    Resilience model: subscriber-enabled buses dispatch each ``publish()`` to
    local SSE subscribers *before* fanning out to Redis, so even if the Redis
    subscriber thread is momentarily dead the publishing process still delivers
    to its own clients. Publisher-only buses skip local dispatch because they
    intentionally have no local SSE subscribers. Each published message carries
    a per-process ``_publisher_id`` so the subscriber loop can drop its own
    echoes and avoid double-delivery.
    """

    CHANNEL_NAME = "nymeria:autonomous_events"

    def __init__(self, redis_url: str, *, enable_subscriber: bool = True):
        """
        Initialize Redis event bus.

        Args:
            redis_url: Redis connection URL (e.g., redis://localhost:6379)
            enable_subscriber: When True (default), start a pub/sub subscriber
                thread so this bus can receive cross-process events and
                dispatch them to local SSE subscribers. Set False for
                publisher-only processes (e.g. the Docker worker) that
                only need to fan events out and do not hold any local SSE
                subscribers. Avoids running a no-op subscriber thread that
                otherwise raises socket-timeout warnings every few seconds.
        """
        super().__init__()
        self.redis_url = redis_url
        self._enable_subscriber = enable_subscriber
        # Publisher uses socket_timeout=5 so a stuck publish fails fast.
        # Subscriber gets its own client (created in _connect when needed)
        # with socket_timeout=None so listen() blocks indefinitely on idle
        # instead of raising TimeoutError every ~5s and re-creating pubsub.
        self._redis_client: Optional[Any] = None
        self._subscriber_client: Optional[Any] = None
        self._pubsub: Optional[Any] = None
        self._subscriber_thread: Optional[threading.Thread] = None
        self._running = False
        self._connected = False
        self._redis_publish_counts: Dict[str, int] = {}
        self._redis_receive_counts: Dict[str, int] = {}
        # Per-process ID so the subscriber loop can ignore its own echoes
        # (we already delivered locally in publish()).
        self._publisher_id = secrets.token_urlsafe(8)

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
            logger.info(
                "[REDIS EVENT BUS] Connected to Redis at %s (subscriber=%s)",
                redact_url_credentials(self.redis_url),
                self._enable_subscriber,
            )

            # Start subscriber thread only when this process needs to receive
            # cross-process events. Publisher-only processes (e.g. the Docker
            # worker) skip this to avoid the idle socket-timeout warning loop.
            if self._enable_subscriber:
                # Dedicated subscriber client: no socket_timeout so the
                # pub/sub listener can sit idle indefinitely without
                # raising TimeoutError. Real connection drops surface
                # as ConnectionError and are still caught by the
                # subscriber loop's except handler.
                self._subscriber_client = redis.from_url(
                    self.redis_url,
                    decode_responses=True,
                    socket_timeout=None,
                    socket_connect_timeout=5,
                )
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
        if self._subscriber_client is None:
            return
        self._pubsub = self._subscriber_client.pubsub()
        self._pubsub.subscribe(self.CHANNEL_NAME)

        def subscriber_loop():
            """Listen for Redis pub/sub messages and dispatch to local subscribers.

            Recovers from connection drops by recreating the pubsub object and
            re-subscribing. Without this, a single dropped Redis connection
            would leave the loop iterating over a dead pubsub forever,
            silently breaking cross-process event delivery.
            """
            import time as _time
            while self._running:
                try:
                    if self._pubsub is None:
                        break
                    for message in self._pubsub.listen():
                        if not self._running:
                            return

                        if message["type"] != "message":
                            continue

                        try:
                            data = json.loads(message["data"])
                            # Skip our own echo — publish() already delivered
                            # this event to local subscribers directly.
                            if data.get("_publisher_id") == self._publisher_id:
                                continue
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

                    # listen() returned without exception (Redis closed the
                    # connection or the iterator ended). Rebuild pubsub.
                    if self._running:
                        logger.warning(
                            "[REDIS EVENT BUS] pubsub.listen() exited cleanly; "
                            "recreating pubsub and re-subscribing"
                        )
                        self._recreate_pubsub()
                        _time.sleep(0.5)

                except Exception as e:
                    if self._running:
                        logger.warning(
                            "[REDIS EVENT BUS] subscriber error %s: %s; "
                            "recreating pubsub and re-subscribing",
                            type(e).__name__,
                            e,
                        )
                        self._recreate_pubsub()
                        _time.sleep(1.0)

        self._subscriber_thread = threading.Thread(
            target=subscriber_loop,
            daemon=True,
            name="redis-event-subscriber",
        )
        self._subscriber_thread.start()
        logger.info("[REDIS EVENT BUS] Subscriber thread started")

    def _recreate_pubsub(self) -> None:
        """Tear down and re-create the pubsub subscription.

        Called when listen() exits or raises so the subscriber loop can
        recover from a dropped Redis connection without restarting the
        whole API process.
        """
        try:
            if self._pubsub is not None:
                try:
                    self._pubsub.close()
                except Exception:
                    logger.debug(
                        "[REDIS EVENT BUS] failed to close stale pubsub",
                        exc_info=True,
                    )
            if self._subscriber_client is None:
                return
            self._pubsub = self._subscriber_client.pubsub()
            self._pubsub.subscribe(self.CHANNEL_NAME)
            logger.info("[REDIS EVENT BUS] pubsub recreated and re-subscribed")
        except Exception as exc:
            logger.error(
                "[REDIS EVENT BUS] failed to recreate pubsub: %s; will retry on next iteration",
                exc,
            )

    def unsubscribe(self, subscriber_id: str) -> None:
        """Unsubscribe and also prune the Redis-path per-subscriber counters.

        ``_dispatch_local`` keys its enqueue/drop counters
        ``f"redis:{sub_id}:{event_type}"``, so the base prune (which targets
        the ``f"{sub_id}:"`` prefix) does not reach them. Drop them here too so
        neither path leaks counter keys as subscribers come and go.
        """
        super().unsubscribe(subscriber_id)
        with self._lock:
            self._prune_counter_keys(f"redis:{subscriber_id}:")

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

            rung = []
            for sub_id, queue in list(self._subscribers.items()):
                try:
                    queue.put_nowait(event)
                    pulse = self._doorbells.get(sub_id)
                    if pulse is not None:
                        rung.append(pulse)
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

        # Ring outside the lock (same rule as the base class): this runs on
        # the Redis subscriber THREAD, and the wake is marshalled onto each
        # listener's loop by the pulse itself.
        for pulse in rung:
            pulse.ring()

    def publish(self, event: AutonomousEvent) -> None:
        """
        Publish an event.

        Delivery is two-step and independent for subscriber-enabled buses:
        dispatch to local SSE subscribers first, then fan out to other
        processes via Redis pub/sub. If Redis is unreachable or the subscriber
        loop is dead, local clients still receive the event. Publisher-only
        buses skip local dispatch because they intentionally have no local SSE
        subscribers. The Redis subscriber loop on *other* processes drops
        their own echoes via ``_publisher_id``, so the publisher process won't
        double-deliver to itself.
        """
        if self._enable_subscriber:
            # This is the only path that reaches SSE subscribers connected to
            # this process and must not depend on Redis being healthy.
            super().publish(event)

        if not self._connected or self._redis_client is None:
            return

        try:
            with self._lock:
                publish_count = self._bump_counter(
                    self._redis_publish_counts,
                    event.event_type,
                )
            event_data = {
                "event_type": event.event_type,
                "thread_id": event.thread_id,
                "user_id": event.user_id,
                "task_id": event.task_id,
                "data": event.data,
                "timestamp": event.timestamp.isoformat(),
                "_publisher_id": self._publisher_id,
            }
            message = json.dumps(event_data)

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
            local_delivery_status = (
                "local delivery already done"
                if self._enable_subscriber
                else "publisher-only local delivery skipped"
            )
            logger.error(
                "[REDIS EVENT BUS] cross-process publish failed: %s "
                "(%s)",
                e,
                local_delivery_status,
            )

    def _teardown(self, *, quiet: bool = False) -> None:
        """Close Redis connections and stop the subscriber thread.

        ``quiet`` suppresses all logging and is used by ``__del__``: a finalizer
        can run during interpreter shutdown when the ``logging`` module and the
        ``redis`` library internals may already be half torn down, where a log
        call can raise or emit "Exception ignored in" noise to stderr.
        """
        self._running = False

        if self._pubsub:
            try:
                self._pubsub.unsubscribe()
                self._pubsub.close()
            except Exception:
                if not quiet:
                    logger.debug("Error closing Redis pubsub during shutdown")
            self._pubsub = None

        if self._redis_client:
            try:
                self._redis_client.close()
            except Exception:
                if not quiet:
                    logger.debug("Error closing Redis client during shutdown")
            self._redis_client = None

        if self._subscriber_client:
            try:
                self._subscriber_client.close()
            except Exception:
                if not quiet:
                    logger.debug("Error closing Redis subscriber client during shutdown")
            self._subscriber_client = None

        self._connected = False
        if not quiet:
            logger.info("[REDIS EVENT BUS] Closed")

    def close(self) -> None:
        """Close Redis connections and stop subscriber thread (logs on completion)."""
        self._teardown(quiet=False)

    def is_connected(self) -> bool:
        """Check if connected to Redis."""
        return self._connected

    def __del__(self):
        """Best-effort cleanup at GC / interpreter shutdown.

        Kept as a safety net because the bus is a process-lived singleton that
        is often never explicitly closed, so its finalizer is the only teardown
        that runs. It runs the quiet teardown so it never logs: the ``logging``
        module may be half-finalized at shutdown, where a log call can raise and
        surface as an "Exception ignored in" traceback on stderr. The teardown
        is self-contained: each Redis ``close()``/``unsubscribe()`` is already
        wrapped in its own handler (silent when quiet) and the rest is plain
        attribute resets that cannot raise, so no outer guard is needed here.
        Explicit ``close()`` remains the owned teardown path and logs as before.
        """
        self._teardown(quiet=True)
