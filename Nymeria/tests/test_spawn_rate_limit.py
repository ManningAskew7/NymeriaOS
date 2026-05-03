"""Tests for spawn_thread rate limiting.

Verifies the process-local rate limiter behaves correctly within a process
and resets when the module state is cleared (simulating a process restart).
"""

from __future__ import annotations

import time
from unittest.mock import patch

from nymeria.tools.spawn_thread import (
    DEFAULT_MAX_SPAWNS_PER_HOUR,
    RATE_WINDOW_SECONDS,
    _check_rate_limit,
    _spawn_counts,
    _spawn_rate_lock,
)


def _clear_rate_state():
    with _spawn_rate_lock:
        _spawn_counts.clear()


class TestRateLimitBasics:
    def setup_method(self):
        _clear_rate_state()

    def teardown_method(self):
        _clear_rate_state()

    def test_allows_up_to_limit(self):
        for i in range(DEFAULT_MAX_SPAWNS_PER_HOUR):
            assert _check_rate_limit("thread-a") is None, f"spawn {i+1} should be allowed"

    def test_rejects_over_limit(self):
        for _ in range(DEFAULT_MAX_SPAWNS_PER_HOUR):
            _check_rate_limit("thread-a")
        err = _check_rate_limit("thread-a")
        assert err is not None
        assert "rate limit" in err.lower()

    def test_independent_per_parent(self):
        for _ in range(DEFAULT_MAX_SPAWNS_PER_HOUR):
            _check_rate_limit("thread-a")
        assert _check_rate_limit("thread-a") is not None
        assert _check_rate_limit("thread-b") is None

    def test_expired_timestamps_are_pruned(self):
        old = time.time() - RATE_WINDOW_SECONDS - 1
        with _spawn_rate_lock:
            _spawn_counts["thread-old"] = [old] * DEFAULT_MAX_SPAWNS_PER_HOUR
        assert _check_rate_limit("thread-old") is None

    @patch.dict("os.environ", {"NYMERIA_MAX_SPAWNS_PER_HOUR": "2"})
    def test_env_override(self):
        assert _check_rate_limit("thread-env") is None
        assert _check_rate_limit("thread-env") is None
        err = _check_rate_limit("thread-env")
        assert err is not None
        assert "2 per hour" in err


class TestProcessRestartSimulation:
    """Verify that clearing module state (simulating restart) resets limits."""

    def setup_method(self):
        _clear_rate_state()

    def teardown_method(self):
        _clear_rate_state()

    def test_clearing_counts_resets_limit(self):
        for _ in range(DEFAULT_MAX_SPAWNS_PER_HOUR):
            _check_rate_limit("thread-restart")
        assert _check_rate_limit("thread-restart") is not None

        _clear_rate_state()

        assert _check_rate_limit("thread-restart") is None

    def test_fresh_dict_simulates_new_process(self):
        """A new process gets a fresh empty dict — verify the old state is gone."""
        import sys

        mod = sys.modules["nymeria.tools.spawn_thread"]

        for _ in range(DEFAULT_MAX_SPAWNS_PER_HOUR):
            _check_rate_limit("thread-fresh")
        assert _check_rate_limit("thread-fresh") is not None

        old_counts = mod._spawn_counts
        mod._spawn_counts = {}
        try:
            assert _check_rate_limit("thread-fresh") is None
        finally:
            mod._spawn_counts = old_counts
