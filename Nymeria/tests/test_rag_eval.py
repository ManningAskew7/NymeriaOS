"""Tests for the RAG eval harness (tools/rag_eval.py).

Covers the relevance-predicate matcher, the metric math, and that the seeded
config comparison detects a recency-induced ranking change. The harness lives
under tools/ (not a package), so we add it to sys.path explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import rag_eval  # noqa: E402
from nymeria.core.memory_index import ChunkResult, MemoryIndex  # noqa: E402
from nymeria.core.time_utils import utc_now  # noqa: E402


def _result(content, chunk_type="conversation", thread_id=None, rid="1"):
    return ChunkResult(id=rid, content=content, chunk_type=chunk_type,
                       thread_id=thread_id, created_at=utc_now(), metadata={}, score=1.0)


# --- predicate matcher -----------------------------------------------------

def test_contains_any_and_all():
    P = rag_eval.Probe
    r = _result("the asyncio task deadlocked")
    assert rag_eval.result_is_relevant(r, P("q", contains_any=["asyncio", "x"]))
    assert not rag_eval.result_is_relevant(r, P("q", contains_any=["nope"]))
    assert rag_eval.result_is_relevant(r, P("q", contains_all=["asyncio", "deadlock"]))
    assert not rag_eval.result_is_relevant(r, P("q", contains_all=["asyncio", "nope"]))


def test_type_and_thread_and_id_predicates():
    P = rag_eval.Probe
    r = _result("x", chunk_type="memory", thread_id="t1", rid="abc")
    assert rag_eval.result_is_relevant(r, P("q", chunk_type="memory"))
    assert not rag_eval.result_is_relevant(r, P("q", chunk_type="todo"))
    assert rag_eval.result_is_relevant(r, P("q", thread_id="t1"))
    assert rag_eval.result_is_relevant(r, P("q", relevant_ids=["abc"]))
    assert not rag_eval.result_is_relevant(r, P("q", relevant_ids=["zzz"]))


def test_empty_predicate_matches_nothing():
    # A probe with no predicate must not silently count everything as a hit.
    assert not rag_eval.result_is_relevant(_result("anything"), rag_eval.Probe("q"))


def test_probe_from_dict_ignores_unknown_keys():
    p = rag_eval.Probe.from_dict({"query": "q", "contains_any": ["a"], "bogus": 1})
    assert p.query == "q" and p.contains_any == ["a"]


# --- metrics ---------------------------------------------------------------

def test_ndcg_rewards_top_ranking():
    assert rag_eval._ndcg([1, 0, 0], 3) == 1.0
    # relevant item at rank 2 scores less than at rank 1
    assert rag_eval._ndcg([0, 1, 0], 3) < 1.0
    assert rag_eval._ndcg([0, 0, 0], 3) == 0.0


# --- end-to-end on the seeded corpus ---------------------------------------

def test_seeded_eval_metrics_present():
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        probes = rag_eval.build_seeded(idx, user_id="eval")
        m = rag_eval.evaluate(idx, probes, user_id="eval", k=5)
        for key in ("hit_rate", "mrr", "precision_at_k", "ndcg_at_k"):
            assert 0.0 <= m[key] <= 1.0
        assert m["n"] == len(probes)


def test_compare_detects_recency_effect():
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        probes = rag_eval.build_seeded(idx, user_id="eval")
        out = rag_eval.compare_configs(
            idx, probes,
            {"rec_on": {"fusion": "rrf", "apply_recency": True},
             "rec_off": {"fusion": "rrf", "apply_recency": False}},
            user_id="eval", k=5,
        )
        # The recency-discriminating probe pushes a relevant chunk down when
        # recency is on, so MRR with recency must be strictly lower.
        assert out["rec_on"]["mrr"] < out["rec_off"]["mrr"]
