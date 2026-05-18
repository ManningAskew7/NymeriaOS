"""Tests for spawn_thread lifetime tracking helpers.

Covers refresh_thread_activity (called on every callable invocation of a
temporary-lifetime thread) and sweep_idle_spawned_threads (called periodically
by the Ticker housekeeping executor).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.core.time_utils import utc_now
from nymeria.tools.spawn_thread import (
    DEFAULT_IDLE_TIMEOUT_HOURS,
    PLATFORM_META_IDLE_TIMEOUT,
    PLATFORM_META_LAST_ACTIVE,
    PLATFORM_META_LIFETIME,
    refresh_thread_activity,
    sweep_idle_spawned_threads,
)


def _make_agent(tmp_path: Path) -> SimpleNamespace:
    """Build a minimal agent stub exposing thread_metadata_manager."""
    return SimpleNamespace(
        thread_metadata_manager=ThreadMetadataManager(tmp_path),
    )


def _seed_thread(
    agent: SimpleNamespace,
    user_id: str,
    thread_id: str,
    *,
    lifetime: str | None = None,
    idle_timeout_hours: int | None = None,
    last_active: str | None = None,
) -> None:
    platform_meta: dict[str, str] = {}
    if lifetime is not None:
        platform_meta[PLATFORM_META_LIFETIME] = lifetime
    if idle_timeout_hours is not None:
        platform_meta[PLATFORM_META_IDLE_TIMEOUT] = str(idle_timeout_hours)
    if last_active is not None:
        platform_meta[PLATFORM_META_LAST_ACTIVE] = last_active
    agent.thread_metadata_manager.upsert_thread(
        user_id,
        thread_id,
        title="seed",
        title_source="callable",
        platform="callable",
        platform_meta=platform_meta or None,
    )


class TestRefreshThreadActivity:
    def test_updates_last_active_on_temporary(self, tmp_path):
        agent = _make_agent(tmp_path)
        _seed_thread(
            agent,
            "u1",
            "spawned-foo-abc123",
            lifetime="temporary",
            idle_timeout_hours=24,
            last_active=(utc_now() - timedelta(hours=5)).isoformat(),
        )
        before = utc_now()
        refresh_thread_activity(agent, "u1", "spawned-foo-abc123")
        meta = agent.thread_metadata_manager.get_thread("u1", "spawned-foo-abc123")
        assert meta is not None
        assert meta.platform_meta is not None
        last_active_str = meta.platform_meta[PLATFORM_META_LAST_ACTIVE]
        from datetime import datetime

        last_active = datetime.fromisoformat(last_active_str)
        # The refresh stamps a timestamp at or after `before` (within seconds).
        assert (last_active - before).total_seconds() >= -1.0

    def test_noop_on_permanent(self, tmp_path):
        agent = _make_agent(tmp_path)
        _seed_thread(agent, "u1", "spawned-bar-def456", lifetime=None)
        refresh_thread_activity(agent, "u1", "spawned-bar-def456")
        meta = agent.thread_metadata_manager.get_thread("u1", "spawned-bar-def456")
        assert meta is not None
        if meta.platform_meta:
            assert PLATFORM_META_LAST_ACTIVE not in meta.platform_meta

    def test_noop_on_missing_thread(self, tmp_path):
        agent = _make_agent(tmp_path)
        # No seed: thread doesn't exist. Should not raise.
        refresh_thread_activity(agent, "u1", "does-not-exist")


class TestSweepIdleSpawnedThreads:
    @patch("nymeria.tools.spawn_thread._delete_spawned")
    def test_deletes_expired_temporary(self, mock_delete, tmp_path):
        agent = _make_agent(tmp_path)
        _seed_thread(
            agent,
            "u1",
            "spawned-expired-aaa111",
            lifetime="temporary",
            idle_timeout_hours=1,
            last_active=(utc_now() - timedelta(hours=2)).isoformat(),
        )
        deleted = sweep_idle_spawned_threads(agent)
        assert deleted == 1
        mock_delete.assert_called_once()
        # Verify the call was for the expired thread.
        kwargs = mock_delete.call_args.kwargs
        assert kwargs["target_thread_id"] == "spawned-expired-aaa111"
        assert kwargs["user_id"] == "u1"
        assert kwargs["caller_thread_id"] is None

    @patch("nymeria.tools.spawn_thread._delete_spawned")
    def test_skips_active_temporary(self, mock_delete, tmp_path):
        agent = _make_agent(tmp_path)
        _seed_thread(
            agent,
            "u1",
            "spawned-active-bbb222",
            lifetime="temporary",
            idle_timeout_hours=24,
            last_active=utc_now().isoformat(),
        )
        deleted = sweep_idle_spawned_threads(agent)
        assert deleted == 0
        mock_delete.assert_not_called()

    @patch("nymeria.tools.spawn_thread._delete_spawned")
    def test_skips_permanent(self, mock_delete, tmp_path):
        agent = _make_agent(tmp_path)
        # Permanent thread, no lifetime field set.
        _seed_thread(agent, "u1", "spawned-permanent-ccc333")
        deleted = sweep_idle_spawned_threads(agent)
        assert deleted == 0
        mock_delete.assert_not_called()

    @patch("nymeria.tools.spawn_thread._delete_spawned")
    def test_skips_when_idle_timeout_missing_or_invalid(self, mock_delete, tmp_path):
        agent = _make_agent(tmp_path)
        # Temporary but missing idle_timeout_hours: defensive skip.
        _seed_thread(
            agent,
            "u1",
            "spawned-broken-ddd444",
            lifetime="temporary",
            idle_timeout_hours=None,
            last_active=(utc_now() - timedelta(days=30)).isoformat(),
        )
        deleted = sweep_idle_spawned_threads(agent)
        assert deleted == 0
        mock_delete.assert_not_called()

    @patch("nymeria.tools.spawn_thread._delete_spawned")
    def test_falls_back_to_created_at_when_last_active_missing(
        self, mock_delete, tmp_path
    ):
        agent = _make_agent(tmp_path)
        # Temporary with idle_timeout but no last_active recorded.
        # The sweeper should fall back to created_at; since we just
        # created the thread, it's still within the idle window.
        _seed_thread(
            agent,
            "u1",
            "spawned-no-lastact-eee555",
            lifetime="temporary",
            idle_timeout_hours=24,
        )
        deleted = sweep_idle_spawned_threads(agent)
        assert deleted == 0
        mock_delete.assert_not_called()

    @patch("nymeria.tools.spawn_thread._delete_spawned")
    def test_handles_empty_metadata_dir(self, mock_delete, tmp_path):
        agent = _make_agent(tmp_path)
        deleted = sweep_idle_spawned_threads(agent)
        assert deleted == 0
        mock_delete.assert_not_called()


class TestParameterDefaults:
    def test_default_idle_timeout_hours_is_24(self):
        assert DEFAULT_IDLE_TIMEOUT_HOURS == 24
