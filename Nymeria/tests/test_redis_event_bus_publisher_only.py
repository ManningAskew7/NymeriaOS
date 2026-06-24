"""Tests for RedisEventBus publisher-only mode.

The Docker worker container publishes autonomous events (task_started,
agent stream chunks, task_completed) but does not consume them.
Running a Redis pub/sub subscriber in that process is useless and
produces idle ``TimeoutError: Timeout reading from socket`` warnings
every ~5 seconds. ``enable_subscriber=False`` suppresses the subscriber.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_redis_module(monkeypatch):
    """Install a fake redis module so RedisEventBus._connect succeeds.

    Yields (fake_redis, fake_client, fake_pubsub) so individual tests can
    assert against call records.
    """
    fake_redis = MagicMock()
    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_pubsub = MagicMock()
    fake_client.pubsub.return_value = fake_pubsub
    fake_redis.from_url.return_value = fake_client
    monkeypatch.setitem(sys.modules, "redis", fake_redis)
    yield fake_redis, fake_client, fake_pubsub


def test_publisher_only_mode_skips_pubsub(fake_redis_module):
    """enable_subscriber=False must not create a pubsub or subscriber thread."""
    from nymeria.core.event_bus_redis import RedisEventBus

    fake_redis, fake_client, _ = fake_redis_module

    with patch.object(RedisEventBus, "_start_subscriber") as mock_start:
        bus = RedisEventBus("redis://localhost:6379", enable_subscriber=False)
        mock_start.assert_not_called()

    assert bus._connected is True
    assert bus._pubsub is None
    assert bus._subscriber_thread is None
    assert bus._subscriber_client is None
    fake_client.pubsub.assert_not_called()
    # Only the publisher client should have been created (one from_url call).
    assert fake_redis.from_url.call_count == 1
    bus.close()


def test_default_mode_starts_subscriber(fake_redis_module):
    """Default (enable_subscriber=True) preserves the API's subscriber path."""
    from nymeria.core.event_bus_redis import RedisEventBus

    fake_redis, _, _ = fake_redis_module

    with patch.object(RedisEventBus, "_start_subscriber") as mock_start:
        bus = RedisEventBus("redis://localhost:6379")
        mock_start.assert_called_once()

    assert bus._connected is True
    assert bus._subscriber_client is not None
    bus.close()


def test_subscriber_client_has_no_socket_timeout(fake_redis_module):
    """The subscriber client must be built with socket_timeout=None.

    This is what stops the idle TimeoutError-every-5s log loop on the
    API. The publisher client keeps socket_timeout=5 so a stuck publish
    still fails fast.
    """
    from nymeria.core.event_bus_redis import RedisEventBus

    fake_redis, _, _ = fake_redis_module

    with patch.object(RedisEventBus, "_start_subscriber"):
        bus = RedisEventBus("redis://localhost:6379")

    calls = fake_redis.from_url.call_args_list
    assert len(calls) == 2, "expected one publisher + one subscriber from_url call"
    publisher_kwargs = calls[0].kwargs
    subscriber_kwargs = calls[1].kwargs
    assert publisher_kwargs.get("socket_timeout") == 5
    assert subscriber_kwargs.get("socket_timeout") is None
    assert subscriber_kwargs.get("socket_connect_timeout") == 5
    bus.close()


def test_publisher_only_mode_can_still_publish_without_local_drop_warning(
    fake_redis_module,
    caplog,
):
    """publish() still fans out to Redis without local no-subscriber noise."""
    from nymeria.core.event_bus import AutonomousEvent
    from nymeria.core.event_bus_redis import RedisEventBus

    _, fake_client, _ = fake_redis_module
    fake_client.publish.return_value = 1

    with patch.object(RedisEventBus, "_start_subscriber"):
        bus = RedisEventBus("redis://localhost:6379", enable_subscriber=False)

    event = AutonomousEvent(
        event_type="task_started",
        thread_id="thread-1",
        user_id="user-1",
        task_id="todo-1",
        data={"prompt": "hi"},
        timestamp=datetime.now(timezone.utc),
    )
    with caplog.at_level(logging.WARNING, logger="nymeria.core.event_bus"):
        bus.publish(event)

    fake_client.publish.assert_called_once()
    channel, _payload = fake_client.publish.call_args.args
    assert channel == RedisEventBus.CHANNEL_NAME
    assert "publish_drop_no_subscribers" not in caplog.text
    bus.close()


def test_close_safe_in_publisher_only_mode(fake_redis_module):
    """close() must be a no-op-friendly tear-down when pubsub was never created."""
    from nymeria.core.event_bus_redis import RedisEventBus

    with patch.object(RedisEventBus, "_start_subscriber"):
        bus = RedisEventBus("redis://localhost:6379", enable_subscriber=False)

    bus.close()
    assert bus._connected is False
    assert bus._pubsub is None
    assert bus._redis_client is None


def test_create_event_bus_propagates_enable_subscriber_false(fake_redis_module):
    """create_event_bus(..., enable_subscriber=False) reaches RedisEventBus."""
    from nymeria.core.event_bus import create_event_bus
    from nymeria.core.event_bus_redis import RedisEventBus

    settings = MagicMock()
    settings.redis_enabled = True
    settings.redis_url = "redis://localhost:6379"

    with patch.object(RedisEventBus, "_start_subscriber") as mock_start:
        bus = create_event_bus(settings, enable_subscriber=False)
        mock_start.assert_not_called()

    assert isinstance(bus, RedisEventBus)
    assert bus._pubsub is None
    bus.close()


def test_unsubscribe_prunes_redis_local_dispatch_counters(fake_redis_module):
    """RedisEventBus.unsubscribe must drop the redis:{sub}: counter keys that
    _dispatch_local creates, which the base f"{sub}:" prune does not reach."""
    from nymeria.core.event_bus import AutonomousEvent
    from nymeria.core.event_bus_redis import RedisEventBus

    with patch.object(RedisEventBus, "_start_subscriber"):
        bus = RedisEventBus("redis://localhost:6379", enable_subscriber=False)

    # A cross-process event with no local subscriber records a bounded,
    # event-type-keyed "no_subscribers" drop counter.
    event = AutonomousEvent(event_type="response", thread_id="t1", user_id="u1")
    bus._dispatch_local(event)
    assert any(k.startswith("redis:no_subscribers:") for k in bus._drop_counts)

    # With a local subscriber, _dispatch_local enqueues and records a
    # per-subscriber redis:{sub}: counter.
    bus.subscribe("sub_x")
    bus._dispatch_local(event)
    assert any(k.startswith("redis:sub_x:") for k in bus._enqueue_counts)

    bus.unsubscribe("sub_x")

    # The per-subscriber redis counters are pruned; the bounded
    # event-type-keyed no_subscribers counter survives.
    assert not any(k.startswith("redis:sub_x:") for k in bus._enqueue_counts)
    assert not any(k.startswith("redis:sub_x:") for k in bus._drop_counts)
    assert any(k.startswith("redis:no_subscribers:") for k in bus._drop_counts)
    bus.close()


def test_create_event_bus_default_starts_subscriber(fake_redis_module):
    """create_event_bus default preserves the API's subscriber behavior."""
    from nymeria.core.event_bus import create_event_bus
    from nymeria.core.event_bus_redis import RedisEventBus

    settings = MagicMock()
    settings.redis_enabled = True
    settings.redis_url = "redis://localhost:6379"

    with patch.object(RedisEventBus, "_start_subscriber") as mock_start:
        bus = create_event_bus(settings)
        mock_start.assert_called_once()

    assert isinstance(bus, RedisEventBus)
    bus.close()


# --- close() vs __del__ teardown semantics (slice 08 F9) -------------------
#
# The bus is a process-lived singleton whose __del__ only fires at GC /
# interpreter shutdown, when the logging module and redis internals may be
# half-finalized. close() (explicit, owned teardown) logs as before; __del__
# must run the same teardown silently and swallow any error so a finalizer
# cannot raise or emit "Exception ignored in" noise to stderr.

_REDIS_BUS_LOGGER = "nymeria.core.event_bus_redis"


def _make_bus_with_all_clients(fake_redis_module):
    """Construct a connected bus and force all three teardown branches live."""
    from nymeria.core.event_bus_redis import RedisEventBus

    _, fake_client, _ = fake_redis_module
    with patch.object(RedisEventBus, "_start_subscriber"):
        bus = RedisEventBus("redis://localhost:6379", enable_subscriber=False)
    # Publisher-only leaves pubsub/subscriber unset; populate every branch so a
    # teardown test exercises all three close paths.
    bus._pubsub = MagicMock()
    bus._subscriber_client = MagicMock()
    assert bus._redis_client is fake_client
    return bus


def _redis_bus_log_text(caplog):
    """Captured text from the event_bus_redis logger only (ignore other loggers)."""
    return "\n".join(
        r.getMessage() for r in caplog.records if r.name == _REDIS_BUS_LOGGER
    )


def test_close_logs_completion_and_clears_clients(fake_redis_module, caplog):
    """Explicit close() still logs the completion line and clears every client."""
    bus = _make_bus_with_all_clients(fake_redis_module)
    pubsub, redis_client, sub_client = (
        bus._pubsub,
        bus._redis_client,
        bus._subscriber_client,
    )

    with caplog.at_level(logging.DEBUG, logger=_REDIS_BUS_LOGGER):
        bus.close()

    assert "[REDIS EVENT BUS] Closed" in _redis_bus_log_text(caplog)
    pubsub.unsubscribe.assert_called_once()
    pubsub.close.assert_called_once()
    redis_client.close.assert_called_once()
    sub_client.close.assert_called_once()
    assert bus._pubsub is None
    assert bus._redis_client is None
    assert bus._subscriber_client is None
    assert bus._connected is False
    assert bus._running is False


def test_del_tears_down_without_logging(fake_redis_module, caplog):
    """__del__ closes the same clients but emits no log records."""
    bus = _make_bus_with_all_clients(fake_redis_module)
    pubsub, redis_client, sub_client = (
        bus._pubsub,
        bus._redis_client,
        bus._subscriber_client,
    )

    with caplog.at_level(logging.DEBUG, logger=_REDIS_BUS_LOGGER):
        bus.__del__()

    # Same teardown effects as close()...
    pubsub.close.assert_called_once()
    redis_client.close.assert_called_once()
    sub_client.close.assert_called_once()
    assert bus._connected is False
    # ...but completely silent (no completion line, no per-client debug lines).
    assert _redis_bus_log_text(caplog) == ""


def test_del_swallows_teardown_errors_silently(fake_redis_module, caplog):
    """A raising client.close() in __del__ must not propagate or log.

    The quiet teardown's per-branch handlers absorb each Redis call error
    without logging (no outer try/except needed in __del__).
    """
    bus = _make_bus_with_all_clients(fake_redis_module)
    bus._pubsub.unsubscribe.side_effect = RuntimeError("pubsub gone")
    bus._redis_client.close.side_effect = RuntimeError("client gone")
    bus._subscriber_client.close.side_effect = RuntimeError("subscriber gone")

    with caplog.at_level(logging.DEBUG, logger=_REDIS_BUS_LOGGER):
        # Must not raise despite every teardown step failing.
        bus.__del__()

    assert _redis_bus_log_text(caplog) == ""
    # Even on error each handle is dropped so the next __del__ is a no-op.
    assert bus._pubsub is None
    assert bus._redis_client is None
    assert bus._subscriber_client is None
    assert bus._connected is False


def test_close_logs_per_client_error_when_not_quiet(fake_redis_module, caplog):
    """close() (quiet=False) preserves the prior per-client debug breadcrumb."""
    bus = _make_bus_with_all_clients(fake_redis_module)
    bus._redis_client.close.side_effect = RuntimeError("client gone")

    with caplog.at_level(logging.DEBUG, logger=_REDIS_BUS_LOGGER):
        bus.close()

    assert "Error closing Redis client during shutdown" in _redis_bus_log_text(caplog)
    assert bus._redis_client is None


def test_close_is_idempotent(fake_redis_module):
    """Calling close() twice (and then __del__) is safe and a no-op the 2nd time."""
    bus = _make_bus_with_all_clients(fake_redis_module)
    bus.close()
    bus.close()  # second close hits the falsy-client guards, no error
    bus.__del__()  # finalizer after an explicit close is a silent no-op
    assert bus._connected is False
