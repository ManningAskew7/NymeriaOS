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
    return SkillEmbeddingIndex(db_path=tmp_path / "skills.db", openai_api_key=None)


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
    idx = SkillEmbeddingIndex(db_path=tmp_path / "skills.db", openai_api_key="cpx-local-test")
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
        openai_api_key=os.environ.get("EMBEDDING_API_KEY"),
        openai_base_url=os.environ.get("EMBEDDING_BASE_URL"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
    )
    idx.rebuild("installed", SAMPLE_SKILLS)
    r = idx.search("read text in pictures", namespace="installed", top_k=3)
    assert r.mode == "semantic"
    assert r.results
    assert r.results[0].name == "ocr-tool"
