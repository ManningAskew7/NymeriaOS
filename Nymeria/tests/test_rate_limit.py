from __future__ import annotations

import pytest

from nymeria.core.rate_limit import SlidingWindowRateLimiter


def test_sliding_window_rate_limiter_blocks_until_oldest_event_expires():
    now = [100.0]
    limiter = SlidingWindowRateLimiter(clock=lambda: now[0])

    first = limiter.check("service:endpoint", limit=2, window_seconds=10.0)
    second = limiter.check("service:endpoint", limit=2, window_seconds=10.0)

    assert first.allowed is True
    assert first.remaining == 1
    assert second.allowed is True
    assert second.remaining == 0

    now[0] = 105.0
    blocked = limiter.check("service:endpoint", limit=2, window_seconds=10.0)

    assert blocked.allowed is False
    assert blocked.retry_after == pytest.approx(5.0)

    now[0] = 110.001
    allowed = limiter.check("service:endpoint", limit=2, window_seconds=10.0)

    assert allowed.allowed is True
    assert allowed.remaining == 1


def test_sliding_window_rate_limiter_buckets_keys_independently():
    limiter = SlidingWindowRateLimiter(clock=lambda: 100.0)

    assert limiter.check("service:a", limit=1, window_seconds=10.0).allowed is True
    assert limiter.check("service:a", limit=1, window_seconds=10.0).allowed is False
    assert limiter.check("service:b", limit=1, window_seconds=10.0).allowed is True


def test_sliding_window_rate_limiter_rejects_invalid_configuration():
    limiter = SlidingWindowRateLimiter()

    with pytest.raises(ValueError, match="limit"):
        limiter.check("x", limit=0, window_seconds=10.0)

    with pytest.raises(ValueError, match="window_seconds"):
        limiter.check("x", limit=1, window_seconds=0.0)
