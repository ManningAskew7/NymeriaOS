"""Tests for the RAG eval harness (tools/rag_eval.py).

Covers the relevance-predicate matcher, the metric math, and that the seeded
config comparison detects a recency-induced ranking change. The harness lives
under tools/ (not a package), so we add it to sys.path explicitly.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
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
        for key in ("hit_rate", "mrr", "precision_at_k", "ndcg_at_k",
                    "false_positive_rate", "distinct_at_k"):
            assert 0.0 <= m[key] <= 1.0
        assert m["n"] == len(probes)


def test_probe_from_dict_parses_expect_empty():
    p = rag_eval.Probe.from_dict({"query": "q", "expect_empty": True,
                                  "contains_any": ["x"]})
    assert p.expect_empty is True


def test_expect_empty_feeds_false_positive_rate_not_hit_rate():
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        probes = rag_eval.build_seeded(idx, user_id="eval")
        # The seeded set carries exactly one no-answer probe.
        assert sum(1 for p in probes if p.expect_empty) == 1
        m = rag_eval.evaluate(idx, probes, user_id="eval", k=5)
        assert m["n_empty"] == 1
        # No spurious "relevant" hit on the no-answer probe in seeded mode.
        assert m["false_positive_rate"] == 0.0
        # Ranking metrics average over answerable probes only; the no-answer
        # probe is in the total but not the ranking denominator.
        assert m["n"] == len(probes)
        assert sum(1 for p in probes if not p.expect_empty) < len(probes)


def test_distinct_at_k_drops_when_dedup_disabled():
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        probes = rag_eval.build_seeded(idx, user_id="eval")
        on = rag_eval.evaluate(idx, probes, user_id="eval", k=5,
                               base_kwargs={"dedup": True})
        off = rag_eval.evaluate(idx, probes, user_id="eval", k=5,
                                base_kwargs={"dedup": False})
        # The seeded near-duplicate pair is collapsed with dedup on (distinct
        # ratio 1.0) and surfaces both copies with dedup off (ratio < 1.0).
        assert on["distinct_at_k"] >= off["distinct_at_k"]
        assert off["distinct_at_k"] < 1.0


# --- retrieval-mode decomposition (vector / bm25 in isolation) --------------

def test_retrieval_mode_bm25_scores_seeded_corpus():
    # BM25 alone needs no embeddings, so it retrieves on the lexically-aligned
    # seeded probes even with provider="none".
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        probes = rag_eval.build_seeded(idx, user_id="eval")
        m = rag_eval.evaluate(idx, probes, user_id="eval", k=5,
                              retrieval_mode="bm25")
        assert m["hit_rate"] > 0.0
        assert 0.0 <= m["mrr"] <= 1.0


def test_retrieval_mode_vector_empty_without_embeddings():
    # provider="none" stores no vectors, so the vector-only branch retrieves
    # nothing (the model A/B path is a no-op until a real embedder is wired).
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        probes = rag_eval.build_seeded(idx, user_id="eval")
        m = rag_eval.evaluate(idx, probes, user_id="eval", k=5,
                              retrieval_mode="vector")
        assert m["hit_rate"] == 0.0


def test_retrieve_bm25_preserves_thread_and_returns_chunkresults():
    # The single-branch helper returns production-shaped ChunkResults with the
    # source session preserved, so result_is_relevant scores them like a hybrid
    # result (session-grained gold).
    with TemporaryDirectory() as tmp:
        data_path = Path(tmp) / "lme.json"
        data_path.write_text(json.dumps(_LME_FIXTURE))
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        probes = rag_eval.load_longmemeval(idx, str(data_path), user_id="eval")
        results = rag_eval._retrieve_bm25(idx, probes[0].query, "eval", 5)
        assert results and all(isinstance(r, ChunkResult) for r in results)
        assert any(r.thread_id == "s_gold" for r in results)


# --- LongMemEval loader + session-grained gold -----------------------------

_LME_FIXTURE = [
    {
        "question_id": "q1",
        "question_type": "single-session-user",
        "question": "what breed is my dog",
        "answer": "a corgi named Biscuit",
        "question_date": "2023/05/20 (Sat) 10:00",
        "haystack_session_ids": ["s_gold", "s_distract"],
        "haystack_dates": ["2023/05/01 (Mon) 09:00", "2023/05/10 (Wed) 14:00"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "my dog is a corgi named Biscuit",
                 "has_answer": True},
                {"role": "assistant", "content": "what a cute corgi"},
            ],
            [
                {"role": "user", "content": "the weather in sydney is sunny today"},
                {"role": "assistant", "content": "enjoy the sunshine"},
            ],
        ],
        "answer_session_ids": ["s_gold"],
    },
    {
        "question_id": "q2_abs",
        "question_type": "single-session-user",
        "question": "what is my bank account pin",
        "answer": "no information available",
        "question_date": "2023/06/01 (Thu) 12:00",
        "haystack_session_ids": ["s_other"],
        "haystack_dates": ["2023/05/15 (Mon) 08:00"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "lets talk about gardening tomatoes"},
                {"role": "assistant", "content": "tomatoes need full sun"},
            ],
        ],
        "answer_session_ids": [],
    },
]


def test_native_dim_lookup():
    assert rag_eval.native_dim("text-embedding-3-large") == 3072
    assert rag_eval.native_dim("text-embedding-3-small") == 1536
    assert rag_eval.native_dim("some-unknown-model") == 1536
    assert rag_eval.native_dim(None) == 1536


def test_parse_lme_date_strips_day_token():
    d = rag_eval._parse_lme_date("2023/05/20 (Sat) 02:21")
    assert d is not None and (d.year, d.month, d.day) == (2023, 5, 20)
    assert rag_eval._parse_lme_date("not a date") is None
    assert rag_eval._parse_lme_date(None) is None


def test_probe_from_dict_parses_relevant_thread_ids():
    p = rag_eval.Probe.from_dict({"query": "q", "relevant_thread_ids": ["a", "b"]})
    assert p.relevant_thread_ids == ["a", "b"]


def test_result_is_relevant_honours_relevant_thread_ids():
    P = rag_eval.Probe
    r = _result("anything", thread_id="s_gold")
    assert rag_eval.result_is_relevant(r, P("q", relevant_thread_ids=["s_gold", "s2"]))
    assert not rag_eval.result_is_relevant(r, P("q", relevant_thread_ids=["s_other"]))
    # An empty gold set (abstention probes) matches nothing.
    assert not rag_eval.result_is_relevant(r, P("q", relevant_thread_ids=[]))


def test_load_longmemeval_indexes_haystack_and_builds_probes():
    with TemporaryDirectory() as tmp:
        data_path = Path(tmp) / "lme.json"
        data_path.write_text(json.dumps(_LME_FIXTURE))
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        probes = rag_eval.load_longmemeval(idx, str(data_path), user_id="eval")

        # One probe per question; gold and abstention parsed correctly.
        assert len(probes) == 2
        assert probes[0].relevant_thread_ids == ["s_gold"]
        assert probes[0].expect_empty is False
        assert probes[1].expect_empty is True            # qid ends in _abs
        assert probes[1].relevant_thread_ids == []

        # Each haystack turn is indexed under its session id with the session
        # date as event_time, so BM25 alone surfaces the gold session and the
        # session-membership predicate scores it relevant.
        results = idx.search(probes[0].query, "eval", limit=5)
        gold = [r for r in results if r.thread_id == "s_gold"]
        assert gold, "gold session should be retrievable for an answerable question"
        assert gold[0].event_time is not None and gold[0].event_time.year == 2023
        assert any(rag_eval.result_is_relevant(r, probes[0]) for r in results)

        # question_date is parsed into a day-precision anchor on each probe.
        assert probes[0].anchor_time == "2023-05-20"
        assert probes[1].anchor_time == "2023-06-01"


def test_longmemeval_abstention_feeds_n_empty_not_hit_rate():
    with TemporaryDirectory() as tmp:
        data_path = Path(tmp) / "lme.json"
        data_path.write_text(json.dumps(_LME_FIXTURE))
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        probes = rag_eval.load_longmemeval(idx, str(data_path), user_id="eval")
        m = rag_eval.evaluate(idx, probes, user_id="eval", k=5)
        assert m["n"] == 2 and m["n_empty"] == 1
        # No gold session for the abstention probe, so no spurious retrieval hit.
        assert m["false_positive_rate"] == 0.0


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


def test_compare_detects_anchor_effect():
    with TemporaryDirectory() as tmp:
        idx = MemoryIndex(Path(tmp) / "e.db", embedding_provider="none")
        # The relevant chunk is on the anchor date but shares no terms with the
        # query, so BM25 alone (provider="none") never retrieves it. A distractor
        # that DOES match the query sits far from the anchor. Only the anchor
        # recall branch can surface the relevant chunk.
        idx.add_chunk("the team offsite was in the snowy mountains", {},
                      "conversation", "eval", thread_id="s-rel",
                      event_time=datetime(2026, 4, 15, tzinfo=timezone.utc))
        idx.add_chunk("budget planning numbers spreadsheet", {},
                      "conversation", "eval", thread_id="s-far",
                      event_time=datetime(2025, 1, 1, tzinfo=timezone.utc))
        probes = [rag_eval.Probe(
            query="budget planning numbers",
            contains_any=["offsite"],
            anchor_time="2026-04-15",
        )]
        out = rag_eval.compare_configs(
            idx, probes,
            {"anchor on": {"fusion": "rrf", "apply_recency": False},
             "anchor off": {"fusion": "rrf", "apply_recency": False, "anchor_off": True}},
            user_id="eval", k=5,
        )
        # Anchor off never retrieves the relevant (content-weak) chunk; anchor on
        # recalls it by date, so MRR is strictly higher.
        assert out["anchor off"]["mrr"] == 0.0
        assert out["anchor on"]["mrr"] > out["anchor off"]["mrr"]
