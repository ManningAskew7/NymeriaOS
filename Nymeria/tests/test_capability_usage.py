"""Unit tests for the shared thread-config prune policy helpers.

`compute_prune_window` and `collect_stale_names` centralize the stale-window
math and stale-by-usage detection shared by the tool prune
(`tools/tool_search._prune_tools`) and the skill prune
(`tools/search_skills._prune_thread_skills`). These tests lock the policy in
one place so the two prune surfaces cannot drift.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nymeria.core.capability_usage import (
    PruneWindow,
    collect_stale_names,
    compute_prune_window,
)

_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)


class TestComputePruneWindow:
    def test_returns_named_tuple_with_unpackable_fields(self):
        window = compute_prune_window(
            stale_after_days=30,
            min_enabled_age_days=7,
            config_updated_at=_NOW - timedelta(days=10),
            now=_NOW,
        )
        assert isinstance(window, PruneWindow)
        stale_days, min_age_days, stale_cutoff, config_old_enough = window
        assert stale_days == 30
        assert min_age_days == 7
        assert stale_cutoff == _NOW - timedelta(days=30)
        assert config_old_enough is True

    def test_cutoff_derives_from_passed_now_not_wall_clock(self):
        fixed = datetime(2020, 1, 1, tzinfo=timezone.utc)
        window = compute_prune_window(
            stale_after_days=5,
            min_enabled_age_days=0,
            config_updated_at=fixed,
            now=fixed,
        )
        assert window.stale_cutoff == fixed - timedelta(days=5)

    def test_stale_days_floor_is_one_for_negative(self):
        window = compute_prune_window(
            stale_after_days=-3,
            min_enabled_age_days=0,
            config_updated_at=_NOW,
            now=_NOW,
        )
        assert window.stale_days == 1

    def test_stale_days_zero_falls_back_to_default_thirty(self):
        # `int(stale_after_days or 30)` treats falsy 0 as "unset" -> 30.
        window = compute_prune_window(
            stale_after_days=0,
            min_enabled_age_days=0,
            config_updated_at=_NOW,
            now=_NOW,
        )
        assert window.stale_days == 30

    def test_min_age_days_floor_is_zero_for_negative(self):
        window = compute_prune_window(
            stale_after_days=30,
            min_enabled_age_days=-5,
            config_updated_at=_NOW,
            now=_NOW,
        )
        assert window.min_age_days == 0

    def test_config_old_enough_boundary_equal_is_old_enough(self):
        # updated_at == now - min_age_days -> `<=` is True (inclusive).
        window = compute_prune_window(
            stale_after_days=30,
            min_enabled_age_days=7,
            config_updated_at=_NOW - timedelta(days=7),
            now=_NOW,
        )
        assert window.config_old_enough is True

    def test_config_old_enough_just_under_age_is_false(self):
        window = compute_prune_window(
            stale_after_days=30,
            min_enabled_age_days=7,
            config_updated_at=_NOW - timedelta(days=7) + timedelta(seconds=1),
            now=_NOW,
        )
        assert window.config_old_enough is False

    def test_config_old_enough_older_than_age_is_true(self):
        window = compute_prune_window(
            stale_after_days=30,
            min_enabled_age_days=7,
            config_updated_at=_NOW - timedelta(days=8),
            now=_NOW,
        )
        assert window.config_old_enough is True

    def test_naive_config_updated_at_treated_as_utc(self):
        # ensure_aware_utc treats naive timestamps as UTC (legacy JSON data).
        naive = (_NOW - timedelta(days=10)).replace(tzinfo=None)
        window = compute_prune_window(
            stale_after_days=30,
            min_enabled_age_days=7,
            config_updated_at=naive,
            now=_NOW,
        )
        assert window.config_old_enough is True


class TestCollectStaleNames:
    def _lookup(self, mapping):
        return lambda name: mapping.get(name)

    def test_returns_empty_when_not_old_enough(self):
        cutoff = _NOW - timedelta(days=30)
        mapping = {"a": (_NOW - timedelta(days=45)).isoformat()}
        result = collect_stale_names(
            ["a"],
            skip=set(),
            get_last_used_at=self._lookup(mapping),
            stale_cutoff=cutoff,
            config_old_enough=False,
        )
        assert result == []

    def test_collects_only_usage_at_or_before_cutoff(self):
        cutoff = _NOW - timedelta(days=30)
        mapping = {
            "stale": (_NOW - timedelta(days=45)).isoformat(),
            "fresh": (_NOW - timedelta(days=1)).isoformat(),
        }
        result = collect_stale_names(
            ["stale", "fresh"],
            skip=set(),
            get_last_used_at=self._lookup(mapping),
            stale_cutoff=cutoff,
            config_old_enough=True,
        )
        assert result == ["stale"]

    def test_boundary_equal_to_cutoff_is_collected(self):
        cutoff = _NOW - timedelta(days=30)
        mapping = {"edge": cutoff.isoformat()}
        result = collect_stale_names(
            ["edge"],
            skip=set(),
            get_last_used_at=self._lookup(mapping),
            stale_cutoff=cutoff,
            config_old_enough=True,
        )
        assert result == ["edge"]

    def test_skip_members_are_ignored(self):
        cutoff = _NOW - timedelta(days=30)
        old = (_NOW - timedelta(days=45)).isoformat()
        mapping = {"a": old, "b": old}
        result = collect_stale_names(
            ["a", "b"],
            skip={"a"},
            get_last_used_at=self._lookup(mapping),
            stale_cutoff=cutoff,
            config_old_enough=True,
        )
        assert result == ["b"]

    def test_no_recorded_usage_is_kept(self):
        cutoff = _NOW - timedelta(days=30)
        result = collect_stale_names(
            ["never_used"],
            skip=set(),
            get_last_used_at=self._lookup({}),  # returns None
            stale_cutoff=cutoff,
            config_old_enough=True,
        )
        assert result == []

    def test_unparseable_timestamp_is_kept(self):
        cutoff = _NOW - timedelta(days=30)
        mapping = {"weird": "not-a-timestamp"}
        result = collect_stale_names(
            ["weird"],
            skip=set(),
            get_last_used_at=self._lookup(mapping),
            stale_cutoff=cutoff,
            config_old_enough=True,
        )
        assert result == []

    def test_iteration_order_is_preserved(self):
        cutoff = _NOW - timedelta(days=30)
        old = (_NOW - timedelta(days=45)).isoformat()
        mapping = {"z": old, "a": old, "m": old}
        result = collect_stale_names(
            ["z", "a", "m"],
            skip=set(),
            get_last_used_at=self._lookup(mapping),
            stale_cutoff=cutoff,
            config_old_enough=True,
        )
        assert result == ["z", "a", "m"]

    def test_skip_accepts_unioned_collections(self):
        # Mirrors the call-site pattern: set(frozenset) | set(list) | ...
        cutoff = _NOW - timedelta(days=30)
        old = (_NOW - timedelta(days=45)).isoformat()
        mapping = {n: old for n in ("a", "b", "c", "d")}
        skip = frozenset({"a"}) | set(["b"]) | set(["c"])
        result = collect_stale_names(
            ["a", "b", "c", "d"],
            skip=skip,
            get_last_used_at=self._lookup(mapping),
            stale_cutoff=cutoff,
            config_old_enough=True,
        )
        assert result == ["d"]


class TestPolicyIntegration:
    """Wire the two helpers together as the prune call sites do."""

    def _lookup(self, mapping):
        return lambda name: mapping.get(name)

    def test_window_not_old_enough_yields_no_stale(self):
        window = compute_prune_window(
            stale_after_days=30,
            min_enabled_age_days=7,
            config_updated_at=_NOW - timedelta(days=1),  # younger than min age
            now=_NOW,
        )
        assert window.config_old_enough is False
        mapping = {"a": (_NOW - timedelta(days=45)).isoformat()}
        result = collect_stale_names(
            ["a"],
            skip=set(),
            get_last_used_at=self._lookup(mapping),
            stale_cutoff=window.stale_cutoff,
            config_old_enough=window.config_old_enough,
        )
        assert result == []

    def test_window_old_enough_collects_stale_via_cutoff(self):
        window = compute_prune_window(
            stale_after_days=30,
            min_enabled_age_days=0,
            config_updated_at=_NOW - timedelta(days=10),
            now=_NOW,
        )
        assert window.config_old_enough is True
        mapping = {
            "stale": (_NOW - timedelta(days=45)).isoformat(),
            "fresh": (_NOW - timedelta(days=1)).isoformat(),
        }
        result = collect_stale_names(
            ["stale", "fresh"],
            skip=set(),
            get_last_used_at=self._lookup(mapping),
            stale_cutoff=window.stale_cutoff,
            config_old_enough=window.config_old_enough,
        )
        assert result == ["stale"]
