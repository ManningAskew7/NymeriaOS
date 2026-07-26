"""Persistence of tool embeddings across process restarts.

These prove the fix for the cold-start hang: once the catalog is embedded and
persisted, a fresh ``ToolSearchIndex`` (a restarted process) loads vectors from
disk and never re-embeds the whole catalog inline.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from nymeria.core.tool_search_index import ToolSearchIndex
from nymeria.tools.metadata import (
    CUSTOM_TOOL_METADATA,
    register_custom_tool_metadata,
    unregister_custom_tool_metadata,
)


def _fake_embedder(dim: int = 8):
    def embed_one(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[:dim]]

    return embed_one


def _index(db: Path) -> ToolSearchIndex:
    index = ToolSearchIndex(
        embedding_api_key="sk-real-looking-key",
        embedding_dimensions=8,
        db_path=db,
    )
    index._semantic_available = True
    return index


def test_cold_start_loads_from_disk_without_reembedding(tmp_path: Path):
    db = tmp_path / "tools.db"
    embed = _fake_embedder()

    # Process A: warm with a working embedder; vectors persist to disk.
    a = _index(db)
    a._embed_texts = lambda texts: [embed(t) for t in texts]
    a._embed = lambda text: embed(text)
    a.warm_embeddings()
    assert a._fully_embedded
    assert a._store is not None and a._store.count() > 0

    # Process B (a "restart"): same db, but its batch embedder RAISES. It must
    # reach a fully-embedded state purely by loading from disk.
    b = _index(db)

    def boom_texts(_texts):
        raise AssertionError("a restart must load from disk, not re-embed")

    b._embed_texts = boom_texts
    b._embed = lambda text: embed(text)  # query embed is allowed
    b.warm_embeddings()
    assert b._fully_embedded

    response = b.search("browser", user_id="default", top_k=5)
    assert response.mode == "semantic"


def test_partial_disk_then_delta_embed(tmp_path: Path):
    db = tmp_path / "tools.db"
    embed = _fake_embedder()

    a = _index(db)
    a._embed_texts = lambda texts: [embed(t) for t in texts]
    a._embed = lambda text: embed(text)
    a.warm_embeddings()
    base_count = a._store.count()
    assert base_count > 0

    # A new tool appears after the persisted warm. A fresh index loads the
    # existing vectors from disk and embeds ONLY the new tool.
    tool_id = "zz_persist_probe"
    old = CUSTOM_TOOL_METADATA.get(tool_id)
    register_custom_tool_metadata(tool_id, "Persist probe tool")
    try:
        b = _index(db)
        seen = {"count": 0}

        def embed_texts(texts):
            seen["count"] += len(texts)
            return [embed(t) for t in texts]

        b._embed_texts = embed_texts
        b._embed = lambda text: embed(text)
        b.warm_embeddings()

        assert b._fully_embedded
        assert 1 <= seen["count"] <= 3  # only the delta, not the catalog
        assert b._store.count() >= base_count + 1
    finally:
        unregister_custom_tool_metadata(tool_id)
        if old is not None:
            CUSTOM_TOOL_METADATA[tool_id] = old
