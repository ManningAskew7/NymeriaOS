"""Tests for the production RAG stack wiring.

Covers the additions that let the install wizard's premium/value/local RAG
choices actually power retrieval: native embedder/reranker config and configurable
vector width in ``memory_index``, the production reranker dispatch in
``rag_quality``, tool-result embedding + hardened ingest dedup, and the
``nymeria init`` embedder/reranker steps + catalog. No network or real models.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from nymeria.config.settings import Settings
from nymeria.core import rag_quality
from nymeria.core.memory_index import MemoryIndex, _canonical_json
from nymeria.setup.rag_catalog import (
    EMBEDDERS,
    RERANKERS,
    apply_quickstart_rag,
    rag_env_for_state,
)
from nymeria.setup.state import WizardState


# --- settings: new knobs, defaults preserve existing behavior ---------------


def test_rag_settings_defaults_preserve_existing_behavior():
    s = Settings()
    assert s.embedding_provider == "openai"
    assert s.embedding_dimensions is None
    assert s.embedding_input_type is None
    assert s.rag_rerank_enabled is False
    assert s.rag_rerank_provider == "llm"
    assert s.rag_rerank_model is None
    assert s.rag_rerank_api_key is None
    # The one intentional new default-on behavior: tool results are embedded.
    assert s.rag_embed_tool_results is True


# --- ingest dedup: canonical JSON + cross-thread tool dedup ------------------


def test_canonical_json_collapses_key_order_and_whitespace():
    assert _canonical_json('{"b": 1, "a": 2}') == _canonical_json('{ "a":2,\n"b":1 }')
    assert _canonical_json('{"b": 1, "a": 2}') is not None
    assert _canonical_json("not json") is None
    assert _canonical_json("look: {not json}") is None


def test_content_hash_collapses_json_variants():
    assert MemoryIndex._content_hash('{"b":1,"a":2}') == MemoryIndex._content_hash(
        '{"a": 2, "b": 1}'
    )


def test_tool_chunk_dedups_cross_thread_while_conversation_is_thread_scoped():
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "m.db", embedding_provider="none")
        first = idx.add_chunk('{"status":"ok","items":[1,2,3]}', {}, "tool", "u1", thread_id="t1")
        # Same payload, different key order, different thread -> deduped (cross-thread,
        # canonical-JSON).
        dup = idx.add_chunk('{"items":[1,2,3],"status":"ok"}', {}, "tool", "u1", thread_id="t2")
        assert first and not dup

        # Conversation dedup stays thread-scoped: same prose in a new thread is kept.
        a = idx.add_chunk("hello world", {}, "conversation", "u1", thread_id="t1")
        b = idx.add_chunk("hello world", {}, "conversation", "u1", thread_id="t2")
        c = idx.add_chunk("hello world", {}, "conversation", "u1", thread_id="t1")
        assert a and b and not c


# --- configurable vector width + dimension-change detection -----------------


def test_vec_width_follows_dimensions_and_mismatch_is_detected(monkeypatch):
    pytest.importorskip("sqlite_vec")
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.db"
        idx = MemoryIndex(path, embedding_provider="none", embedding_dimensions=4)
        assert idx.embedding_dimensions == 4
        monkeypatch.setattr(
            idx, "embed_text", lambda text, input_type="document": [0.1, 0.2, 0.3, 0.4]
        )
        assert idx.add_chunk("alpha", {}, "memory", "u1")
        if idx._stored_vec_dim(idx._get_connection().cursor()) is None:
            pytest.skip("vec0 not available to store vectors")

        # Reopen at a different width: flagged loudly, never silently corrupted.
        assert MemoryIndex(path, embedding_provider="none", embedding_dimensions=8)._dim_mismatch
        assert not MemoryIndex(path, embedding_provider="none", embedding_dimensions=4)._dim_mismatch


# --- production reranker dispatch (pure, no network) -------------------------


def test_rerank_order_parsers_handle_both_response_shapes():
    # Cohere/ZeroEntropy: results[].index. Voyage: data[].index.
    assert rag_quality._parse_rerank_order(
        {"results": [{"index": 2}, {"index": 0}, {"index": 1}]}, 3
    ) == [2, 0, 1]
    assert rag_quality._parse_rerank_order({"data": [{"index": 1}, {"index": 0}]}, 2) == [1, 0]
    # Score-only entries -> argsort descending, with missing indices backfilled.
    assert rag_quality._parse_rerank_order(
        {"results": [{"relevance_score": 0.1}, {"relevance_score": 0.9}]}, 2
    ) == [1, 0]


def test_rerankers_noop_when_unconfigured():
    class _R:
        def __init__(self, content):
            self.content = content

    results = [_R("a"), _R("b"), _R("c")]
    # Managed reranker with no key, or unknown provider -> input unchanged.
    assert rag_quality.api_rerank("voyage", "rerank-2.5", None, "q", results) is results
    assert rag_quality.api_rerank("bogus", "m", "k", "q", results) is results
    # Local reranker with no model id -> input unchanged.
    assert rag_quality.local_rerank(None, "q", results) is results


# --- install-wizard RAG catalog + steps -------------------------------------


def test_rag_catalog_options_are_well_formed():
    for e in EMBEDDERS:
        assert e.provider in {"openai", "cohere", "gemini", "local"}
        assert e.dimensions > 0
        if e.requires_key:
            assert e.key_vendor
    for r in RERANKERS:
        assert r.provider in {"voyage", "cohere", "zeroentropy", "local", "none"}
        if r.requires_key:
            assert r.key_vendor


def test_rag_env_for_state_maps_each_tier():
    s = WizardState()
    s.embedder, s.reranker = "value-gemini", "value-voyage-2.5-lite"
    env = rag_env_for_state(s)
    assert env["EMBEDDING_PROVIDER"] == "gemini"
    assert env["EMBEDDING_DIMENSIONS"] == "1024"
    assert env["RAG_RERANK_ENABLED"] == "true"
    assert env["RAG_RERANK_PROVIDER"] == "voyage"
    assert env["RAG_RERANK_MODEL"] == "rerank-2.5-lite"

    voyage = WizardState()
    voyage.embedder = "value-voyage-lite"
    venv = rag_env_for_state(voyage)
    assert venv["EMBEDDING_PROVIDER"] == "openai"
    assert venv["EMBEDDING_BASE_URL"] == "https://api.voyageai.com/v1"
    assert venv["EMBEDDING_INPUT_TYPE"] == "voyage"

    none_rr = WizardState()
    none_rr.embedder, none_rr.reranker = "local-granite", "none"
    nenv = rag_env_for_state(none_rr)
    assert nenv["EMBEDDING_PROVIDER"] == "local"
    assert nenv["RAG_RERANK_ENABLED"] == "false"

    # No embedder chosen -> no RAG config written (unchanged default behavior).
    assert rag_env_for_state(WizardState()) == {}


def test_quickstart_equips_local_stack_without_keys():
    s = WizardState()
    apply_quickstart_rag(s)
    env = rag_env_for_state(s)
    assert env["EMBEDDING_PROVIDER"] == "local"
    assert env["RAG_RERANK_PROVIDER"] == "local"
    assert "EMBEDDING_API_KEY" not in s.optional_env
    assert "RAG_RERANK_API_KEY" not in s.optional_env


def test_wizard_includes_rag_steps_and_reranker_is_gated_on_embedder():
    from nymeria.setup.steps import build_default_steps

    steps = {s.id: s for s in build_default_steps()}
    assert "embedder" in steps
    assert "reranker" in steps
    assert "rag_search" not in steps  # placeholder retired

    reranker = steps["reranker"]
    assert reranker.applies(WizardState()) is False
    chosen = WizardState()
    chosen.embedder = "local-granite"
    assert reranker.applies(chosen) is True


def test_write_config_emits_rag_env_and_keys(tmp_path):
    from nymeria.setup import finalize as F

    s = WizardState()
    s.embedder, s.reranker = "value-gemini", "value-voyage-2.5-lite"
    s.optional_env["EMBEDDING_API_KEY"] = "gem-key"
    s.optional_env["RAG_RERANK_API_KEY"] = "voy-key"

    cfg = tmp_path / "config.env"
    F.write_config(
        cfg,
        data_dir=tmp_path / "data",
        spec=None,
        model="",
        api_key="",
        optional_env=F._resolve_optional_env(s, spec=None, api_key=""),
        extra_env=F._resolve_extra_env(s),
    )
    text = cfg.read_text()
    for expected in (
        "EMBEDDING_PROVIDER=gemini",
        "EMBEDDING_MODEL=gemini-embedding-001",
        "EMBEDDING_DIMENSIONS=1024",
        "RAG_RERANK_ENABLED=true",
        "RAG_RERANK_PROVIDER=voyage",
        "RAG_RERANK_MODEL=rerank-2.5-lite",
        "EMBEDDING_API_KEY=gem-key",
        "RAG_RERANK_API_KEY=voy-key",
    ):
        assert expected in text, expected


def test_rebuild_vectors_reembeds_at_new_width(monkeypatch):
    pytest.importorskip("sqlite_vec")
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "m.db"
        idx4 = MemoryIndex(path, embedding_provider="none", embedding_dimensions=4)
        monkeypatch.setattr(
            idx4, "embed_text", lambda text, input_type="document": [0.1, 0.2, 0.3, 0.4]
        )
        idx4.add_chunk("alpha", {}, "memory", "u1")
        idx4.add_chunk("beta", {}, "memory", "u1")

        # Reopen configured for a different width: mismatch flagged.
        idx8 = MemoryIndex(path, embedding_provider="none", embedding_dimensions=8)
        if idx8._stored_vec_dim(idx8._get_connection().cursor()) is None:
            pytest.skip("vec0 not available to store vectors")
        assert idx8._dim_mismatch
        monkeypatch.setattr(
            idx8, "_embed_texts",
            lambda texts, input_type="document": [[0.0] * 8 for _ in texts],
        )
        result = idx8.rebuild_vectors()
        assert idx8._dim_mismatch is False
        assert result["total"] == 2 and result["embedded"] == 2
        assert idx8._stored_vec_dim(idx8._get_connection().cursor()) == 8
