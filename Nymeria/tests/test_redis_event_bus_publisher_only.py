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
