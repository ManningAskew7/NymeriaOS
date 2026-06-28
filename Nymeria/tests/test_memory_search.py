"""Characterization tests for `MemoryIndex.search()` (slice 04 F3).

These lock the exact retrieval/scoring behavior of the hybrid search method
across its decomposition into per-branch helpers, so the refactor stays provably
byte-for-byte behavior-preserving. They assert EXACT `ChunkResult.score` floats
per scoring path (rrf base, weighted base, anchor RRF term, anchor plateau /
Gaussian multiplier, recency factor, prose factor, vector term), because the
`tools/rag_eval.py --compare` gate is rank/set-based and cannot catch an
ordering-preserving float-math drift.

Fusion is rank-based Reciprocal Rank Fusion, so with a controlled candidate set
the score of a single in-scope match is fully determined by its branch ranks and
the post-fusion multipliers. Expected scores are written as literal arithmetic
(e.g. ``1.0 / 61`` for RRF_K=60 at rank 0) rather than re-derived from the code's
constants, so a changed constant or off-by-one offset fails the assertion.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from nymeria.core.memory_index import (
    PROSE_PRIORITY_WEIGHT,
    TOOL_ACTIVITY_MARKER,
    MemoryIndex,
)


@contextmanager
def fresh_index(dims=None):
    """A throwaway MemoryIndex with no real embedder (vector branch off unless a
    deterministic `embed_text` is monkeypatched in)."""
    with TemporaryDirectory() as tmpdir:
        index = MemoryIndex(
            Path(tmpdir) / "memory.db",
            embedding_provider="none",
            embedding_dimensions=dims,
        )
        try:
            yield index
        finally:
            index.close()


def _basis(i: int, dims: int) -> list[float]:
    """A unit basis vector (1.0 at position i). Two chunks in the same bucket are
    distance 0 under vec0's L2 metric, so bucket choice controls vector rank."""
    vec = [0.0] * dims
    vec[i] = 1.0
    return vec


# --- RRF / weighted base (single BM25 match, no multipliers) -----------------

def test_rrf_base_single_bm25_match_exact_score():
    with fresh_index() as index:
        index.add_chunk("zebra unique sentinel token", {}, "memory", "u")
        index.add_chunk("completely different content here", {}, "memory", "u")
        res = index.search(
            "zebra", "u",
            apply_recency=False, apply_prose_priority=False, dedup=False,
        )
        assert [r.content for r in res] == ["zebra unique sentinel token"]
        # bm_rank 0, no vector / anchor: base = bm25_weight / (RRF_K + 0 + 1).
        assert res[0].score == pytest.approx(1.0 / 61)


def test_weighted_base_single_bm25_match_exact_score():
    with fresh_index() as index:
        index.add_chunk("zebra unique sentinel token", {}, "memory", "u")
        res = index.search(
            "zebra", "u", fusion="weighted",
            apply_recency=False, apply_prose_priority=False, dedup=False,
        )
        # weighted, bm-only rank 0: base = 0.3 / (1 + 0).
        assert res[0].score == pytest.approx(0.3)


# --- recency multiplier ------------------------------------------------------

def test_recency_multiplier_exact_one_half_life():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with fresh_index() as index:
        index.add_chunk(
            "zebra recency probe", {}, "conversation", "u",
            event_time=now - timedelta(days=730),  # exactly one conversation half-life
        )
        res = index.search(
            "zebra", "u", now=now, apply_prose_priority=False, dedup=False,
        )
        # base 1/61 * 0.5 ** (730 / 730) = base * 0.5.
        assert res[0].score == pytest.approx((1.0 / 61) * 0.5)


def test_recency_disabled_is_base_only():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with fresh_index() as index:
        index.add_chunk(
            "zebra norec probe", {}, "conversation", "u",
            event_time=now - timedelta(days=2000),  # ancient, but recency off
        )
        res = index.search(
            "zebra", "u", now=now,
            apply_recency=False, apply_prose_priority=False, dedup=False,
        )
        assert res[0].score == pytest.approx(1.0 / 61)


# --- prose-priority multiplier ----------------------------------------------

def test_prose_priority_multiplier_exact():
    content = "zebra prose lead in" + TOOL_ACTIVITY_MARKER + " big tool output dump payload"
    with fresh_index() as index:
        index.add_chunk(content, {}, "conversation", "u")
        res = index.search(
            "zebra", "u", apply_recency=False, dedup=False,  # prose on (default)
        )
        marker_idx = content.find(TOOL_ACTIVITY_MARKER)
        frac = (len(content) - marker_idx) / len(content)
        assert frac > 0  # fixture really is tool-text-heavy
        expected = (1.0 / 61) * (1.0 - PROSE_PRIORITY_WEIGHT * frac)
        assert res[0].score == pytest.approx(expected)


def test_prose_priority_disabled_is_base_only():
    content = "zebra prose lead in" + TOOL_ACTIVITY_MARKER + " big tool output dump payload"
    with fresh_index() as index:
        index.add_chunk(content, {}, "conversation", "u")
        res = index.search(
            "zebra", "u",
            apply_recency=False, apply_prose_priority=False, dedup=False,
        )
        assert res[0].score == pytest.approx(1.0 / 61)


# --- anchor recall branch (RRF term + plateau/Gaussian multiplier) -----------

def test_anchor_in_window_plateau_and_rrf_term_exact():
    base_t = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    with fresh_index() as index:
        index.add_chunk(
            "zebra anchored note", {}, "conversation", "u",
            thread_id="T", event_time=base_t,
        )
        # A same-term chunk in another thread, scoped out by thread_id="T".
        index.add_chunk(
            "zebra elsewhere note", {}, "conversation", "u",
            thread_id="OTHER", event_time=base_t,
        )
        res = index.search(
            "zebra", "u", thread_id="T",
            anchor_start=base_t - timedelta(days=1),
            anchor_end=base_t + timedelta(days=1),
            apply_prose_priority=False, dedup=False,
        )
        assert [r.content for r in res] == ["zebra anchored note"]
        # bm rank0 (1/61) + anchor rank0 (0.5/61); in-window plateau multiplier
        # = ANCHOR_FLOOR + (1 - ANCHOR_FLOOR) * 1.0 = 1.0.
        assert res[0].score == pytest.approx((1.0 + 0.5) / 61)


def test_anchor_gaussian_falloff_exact():
    lo = datetime(2026, 6, 1, tzinfo=timezone.utc)
    hi = lo + timedelta(days=1)
    ev = hi + timedelta(days=2)  # 2 days past the window end
    with fresh_index() as index:
        index.add_chunk(
            "zebra faroff note", {}, "conversation", "u",
            thread_id="T", event_time=ev,
        )
        res = index.search(
            "zebra", "u", thread_id="T",
            anchor_start=lo, anchor_end=hi, anchor_edge_sigma_days=1.0,
            apply_prose_priority=False, dedup=False,
        )
        gauss = math.exp(-0.5 * (2.0 / 1.0) ** 2)
        multiplier = 0.4 + 0.6 * gauss  # ANCHOR_FLOOR=0.4
        expected = ((1.0 + 0.5) / 61) * multiplier
        assert res[0].score == pytest.approx(expected)


# --- vector branch (deterministic embedder) ----------------------------------

def test_vector_only_branch_exact_score_and_diag(monkeypatch):
    with fresh_index(dims=8) as index:
        vecs = {
            "alpha target content": _basis(0, 8),
            "beta other content": _basis(3, 8),
            "qqzz outoftext query": _basis(0, 8),  # query shares target's bucket
        }

        def fake_embed(text, input_type="document"):
            return vecs.get(text, _basis(7, 8))

        monkeypatch.setattr(index, "embed_text", fake_embed)
        index.add_chunk("alpha target content", {}, "memory", "u")
        index.add_chunk("beta other content", {}, "memory", "u")
        # Query tokens appear in NO chunk, so BM25 contributes nothing; only the
        # vector branch supplies candidates (target rank0, other rank1).
        res = index.search(
            "qqzz outoftext query", "u",
            apply_recency=False, apply_prose_priority=False, dedup=False,
        )
        assert res[0].content == "alpha target content"
        assert res[0].score == pytest.approx(1.0 / 61)  # vec rank0
        assert res[1].score == pytest.approx(1.0 / 62)  # vec rank1
        diag = index._last_search_diag
        assert diag is not None
        assert diag["vector_used"] is True
        assert diag["vector_candidates"] == 2
        assert diag["bm25_candidates"] == 0


def test_weighted_fusion_vector_term_exact(monkeypatch):
    with fresh_index(dims=8) as index:
        vecs = {"alpha target content": _basis(0, 8), "qqzz outoftext query": _basis(0, 8)}

        def fake_embed(text, input_type="document"):
            return vecs.get(text, _basis(7, 8))

        monkeypatch.setattr(index, "embed_text", fake_embed)
        index.add_chunk("alpha target content", {}, "memory", "u")
        # Query token absent from content -> no BM25; vector rank0 under the
        # legacy weighted blend: base = 0.7 / (1 + 0).
        res = index.search(
            "qqzz outoftext query", "u", fusion="weighted",
            apply_recency=False, apply_prose_priority=False, dedup=False,
        )
        assert res[0].content == "alpha target content"
        assert res[0].score == pytest.approx(0.7)


def test_post_fetch_filter_drop_returns_empty(monkeypatch):
    """The vec0 table carries no user column, so the vector branch can surface
    another user's chunk; the fetch step's scope filter then drops it. When that
    leaves no surviving rows, search hits the second early return (`not
    rows_by_id`), distinct from the no-candidate return."""
    with fresh_index(dims=8) as index:
        vecs = {"owner alpha chunk": _basis(0, 8), "qqzz outoftext query": _basis(0, 8)}

        def fake_embed(text, input_type="document"):
            return vecs.get(text, _basis(7, 8))

        monkeypatch.setattr(index, "embed_text", fake_embed)
        index.add_chunk("owner alpha chunk", {}, "memory", "owner")
        # Searcher "intruder" gets the owner's chunk as an unfiltered vector
        # candidate (query token absent from content -> no BM25), but the fetch
        # step scopes to user_id="intruder" and drops it.
        res = index.search(
            "qqzz outoftext query", "intruder",
            apply_recency=False, apply_prose_priority=False, dedup=False,
        )
        assert res == []
        diag = index._last_search_diag
        assert diag is not None
        assert diag["vector_candidates"] == 1  # candidate found, then filtered out


def test_vector_and_bm25_fusion_exact(monkeypatch):
    with fresh_index(dims=8) as index:
        vecs = {"alpha shared chunk": _basis(0, 8), "alpha": _basis(0, 8)}

        def fake_embed(text, input_type="document"):
            return vecs.get(text, _basis(7, 8))

        monkeypatch.setattr(index, "embed_text", fake_embed)
        index.add_chunk("alpha shared chunk", {}, "memory", "u")
        res = index.search(
            "alpha", "u",
            apply_recency=False, apply_prose_priority=False, dedup=False,
        )
        # bm rank0 (1/61) + vec rank0 (1/61).
        assert res[0].content == "alpha shared chunk"
        assert res[0].score == pytest.approx(2.0 / 61)
        diag = index._last_search_diag
        assert diag is not None
        assert diag["vector_used"] is True
        assert diag["bm25_used"] is True


def test_empty_list_embedding_skips_select_but_diag_reports_used(monkeypatch):
    """The single most counterintuitive invariant: an empty-list embedding is
    falsy (so the vec0 SELECT is skipped, vector_candidates == 0) yet `is not
    None` (so diag.vector_used stays True). The branch's `if query_embedding`
    guard and the diag's `is not None` must NOT be collapsed by the refactor."""
    with fresh_index() as index:
        monkeypatch.setattr(index, "embed_text", lambda text, input_type="document": [])
        index.add_chunk("alpha sentinel note", {}, "memory", "u")
        res = index.search(
            "alpha", "u", apply_recency=False, apply_prose_priority=False, dedup=False,
        )
        diag = index._last_search_diag
        assert diag is not None
        assert diag["vector_used"] is True        # [] is not None
        assert diag["vector_candidates"] == 0      # but falsy -> SELECT skipped
        assert diag["bm25_used"] is True
        assert [r.content for r in res] == ["alpha sentinel note"]
        assert res[0].score == pytest.approx(1.0 / 61)


# --- greedy result dedup -----------------------------------------------------

def test_dedup_collapses_near_duplicates_and_off_keeps_both():
    a = "the garage door keypad code is four seven one two"
    b = a + " now"  # token-set Jaccard 10/11 ~= 0.909 >= DEDUP_THRESHOLD (0.9)
    with fresh_index() as index:
        index.add_chunk(a, {}, "memory", "u")
        index.add_chunk(b, {}, "memory", "u")
        kept = index.search("garage", "u")  # dedup default True
        both = index.search("garage", "u", dedup=False)
        assert len(kept) == 1
        assert len(both) == 2


# --- early returns + scope floor ---------------------------------------------

def test_no_matching_candidates_returns_empty():
    with fresh_index() as index:
        index.add_chunk("alpha note", {}, "memory", "u")
        assert index.search("nonexistentterm", "u") == []


def test_empty_query_returns_empty_and_resets_diag():
    with fresh_index() as index:
        index.add_chunk("alpha note", {}, "memory", "u")
        index.search("alpha", "u")
        assert index._last_search_diag is not None
        assert index.search("   ", "u") == []
        assert index._last_search_diag is None


def test_vector_limit_floor_only_when_thread_scoped():
    with fresh_index() as index:
        index.add_chunk("alpha scoped probe", {}, "memory", "u", thread_id="T")
        index.search("alpha", "u")  # not selective
        diag = index._last_search_diag
        assert diag is not None
        # candidate_pool = max(limit * 5, 30) = max(25, 30) = 30.
        assert diag["vector_limit"] == 30
        index.search("alpha", "u", thread_id="T")  # selective -> floor
        diag = index._last_search_diag
        assert diag is not None
        assert diag["vector_limit"] == 200  # max(30, VECTOR_FILTER_POOL)
