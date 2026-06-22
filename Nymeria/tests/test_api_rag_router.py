"""RAG router helper regressions (optimization slice 10 F4/F6)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
