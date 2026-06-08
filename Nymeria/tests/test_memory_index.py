"""Tests for RAG memory-index maintenance helpers."""

from __future__ import annotations

from tempfile import TemporaryDirectory
from pathlib import Path

from nymeria.core.memory_index import MemoryIndex


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
