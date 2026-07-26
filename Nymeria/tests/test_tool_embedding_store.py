from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.core.tool_embedding_store import ToolEmbeddingStore


def test_put_get_roundtrip(tmp_path: Path):
    store = ToolEmbeddingStore(tmp_path / "t.db", model="m", dimensions=4)
    store.put_many([("h1", [0.1, 0.2, 0.3, 0.4]), ("h2", [1.0, 0.0, -1.0, 0.5])])

    got = store.get_many(["h1", "h2", "missing"])

    assert set(got) == {"h1", "h2"}
    assert got["h1"] == pytest.approx([0.1, 0.2, 0.3, 0.4], abs=1e-6)
    assert got["h2"] == pytest.approx([1.0, 0.0, -1.0, 0.5], abs=1e-6)
    assert store.count() == 2


def test_model_change_wipes_cache(tmp_path: Path):
    db = tmp_path / "t.db"
    first = ToolEmbeddingStore(db, model="model-a", dimensions=4)
    first.put_many([("h1", [0.1, 0.2, 0.3, 0.4])])
    assert first.count() == 1

    # Reopen with a different model: stored vectors are from another embedding
    # space, so they must be dropped rather than mixed.
    second = ToolEmbeddingStore(db, model="model-b", dimensions=4)
    assert second.count() == 0
    assert second.get_many(["h1"]) == {}


def test_dimension_change_wipes_cache(tmp_path: Path):
    db = tmp_path / "t.db"
    first = ToolEmbeddingStore(db, model="m", dimensions=4)
    first.put_many([("h1", [0.1, 0.2, 0.3, 0.4])])
    assert first.count() == 1

    second = ToolEmbeddingStore(db, model="m", dimensions=8)
    assert second.count() == 0


def test_put_many_rejects_wrong_width(tmp_path: Path):
    store = ToolEmbeddingStore(tmp_path / "t.db", model="m", dimensions=4)
    store.put_many([("good", [0.1, 0.2, 0.3, 0.4]), ("bad", [0.1, 0.2])])

    got = store.get_many(["good", "bad"])
    assert "good" in got
    assert "bad" not in got


def test_two_instances_share_one_file_without_locking(tmp_path: Path):
    db = tmp_path / "t.db"
    a = ToolEmbeddingStore(db, model="m", dimensions=4)
    b = ToolEmbeddingStore(db, model="m", dimensions=4)

    a.put_many([("h1", [0.1, 0.2, 0.3, 0.4])])
    b.put_many([("h2", [0.5, 0.6, 0.7, 0.8])])

    # WAL + busy_timeout + INSERT OR REPLACE: concurrent readers/writers on one
    # file must not raise "database is locked".
    assert set(a.get_many(["h1", "h2"])) == {"h1", "h2"}
    assert set(b.get_many(["h1", "h2"])) == {"h1", "h2"}


def test_unusable_store_degrades_to_noop(tmp_path: Path):
    # Point the db at a path whose parent is a file, so the store can't init.
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    store = ToolEmbeddingStore(blocker / "nested.db", model="m", dimensions=4)

    assert store.usable is False
    # Every operation is a safe no-op when the store is unusable.
    store.put_many([("h1", [0.1, 0.2, 0.3, 0.4])])
    assert store.get_many(["h1"]) == {}
    assert store.count() == 0


def test_provider_change_wipes_cache(tmp_path: Path):
    db = tmp_path / "t.db"
    first = ToolEmbeddingStore(db, model="m", dimensions=4, provider="openai")
    first.put_many([("h1", [0.1, 0.2, 0.3, 0.4])])
    assert first.count() == 1

    # Same model name through a different provider is a different embedding
    # space; the cache must not mix them.
    second = ToolEmbeddingStore(db, model="m", dimensions=4, provider="local")
    assert second.count() == 0


def test_legacy_store_without_provider_stamp_survives_as_openai(tmp_path: Path):
    """Pre-provider-stamp stores (only openai existed then) must NOT be wiped
    by the upgrade: a missing stored provider reads as 'openai'."""
    import sqlite3

    db = tmp_path / "t.db"
    first = ToolEmbeddingStore(db, model="m", dimensions=4)
    first.put_many([("h1", [0.1, 0.2, 0.3, 0.4])])
    # Simulate a legacy store: remove the provider stamp the constructor wrote.
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM meta WHERE key = 'provider'")
        conn.commit()

    second = ToolEmbeddingStore(db, model="m", dimensions=4, provider="openai")
    assert second.count() == 1
    assert second.get_many(["h1"])
