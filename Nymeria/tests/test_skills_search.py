"""Tests for SkillEmbeddingIndex — focus on the non-network fallback paths.

The semantic path calls OpenAI so we keep it out of the default suite; the
BM25 / FTS5 and substring fallbacks can be verified with no network.

Run with:
    docker exec nymeria-api python -m pytest /app/tests/test_skills_search.py -v
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

import pytest

from nymeria.skills.embedding_index import SkillEmbeddingIndex


def _env_has_openai_key() -> bool:
    return bool(os.environ.get("EMBEDDING_API_KEY"))


@dataclass
class FakeSkill:
    name: str
    description: str
    scope: str = "user"
    allowed_tools: List[str] = None  # type: ignore

    def __post_init__(self):
        if self.allowed_tools is None:
            self.allowed_tools = []


def _fresh_index(tmp_path: Path) -> SkillEmbeddingIndex:
    # No EMBEDDING_API_KEY => semantic path disabled, BM25/substring remain.
    return SkillEmbeddingIndex(db_path=tmp_path / "skills.db", embedding_api_key=None)


SAMPLE_SKILLS = [
    FakeSkill("pdf", "Extract text and tables from PDFs. Fill interactive forms."),
    FakeSkill("ocr-tool", "Recognize text in images and photographs."),
    FakeSkill("skill-creator", "Create new skills, edit existing skills, measure performance."),
    FakeSkill("email-sender", "Compose and send emails via SMTP."),
]


def test_rebuild_counts(tmp_path):
    idx = _fresh_index(tmp_path)
    summary = idx.rebuild("installed", SAMPLE_SKILLS)
    assert summary["fts_indexed"] == 4
    # Without a real API key the semantic path is skipped.
    assert summary["semantic_indexed"] == 0
    assert summary["semantic_available"] is False
    assert "EMBEDDING_API_KEY" in (summary.get("warning") or "")


def test_bm25_fallback_returns_keyword_matches(tmp_path):
    idx = _fresh_index(tmp_path)
    idx.rebuild("installed", SAMPLE_SKILLS)
    r = idx.search("pdf", namespace="installed", top_k=5)
    assert r.mode == "bm25"
    assert r.results
    assert r.results[0].name == "pdf"
    assert r.warning is not None
    assert "EMBEDDING_API_KEY" in r.warning


def test_cliproxy_gatekeeper_key_does_not_hit_embeddings(tmp_path):
    idx = SkillEmbeddingIndex(db_path=tmp_path / "skills.db", embedding_api_key="cpx-local-test")
    summary = idx.rebuild("installed", SAMPLE_SKILLS)

    assert summary["semantic_indexed"] == 0
    assert "CLIProxy gatekeeper" in (summary.get("warning") or "")


def test_substring_fallback_when_bm25_empty(tmp_path):
    """A query that tokenizes to nothing (e.g. only punctuation) falls
    through to the substring path, not an error."""
    idx = _fresh_index(tmp_path)
    idx.rebuild("installed", SAMPLE_SKILLS)
    # Empty query => no FTS tokens => substring finds everything.
    r = idx.search("", namespace="installed", top_k=10)
    assert r.mode == "substring"
    assert len(r.results) == 4


def test_namespaces_are_isolated(tmp_path):
    idx = _fresh_index(tmp_path)
    idx.rebuild("installed", SAMPLE_SKILLS[:2])  # pdf + ocr-tool
    idx.rebuild("marketplace:anthropic", SAMPLE_SKILLS[2:])  # skill-creator + email-sender

    r_installed = idx.search("pdf", namespace="installed", top_k=5)
    assert {h.name for h in r_installed.results} == {"pdf"}

    r_mp = idx.search("skill", namespace="marketplace:anthropic", top_k=5)
    assert r_mp.results
    assert any(h.name == "skill-creator" for h in r_mp.results)
    # Installed namespace must not leak into the marketplace search.
    assert not any(h.name == "pdf" for h in r_mp.results)


def test_clear_namespace_removes_rows(tmp_path):
    idx = _fresh_index(tmp_path)
    idx.rebuild("installed", SAMPLE_SKILLS)
    idx.clear_namespace("installed")
    r = idx.search("pdf", namespace="installed", top_k=5)
    assert r.results == []


def test_to_json_shape(tmp_path):
    idx = _fresh_index(tmp_path)
    idx.rebuild("installed", SAMPLE_SKILLS)
    r = idx.search("pdf", namespace="installed", top_k=5)
    out = r.to_json()
    assert set(out.keys()) >= {"count", "mode", "results"}
    assert out["count"] == len(out["results"])
    # When degraded, warning must be present and non-empty.
    assert out["warning"]
    for row in out["results"]:
        assert "name" in row and "description" in row and "score" in row


@pytest.mark.skipif(
    not _env_has_openai_key(),
    reason="semantic path requires EMBEDDING_API_KEY",
)
def test_semantic_search_finds_intent_matches(tmp_path):
    """Only runs when EMBEDDING_API_KEY is present. Proves semantic matching
    can find skills whose names don't share keywords with the query."""
    idx = SkillEmbeddingIndex(
        db_path=tmp_path / "skills.db",
        embedding_api_key=os.environ.get("EMBEDDING_API_KEY"),
        embedding_base_url=os.environ.get("EMBEDDING_BASE_URL"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    idx.rebuild("installed", SAMPLE_SKILLS)
    r = idx.search("read text in pictures", namespace="installed", top_k=3)
    assert r.mode == "semantic"
    assert r.results
    assert r.results[0].name == "ocr-tool"


def test_embedding_client_uses_bounded_timeout_and_no_retries(tmp_path):
    """A slow/unreachable embeddings endpoint must fail fast and degrade to
    FTS5/keyword search instead of stalling the agent turn for the SDK default
    (~600s x 2 retries). Guards against regressing the client construction.
    """
    import openai

    from nymeria.skills.embedding_index import EMBED_REQUEST_TIMEOUT_SECONDS

    captured: dict = {}

    class _RecordingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    original = openai.OpenAI
    openai.OpenAI = _RecordingClient
    try:
        idx = SkillEmbeddingIndex(
            db_path=tmp_path / "skills.db",
            embedding_api_key="sk-real-looking-key",
        )
        idx._client._get_openai_client()
    finally:
        openai.OpenAI = original

    assert captured["timeout"] == EMBED_REQUEST_TIMEOUT_SECONDS
    assert captured["max_retries"] == 0


def test_search_reuses_one_cached_connection(tmp_path):
    """F11: the latency-sensitive search() path reuses a single cached SQLite
    connection (one sqlite-vec load) instead of reopening on every call, while
    the one-shot/bulk ops (init, rebuild) keep their own short-lived connection.
    """
    idx = _fresh_index(tmp_path)
    idx.rebuild("installed", SAMPLE_SKILLS)
    # rebuild + init manage their own short-lived connections, so the cache is
    # still empty after them.
    assert idx._conn is None

    idx.search("pdf", namespace="installed", top_k=5)
    first = idx._conn
    assert first is not None  # search opened and cached a connection

    idx.search("ocr", namespace="installed", top_k=5)
    assert idx._conn is first  # reused, not reopened


def test_close_is_idempotent_and_search_reopens(tmp_path):
    """close() releases the cached connection and is safe to call twice; the
    next search transparently reopens and still returns results."""
    idx = _fresh_index(tmp_path)
    idx.rebuild("installed", SAMPLE_SKILLS)
    idx.search("pdf", namespace="installed", top_k=5)
    before = idx._conn
    assert before is not None

    idx.close()
    assert idx._conn is None
    idx.close()  # idempotent: no error on a second close
    assert idx._conn is None

    r = idx.search("pdf", namespace="installed", top_k=5)
    assert r.results and r.results[0].name == "pdf"
    assert idx._conn is not None  # reopened transparently
    assert idx._conn is not before  # a genuinely new connection, not the closed one


def test_cached_search_connection_sees_later_committed_writes(tmp_path):
    """A search that already opened the cached read connection must still observe
    rows that a later rebuild/clear commits on its own short-lived connection
    (cross-connection visibility; the cached connection reads in autocommit)."""
    idx = _fresh_index(tmp_path)
    idx.rebuild("installed", SAMPLE_SKILLS)

    r1 = idx.search("pdf", namespace="installed", top_k=5)
    assert any(h.name == "pdf" for h in r1.results)
    cached = idx._conn
    assert cached is not None

    # clear_namespace commits a delete on a separate (fresh) connection.
    idx.clear_namespace("installed")
    r2 = idx.search("pdf", namespace="installed", top_k=5)
    assert idx._conn is cached  # same cached read connection, not reopened
    assert r2.results == []  # but it observes the committed delete

    # And a subsequent rebuild's inserts are visible on the same cached conn too.
    idx.rebuild("installed", SAMPLE_SKILLS)
    r3 = idx.search("pdf", namespace="installed", top_k=5)
    assert idx._conn is cached
    assert any(h.name == "pdf" for h in r3.results)


class _FakeLocalEncoder:
    """Deterministic stand-in for a sentence-transformers model (4-d)."""

    def encode(self, inputs, **kwargs):
        import math

        out = []
        for text in inputs:
            t = text.lower()
            v = [
                1.0 + 3.0 * t.count("pdf"),
                1.0 + 3.0 * (t.count("image") + t.count("photograph") + t.count("picture")),
                1.0 + 3.0 * t.count("email"),
                1.0,
            ]
            norm = math.sqrt(sum(x * x for x in v))
            out.append([x / norm for x in v])
        return out


def test_local_provider_semantic_search_needs_no_key(tmp_path, monkeypatch):
    """EMBEDDING_PROVIDER=local (the wizard-recommended granite shape) must get
    semantic skill search with NO key configured. Regression for backlog #101
    entry 13: the old OpenAI-only gate reported 'EMBEDDING_API_KEY not set' and
    disabled semantic search on exactly this install shape."""
    pytest.importorskip("sqlite_vec")
    from nymeria.core.embedding_client import EmbeddingClient

    monkeypatch.setattr(
        EmbeddingClient, "_get_local_embedder", lambda self: _FakeLocalEncoder()
    )
    idx = SkillEmbeddingIndex(
        db_path=tmp_path / "skills.db",
        embedding_provider="local",
        embedding_api_key=None,
        embedding_model="fake-granite",
        embedding_dimensions=4,
    )
    assert idx.is_semantic_available() is True

    summary = idx.rebuild("installed", SAMPLE_SKILLS)
    assert summary["semantic_indexed"] == 4
    assert summary["warning"] is None

    r = idx.search("recognize text in pictures", namespace="installed", top_k=3)
    assert r.mode == "semantic"
    assert r.results[0].name == "ocr-tool"
    assert r.warning is None


def test_embedder_change_wipes_stale_vectors(tmp_path, monkeypatch):
    """Changing the embedding (provider, model, dimensions) drops the stored
    vector table (the vec0 width is fixed at creation and vectors from another
    model are a different space) instead of mixing or erroring."""
    pytest.importorskip("sqlite_vec")
    import sqlite3

    from nymeria.core.embedding_client import EmbeddingClient

    monkeypatch.setattr(
        EmbeddingClient, "_get_local_embedder", lambda self: _FakeLocalEncoder()
    )
    idx = SkillEmbeddingIndex(
        db_path=tmp_path / "skills.db",
        embedding_provider="local",
        embedding_model="fake-a",
        embedding_dimensions=4,
    )
    assert idx.rebuild("installed", SAMPLE_SKILLS)["semantic_indexed"] == 4
    idx.close()

    # Same DB, different model: the stale vectors must be gone before any
    # rebuild runs, and the stamp updated.
    idx2 = SkillEmbeddingIndex(
        db_path=tmp_path / "skills.db",
        embedding_provider="local",
        embedding_model="fake-b",
        embedding_dimensions=4,
    )
    conn = sqlite3.connect(str(tmp_path / "skills.db"))
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        count = conn.execute("SELECT count(*) FROM skills_vec").fetchone()[0]
        stamp = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
    finally:
        conn.close()
    assert count == 0
    assert stamp["model"] == "fake-b"
    idx2.close()


def test_embed_failure_does_not_permanently_disable_semantic(tmp_path, monkeypatch):
    """A transient embedding failure must degrade that call only, never latch
    semantic search off for the whole process (mirrors the tool-search index)."""
    idx = SkillEmbeddingIndex(
        db_path=tmp_path / "skills.db",
        embedding_api_key="sk-real-looking-key",
    )

    def _boom(self):
        raise RuntimeError("endpoint down")

    from nymeria.core.embedding_client import EmbeddingClient

    monkeypatch.setattr(EmbeddingClient, "_get_openai_client", _boom)

    assert idx._embed("hello") is None
    assert "endpoint down" in (idx.last_error or "")
    # The old behavior latched _semantic_available False here.
    assert idx.is_semantic_available() is True


def test_legacy_unstamped_db_adopted_without_wipe(tmp_path):
    """A pre-stamp DB (the OpenAI-only 1536 world) whose current config still
    matches what could have produced it is adopted in place: stamped, vectors
    kept, no wipe."""
    pytest.importorskip("sqlite_vec")
    import struct

    idx = SkillEmbeddingIndex(
        db_path=tmp_path / "skills.db",
        embedding_api_key="sk-real-looking-key",
    )
    # Plant a vector row, then strip the stamp to simulate the legacy DB.
    conn = idx._get_connection()
    vec = struct.pack("1536f", *([0.1] * 1536))
    conn.execute(
        "INSERT INTO skills_vec(skill_key, embedding) VALUES (?, ?)",
        ("installed:pdf", vec),
    )
    conn.execute("DELETE FROM index_meta")
    conn.commit()
    idx.close()

    idx2 = SkillEmbeddingIndex(
        db_path=tmp_path / "skills.db",
        embedding_api_key="sk-real-looking-key",
    )
    conn = idx2._get_connection()
    count = conn.execute("SELECT count(*) FROM skills_vec").fetchone()[0]
    stamp = dict(conn.execute("SELECT key, value FROM index_meta").fetchall())
    idx2.close()
    assert count == 1  # adopted, not wiped
    assert stamp["provider"] == "openai"
    assert stamp["dim"] == "1536"
    assert stamp["input_type"] == ""


class _FlakyLocalEncoder(_FakeLocalEncoder):
    """Fake encoder that can be broken mid-test (rebuild works, query fails)."""

    def __init__(self):
        self.fail = False

    def encode(self, inputs, **kwargs):
        if self.fail:
            raise RuntimeError("weights corrupted")
        return super().encode(inputs, **kwargs)


def test_search_warns_when_query_embed_fails(tmp_path, monkeypatch):
    """A failed query embed while semantic search is nominally available must
    surface the degradation warning, not silently serve keyword results."""
    pytest.importorskip("sqlite_vec")
    from nymeria.core.embedding_client import EmbeddingClient

    enc = _FlakyLocalEncoder()
    monkeypatch.setattr(EmbeddingClient, "_get_local_embedder", lambda self: enc)
    idx = SkillEmbeddingIndex(
        db_path=tmp_path / "skills.db",
        embedding_provider="local",
        embedding_model="fake-granite",
        embedding_dimensions=4,
    )
    assert idx.rebuild("installed", SAMPLE_SKILLS)["semantic_indexed"] == 4

    enc.fail = True
    r = idx.search("pdf", namespace="installed", top_k=3)
    assert r.mode in ("bm25", "substring")
    assert r.warning and "semantic search unavailable" in r.warning
    assert idx.is_semantic_available() is True  # still not latched off
