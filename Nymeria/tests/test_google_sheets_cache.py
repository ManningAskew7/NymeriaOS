"""Unit tests for the bounded Google Sheets cache (slice 16 F6).

The module cache is now an LRU-bounded ``OrderedDict``: writes evict the
least-recently-used entry over the size cap, and both reads and rewrites bump
recency.
"""

import pytest

from nymeria.tools import google_sheets as gs


@pytest.fixture(autouse=True)
def small_cache(monkeypatch):
    monkeypatch.setattr(gs, "_CACHE_MAX_ENTRIES", 3)
    gs._sheet_cache.clear()
    yield
    gs._sheet_cache.clear()


def _entry():
    return (0.0, [], [])


def test_cache_store_evicts_oldest_over_cap():
    for i in range(3):
        gs._cache_store(f"k{i}", _entry())
    assert list(gs._sheet_cache.keys()) == ["k0", "k1", "k2"]

    gs._cache_store("k3", _entry())

    assert "k0" not in gs._sheet_cache
    assert list(gs._sheet_cache.keys()) == ["k1", "k2", "k3"]


def test_cache_store_rewrite_refreshes_recency():
    for i in range(3):
        gs._cache_store(f"k{i}", _entry())

    # Rewriting k0 makes it most-recent, so the next eviction drops k1.
    gs._cache_store("k0", (1.0, [], []))
    gs._cache_store("k3", _entry())

    assert "k1" not in gs._sheet_cache
    assert "k0" in gs._sheet_cache
    assert list(gs._sheet_cache.keys()) == ["k2", "k0", "k3"]


def test_cache_never_exceeds_cap():
    for i in range(20):
        gs._cache_store(f"k{i}", _entry())
    assert len(gs._sheet_cache) == 3
    # Only the last three keys survive.
    assert list(gs._sheet_cache.keys()) == ["k17", "k18", "k19"]
