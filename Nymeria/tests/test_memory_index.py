"""Tests for RAG memory-index maintenance helpers."""

from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace

from nymeria.core.memory_index import EMBEDDING_DIMENSIONS, MemoryIndex


def test_delete_memory_key_removes_only_matching_memory_chunks():
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db", embedding_provider="none")
        index.add_chunk("alpha: first saved memory", {"key": "alpha"}, "memory", "user-1")
        index.add_chunk("beta: second saved memory", {"key": "beta"}, "memory", "user-1")
        index.add_chunk("alpha project conversation", {}, "conversation", "user-1", thread_id="thread-1")

        assert index.delete_memory_key("user-1", "alpha") == 1

        stats = index.get_stats("user-1")
        assert stats["by_type"] == {"conversation": 1, "memory": 1}
        assert [result.content for result in index.search("beta", "user-1", chunk_types=["memory"])] == [
            "beta: second saved memory"
        ]
        assert index.search("alpha", "user-1", chunk_types=["memory"]) == []


def test_delete_by_type_removes_all_memory_chunks_without_touching_conversations():
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db", embedding_provider="none")
        index.add_chunk("alpha: first saved memory", {"key": "alpha"}, "memory", "user-1")
        index.add_chunk("beta: second saved memory", {"key": "beta"}, "memory", "user-1")
        index.add_chunk("alpha project conversation", {}, "conversation", "user-1", thread_id="thread-1")

        assert index.delete_by_type("user-1", "memory") == 2

        stats = index.get_stats("user-1")
        assert stats["by_type"] == {"conversation": 1}
        assert index.search("alpha", "user-1", chunk_types=["conversation"])[0].content == "alpha project conversation"


def test_search_records_retrieval_diagnostics_for_footer():
    """search() stashes per-call diagnostics (which branches ran, embedder
    identity, candidate counts) on the instance so rag_search can confirm the
    live retrieval stack. With embedding_provider='none' the vector branch
    cannot run, so the diag reports BM25-only."""
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db", embedding_provider="none")
        index.add_chunk("alpha project budget meeting notes", {}, "conversation",
                        "u1", thread_id="t1")
        index.add_chunk("beta unrelated grocery list", {}, "memory", "u1")

        results = index.search("budget", "u1")

        assert results  # BM25 still finds the budget chunk
        diag = index._last_search_diag
        assert diag is not None
        assert diag["embedding_provider"] == "none"
        assert diag["vector_used"] is False        # 'none' provider -> no vector
        assert diag["bm25_used"] is True
        assert diag["bm25_candidates"] >= 1
        assert diag["candidate_pool"] >= 30
        assert diag["retrieval_mode"] == "hybrid"


def test_search_diagnostics_reset_on_empty_query():
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db", embedding_provider="none")
        index.add_chunk("alpha", {}, "memory", "u1")
        index.search("alpha", "u1")
        assert index._last_search_diag is not None
        index.search("   ", "u1")  # blank query returns early
        assert index._last_search_diag is None


def test_connection_is_cached_and_reopens_after_close():
    """F2: the SQLite connection (with sqlite-vec loaded) is created once and
    reused across calls; close() releases it and the next call transparently
    reopens, so operations keep working."""
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db", embedding_provider="none")
        c1 = index._get_connection()
        assert index._get_connection() is c1  # reused, not reopened

        index.close()
        assert index._conn is None

        c2 = index._get_connection()
        assert c2 is not c1  # transparently reopened after close

        # Operations still work after a close (recreate-if-closed guard).
        index.add_chunk("hello world memory", {"key": "k"}, "memory", "u1")
        assert index.get_stats("u1")["total_chunks"] == 1
        index.close()


def test_add_chunk_batches_embeddings_for_multichunk_content(monkeypatch):
    """F4: a multi-chunk add embeds chunk 0 via the per-item path and chunks
    2..N in a single batched _embed_texts call (not one round-trip per chunk)."""
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db", embedding_provider="none")
        # Force a deterministic 3-chunk split.
        monkeypatch.setattr(index, "_chunk_text",
                            lambda content: ["chunk a", "chunk b", "chunk c"])

        single = {"n": 0}
        batch = {"n": 0, "sizes": []}

        def fake_embed_text(text, input_type="document"):
            single["n"] += 1
            return [0.1] * EMBEDDING_DIMENSIONS

        def fake_embed_texts(texts, input_type="document"):
            batch["n"] += 1
            batch["sizes"].append(len(texts))
            return [[0.1] * EMBEDDING_DIMENSIONS for _ in texts]

        monkeypatch.setattr(index, "embed_text", fake_embed_text)
        monkeypatch.setattr(index, "_embed_texts", fake_embed_texts)

        ids = index.add_chunk("long content", {}, "conversation", "u1", thread_id="t1")

        assert len(ids) == 3
        assert single["n"] == 1          # chunk 0 only
        assert batch["n"] == 1           # one batched call for chunks 2..N
        assert batch["sizes"] == [2]     # the remaining two chunks
        assert index.get_stats("u1")["vector_count"] == 3


def test_delete_chunks_batches_over_param_cap_and_clears_vectors(monkeypatch):
    """F5: deleting more ids than SQLite's bound-parameter cap works (batched
    IN(...) deletes) and removes the matching vectors with no orphans."""
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db",
                            embedding_provider="openai", embedding_api_key="test")
        monkeypatch.setattr(
            index, "embed_text",
            lambda text, input_type="document": [0.1] * EMBEDDING_DIMENSIONS,
        )

        ids = []
        for i in range(1050):  # exceeds the 500-id batch and the 999 var cap
            ids += index.add_chunk(f"unique chunk {i}", {}, "conversation",
                                   "u1", thread_id="t1")
        assert len(ids) == 1050
        assert index.get_stats("u1")["vector_count"] == 1050

        assert index.delete_chunks(ids) == 1050
        stats = index.get_stats("u1")
        assert stats["total_chunks"] == 0
        assert stats["vector_count"] == 0  # vec rows deleted, no orphans


def test_delete_memory_key_ignores_keyless_and_missing_keys():
    """F10: the json_extract filter deletes only the matching key, leaves
    keyless memory chunks alone, and matches nothing for an absent key."""
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db", embedding_provider="none")
        index.add_chunk("alpha fact", {"key": "alpha"}, "memory", "u1")
        index.add_chunk("keyless memory fact", {}, "memory", "u1")  # no 'key'
        index.add_chunk("beta fact", {"key": "beta"}, "memory", "u1")

        assert index.delete_memory_key("u1", "alpha") == 1
        assert index.get_stats("u1")["by_type"] == {"memory": 2}

        # An absent key deletes nothing (json_extract yields no match).
        assert index.delete_memory_key("u1", "ghost") == 0
        assert index.get_stats("u1")["by_type"] == {"memory": 2}


def test_delete_memory_key_tolerates_malformed_metadata():
    """F10 robustness: a row with malformed JSON metadata is skipped by the
    json_valid guard rather than aborting the whole delete (matching the old
    per-row try/except json.loads behaviour)."""
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db", embedding_provider="none")
        index.add_chunk("alpha fact", {"key": "alpha"}, "memory", "u1")
        # Inject a memory chunk with non-JSON metadata directly.
        conn = index._get_connection()
        conn.execute(
            "INSERT INTO chunks (id, user_id, content, chunk_type, metadata, content_hash) "
            "VALUES (?, ?, ?, 'memory', ?, ?)",
            ("bad-1", "u1", "corrupt row", "{not valid json", "h"),
        )
        conn.commit()

        # Does not raise; deletes only the valid matching row.
        assert index.delete_memory_key("u1", "alpha") == 1
        # The malformed row survives (it was never a match).
        assert index.get_stats("u1")["by_type"] == {"memory": 1}


def test_tools_memory_caches_index_per_user(monkeypatch):
    """The memory tools reuse one MemoryIndex per user (and rebuild only when the
    data dir is repointed), instead of constructing a fresh index per call."""
    from nymeria.tools import memory as mem

    with TemporaryDirectory() as tmpdir:
        mem._reset_memory_index_cache()
        profile = SimpleNamespace(opt_in=SimpleNamespace(rag_enabled=True))
        monkeypatch.setattr(
            mem, "_get_profile_manager",
            lambda: SimpleNamespace(get_profile=lambda uid: profile),
        )
        monkeypatch.setattr(
            "nymeria.config.get_settings",
            lambda: SimpleNamespace(data_dir=Path(tmpdir)),
        )

        a = mem._get_memory_index("u1")
        assert a is not None
        assert mem._get_memory_index("u1") is a       # reused
        assert mem._get_memory_index("u2") is not a   # per-user

        mem._reset_memory_index_cache()
        assert mem._get_memory_index("u1") is not a   # rebuilt after reset
        mem._reset_memory_index_cache()


def test_team_memory_chunks_delete_by_composite_key_and_team():
    """team_memory chunks (backlog #100 phase 3): per-key replace + team wipe.

    Exercises the real SQL paths: delete_memory_key with chunk_type filters on
    the composite "<team_id>:<key>" metadata key without touching profile
    "memory" chunks or other teams, and delete_team_memory_chunks clears one
    team wholesale via the metadata team_id.
    """
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(Path(tmpdir) / "memory.db", embedding_provider="none")
        # Profile chunk with a colliding bare key name.
        index.add_chunk("endpoint: profile value", {"key": "endpoint"}, "memory", "user-1")
        # Two teams sharing a key name, plus a second key on team A.
        index.add_chunk(
            "endpoint: team A value",
            {"key": "team-a:endpoint", "team_id": "team-a", "team_key": "endpoint"},
            "team_memory",
            "user-1",
        )
        index.add_chunk(
            "endpoint: team B value",
            {"key": "team-b:endpoint", "team_id": "team-b", "team_key": "endpoint"},
            "team_memory",
            "user-1",
        )
        index.add_chunk(
            "runbook: team A runbook",
            {"key": "team-a:runbook", "team_id": "team-a", "team_key": "runbook"},
            "team_memory",
            "user-1",
        )

        # Keyed delete hits exactly the one composite key.
        assert index.delete_memory_key("user-1", "team-a:endpoint", chunk_type="team_memory") == 1
        stats = index.get_stats("user-1")
        assert stats["by_type"] == {"memory": 1, "team_memory": 2}

        # The default chunk_type still means profile memory only.
        assert index.delete_memory_key("user-1", "endpoint") == 1
        assert index.get_stats("user-1")["by_type"] == {"team_memory": 2}

        # Team wipe removes only that team's chunks.
        assert index.delete_team_memory_chunks("user-1", "team-a") == 1
        remaining = index.search("endpoint", "user-1", chunk_types=["team_memory"])
        assert [result.content for result in remaining] == ["endpoint: team B value"]
        # Other users are never touched by construction (user_id predicate).
        assert index.delete_team_memory_chunks("user-2", "team-b") == 0
