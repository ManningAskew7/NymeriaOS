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
