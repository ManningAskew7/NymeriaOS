"""RAG router helper regressions (optimization slice 10 F4/F6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.api.routers.rag import _memory_db_path, _rag_settings_payload


def _agent(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(data_dir=data_dir))


def test_memory_db_path_builds_canonical_layout(tmp_path: Path):
    agent = _agent(tmp_path)
    assert _memory_db_path(agent, "alice") == tmp_path / "users" / "alice" / "memory.db"
    # Allowed characters (alphanumeric, dash, underscore) survive verbatim.
    assert (
        _memory_db_path(agent, "user-1_X")
        == tmp_path / "users" / "user-1_X" / "memory.db"
    )


def test_memory_db_path_sanitizes_traversal_and_empty_ids(tmp_path: Path):
    agent = _agent(tmp_path)
    # Path separators and dots are stripped, so the result can never escape the
    # per-user directory.
    sanitized = _memory_db_path(agent, "../../etc/passwd")
    assert sanitized == tmp_path / "users" / "etcpasswd" / "memory.db"
    assert ".." not in sanitized.parts
    # Empty / all-stripped ids fall back to the shared "default" bucket.
    assert _memory_db_path(agent, "") == tmp_path / "users" / "default" / "memory.db"
    assert _memory_db_path(agent, "///") == tmp_path / "users" / "default" / "memory.db"


def _profile(*, rag_enabled: bool, prefs: dict) -> SimpleNamespace:
    return SimpleNamespace(
        opt_in=SimpleNamespace(rag_enabled=rag_enabled),
        get_rag_preferences=lambda: prefs,
    )


def test_rag_settings_payload_uses_defaults_when_prefs_empty():
    profile = _profile(rag_enabled=True, prefs={})
    settings = SimpleNamespace(rag_retrieval_mode="hybrid", rag_rerank_enabled=False)

    assert _rag_settings_payload(profile, settings) == {
        "enabled": True,
        "max_chunks": 5,
        "include_conversations": True,
        "include_memories": False,
        "include_todos": True,
        "include_tools": True,
        "auto_flush": True,
        "retrieval_mode": "hybrid",
        "rerank_enabled": False,
    }


def test_rag_settings_payload_applies_overrides():
    prefs = {
        "max_chunks": 8,
        "include_memories": True,
        "include_tools": False,
        "retrieval_mode": "vector",
        "rerank_enabled": True,
    }
    profile = _profile(rag_enabled=False, prefs=prefs)
    settings = SimpleNamespace(rag_retrieval_mode="hybrid", rag_rerank_enabled=False)

    payload = _rag_settings_payload(profile, settings)
    assert payload["enabled"] is False
    assert payload["max_chunks"] == 8
    assert payload["include_memories"] is True
    assert payload["include_tools"] is False
    # Per-user override wins over the global default.
    assert payload["retrieval_mode"] == "vector"
    assert payload["rerank_enabled"] is True


def test_rag_settings_payload_rerank_falls_back_to_settings_default():
    # rerank_enabled absent from prefs -> the settings default is used.
    profile = _profile(rag_enabled=True, prefs={})
    settings = SimpleNamespace(rag_retrieval_mode="hybrid", rag_rerank_enabled=True)
    assert _rag_settings_payload(profile, settings)["rerank_enabled"] is True


@pytest.mark.parametrize("operation", ["search", "reindex", "clear"])
def test_rag_requests_embed_off_loop_and_preserve_results(operation, tmp_path, monkeypatch):
    import asyncio
    import threading

    import httpx
    from fastapi import FastAPI

    from nymeria.api.routers.rag import create_rag_router
    from nymeria.core.memory_index import MemoryIndex

    path = tmp_path / "users" / "u1" / "memory.db"
    path.parent.mkdir(parents=True)
    index = MemoryIndex(path, embedding_provider="none")
    index.add_chunk("meeting: Tuesday", {"key": "meeting"}, "memory", "u1")
    index.add_chunk("keep conversation", {}, "conversation", "u1", "t1")
    embed_threads = []
    loop_thread = threading.get_ident()

    def embed(self, texts, input_type):
        embed_threads.append(threading.get_ident())
        return [None] * len(texts)

    monkeypatch.setattr(MemoryIndex, "_embed_batch", embed)
    profile = _profile(rag_enabled=True, prefs={"include_memories": True})
    profile.memories = [SimpleNamespace(key="meeting", value="Wednesday")]
    host = _agent(tmp_path)
    host.profile_manager = SimpleNamespace(get_profile=lambda uid: profile)
    host._get_memory_index = lambda uid: index

    async def user():
        return None

    app = FastAPI()
    app.include_router(create_rag_router(user, lambda: host, lambda *args: None))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            if operation == "search":
                response = await client.get("/users/u1/rag/search?q=meeting")
                assert response.status_code == 200
                assert [result["content"] for result in response.json()["results"]] == ["meeting: Tuesday"]
                assert response.json()["results"][0]["metadata"]["key"] == "meeting"
            elif operation == "reindex":
                response = await client.post("/users/u1/rag/reindex")
                assert response.status_code == 200
                assert response.json()["indexed_memories"] == 1
                assert response.json()["cleared_memory_chunks"] == 1
            else:
                from nymeria.core.embedding_jobs import schedule_embedding_job, wait_for_pending_embedding_jobs

                entered = threading.Event()
                release = threading.Event()

                def blocked():
                    entered.set()
                    assert release.wait(5)

                schedule_embedding_job(blocked, site="test.rag.blocked")
                try:
                    assert await asyncio.to_thread(entered.wait, 5)
                    schedule_embedding_job(index.add_chunk, "pending tail", {}, "conversation", "u1", "t1",
                                           site="test.rag.tail", thread_key="t1")
                    clearing = asyncio.create_task(client.delete("/users/u1/rag/index"))
                    await asyncio.sleep(0)
                finally:
                    release.set()
                response = await clearing
                await wait_for_pending_embedding_jobs()
                assert response.status_code == 200
                assert response.json()["cleared_chunks"] == 3

    try:
        asyncio.run(exercise())
        assert embed_threads and all(tid != loop_thread for tid in embed_threads)
        if operation == "reindex":
            rows = index._get_connection().execute("SELECT content FROM chunks ORDER BY content").fetchall()
            assert [row["content"] for row in rows] == ["keep conversation", "meeting: Wednesday"]
        elif operation == "clear":
            assert index.get_stats("u1")["total_chunks"] == 0
    finally:
        index.close()
