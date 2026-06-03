#!/usr/bin/env python3
"""Retrieval-quality evaluation harness for the native RAG memory index.

Runs a set of ``query -> relevance predicate`` probes against a ``MemoryIndex``
and reports hit-rate@k, precision@k, MRR, nDCG@k, distinct@k (how free the
result set is of near-duplicates) and false_positive_rate (over no-answer
probes). Its purpose is to make ranking changes (RRF vs weighted, recency,
result dedup, contextual retrieval, reranking, consolidation) measurable instead
of asserted: capture a baseline, change one thing, re-run, compare.

Relevance is judged by PREDICATE, not by volatile chunk id, so a probe set
survives reindexing, scrubbing and re-embedding. A result counts as relevant
when it satisfies every predicate the probe specifies (unspecified predicates
are ignored):

    relevant_ids         result.id is in this list
    contains_any         result.content contains ANY of these substrings (ci)
    contains_all         result.content contains ALL of these substrings (ci)
    chunk_type           result.chunk_type equals this
    thread_id            result.thread_id equals this
    relevant_thread_ids  result.thread_id is in this list (session-grained gold)

A probe may also set ``expect_empty: true`` to mark a no-answer query: the
corpus holds nothing that should satisfy it. Such probes are excluded from the
ranking metrics and instead feed ``false_positive_rate`` (any returned result
that matches the predicates counts as a spurious hit; lower is better).

A probe may also carry ``search`` kwargs (e.g. ``{"thread_id": "...", "since":
"2026-05-01"}``) passed straight through to ``MemoryIndex.search`` so filter
behaviour can be evaluated too.

Modes:

    # Seeded synthetic corpus (no API key, deterministic, used by CI/tests):
    python3 tools/rag_eval.py
    python3 tools/rag_eval.py --compare        # rrf vs weighted, recency on/off

    # A real per-user corpus (needs a real EMBEDDING_API_KEY for the vectors):
    python3 tools/rag_eval.py --db /data/users/default/memory.db \
        --user default --probes data/rag_eval_probes.default.json --compare

    # LongMemEval: index the dataset's chat haystack and probe it (real key).
    # Compare embedding models by running once per model and diffing the dumps;
    # --embedding-dim defaults to the model's native width (its best config):
    python3 tools/rag_eval.py --dataset longmemeval \
        --data data/eval/longmemeval/longmemeval_oracle.json \
        --embedding-model text-embedding-3-small --out runs/small.json
    python3 tools/rag_eval.py --dataset longmemeval \
        --data data/eval/longmemeval/longmemeval_oracle.json \
        --embedding-model text-embedding-3-large --out runs/large.json
    python3 tools/rag_eval.py --compare-runs runs/small.json runs/large.json

The live corpus lives in the Docker ``nymeria_data`` volume and ``tools/`` is
not bind-mounted, so to run live: ``docker cp`` this file (plus the probes JSON
or dataset) into the container and run it there (the embedding key is in its env).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional

# Allow running directly from Nymeria/ (script lives in Nymeria/tools/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nymeria.core.memory_index import DEDUP_THRESHOLD, MemoryIndex  # noqa: E402
from nymeria.core.time_utils import utc_now  # noqa: E402


# --- probe model -----------------------------------------------------------

@dataclass
class Probe:
    query: str
    relevant_ids: Optional[List[str]] = None
    contains_any: Optional[List[str]] = None
    contains_all: Optional[List[str]] = None
    chunk_type: Optional[str] = None
    thread_id: Optional[str] = None
    # Result is relevant when its source thread (session) is in this set. The
    # natural gold signal for session-grained datasets like LongMemEval, where a
    # question maps to one or more evidence sessions rather than a single thread.
    relevant_thread_ids: Optional[List[str]] = None
    # No-answer probe: the corpus has nothing that should satisfy this query.
    # Excluded from hit_rate/MRR (it has no correct answer); instead it feeds
    # false_positive_rate, where any returned result that matches the probe's
    # predicates counts as a spurious "relevant" hit (lower is better).
    expect_empty: bool = False
    search: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Probe":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def result_is_relevant(result, probe: Probe) -> bool:
    """True when a ChunkResult satisfies every predicate the probe specifies."""
    content = (result.content or "").lower()
    if probe.relevant_ids is not None and result.id not in probe.relevant_ids:
        return False
    if probe.contains_any is not None and not any(
        s.lower() in content for s in probe.contains_any
    ):
        return False
    if probe.contains_all is not None and not all(
        s.lower() in content for s in probe.contains_all
    ):
        return False
    if probe.chunk_type is not None and result.chunk_type != probe.chunk_type:
        return False
    if probe.thread_id is not None and result.thread_id != probe.thread_id:
        return False
    if probe.relevant_thread_ids is not None and result.thread_id not in probe.relevant_thread_ids:
        return False
    # A probe with no predicate at all matches nothing (avoid silent all-hits).
    has_predicate = any(
        x is not None for x in (
            probe.relevant_ids, probe.contains_any, probe.contains_all,
            probe.chunk_type, probe.thread_id, probe.relevant_thread_ids,
        )
    )
    return has_predicate


# --- metrics ---------------------------------------------------------------

def _ndcg(rels: List[int], k: int) -> float:
    """Binary-relevance nDCG@k over a ranked 0/1 list."""
    rels = rels[:k]
    dcg = sum(r / math.log2(i + 2) for i, r in enumerate(rels))
    ideal = sorted(rels, reverse=True)
    idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


def _distinct_ratio(results, threshold: float) -> float:
    """Share of a result list that is NOT a near-duplicate of an earlier result.

    1.0 means every returned result is distinct; lower means twins occupy slots.
    An empty result list is vacuously fully-distinct (1.0). Uses the same
    near-duplicate test the index applies when ``dedup`` is on, so this measures
    redundancy independently of whether the search itself deduped.
    """
    if not results:
        return 1.0
    kept = []
    for r in results:
        if any(
            MemoryIndex._is_near_duplicate(r.content, k.content, threshold)
            for k in kept
        ):
            continue
        kept.append(r)
    return len(kept) / len(results)


def evaluate(
    index: MemoryIndex,
    probes: List[Probe],
    user_id: str = "eval",
    k: int = 5,
    base_kwargs: Optional[Dict[str, Any]] = None,
    distinct_threshold: float = DEDUP_THRESHOLD,
) -> Dict[str, Any]:
    """Run probes and return aggregate + per-query metrics.

    ``base_kwargs`` are search kwargs applied to every probe (e.g. a fusion or
    recency override for a config comparison); a probe's own ``search`` kwargs
    take precedence on conflict.

    Ranking metrics (hit_rate, MRR, precision, nDCG) are averaged over the
    answerable probes only. ``expect_empty`` (no-answer) probes feed
    ``false_positive_rate`` instead. ``distinct_at_k`` (the share of returned
    results that are not near-duplicates) is averaged over every probe.
    """
    base_kwargs = base_kwargs or {}
    hits = 0
    rr_total = 0.0
    p_total = 0.0
    ndcg_total = 0.0
    fp_count = 0
    n_empty = 0
    distinct_total = 0.0
    per_query: List[Dict[str, Any]] = []

    for p in probes:
        kwargs = {**base_kwargs, **p.search}
        results = index.search(p.query, user_id, limit=k, **kwargs)
        distinct = _distinct_ratio(results, distinct_threshold)
        distinct_total += distinct

        if p.expect_empty:
            n_empty += 1
            fp = 1 if any(result_is_relevant(r, p) for r in results) else 0
            fp_count += fp
            per_query.append({
                "query": p.query,
                "expect_empty": True,
                "false_positive": bool(fp),
                "distinct": round(distinct, 3),
                "note": p.note,
            })
            continue

        rels = [1 if result_is_relevant(r, p) else 0 for r in results]
        rr = next((1.0 / rank for rank, r in enumerate(rels, 1) if r), 0.0)
        hit = 1 if any(rels) else 0
        precision = (sum(rels) / k) if k else 0.0
        ndcg = _ndcg(rels, k)

        hits += hit
        rr_total += rr
        p_total += precision
        ndcg_total += ndcg
        per_query.append({
            "query": p.query,
            "hit": bool(hit),
            "rr": round(rr, 3),
            "p_at_k": round(precision, 3),
            "ndcg": round(ndcg, 3),
            "distinct": round(distinct, 3),
            "ranks": [i for i, r in enumerate(rels, 1) if r],
            "note": p.note,
        })

    n_scored = sum(1 for p in probes if not p.expect_empty) or 1
    n_all = len(probes) or 1
    return {
        "k": k,
        "n": len(probes),
        "n_empty": n_empty,
        "hit_rate": hits / n_scored,
        "mrr": rr_total / n_scored,
        "precision_at_k": p_total / n_scored,
        "ndcg_at_k": ndcg_total / n_scored,
        "false_positive_rate": (fp_count / n_empty) if n_empty else 0.0,
        "distinct_at_k": distinct_total / n_all,
        "per_query": per_query,
    }


def compare_configs(
    index: MemoryIndex,
    probes: List[Probe],
    configs: Dict[str, Dict[str, Any]],
    user_id: str = "eval",
    k: int = 5,
) -> Dict[str, Dict[str, Any]]:
    """Evaluate the same probes under several named search-kwarg configs."""
    return {
        name: evaluate(index, probes, user_id=user_id, k=k, base_kwargs=kwargs)
        for name, kwargs in configs.items()
    }


# --- seeded synthetic corpus (no API key) ----------------------------------

def build_seeded(index: MemoryIndex, user_id: str = "eval") -> List[Probe]:
    """Seed a small but varied corpus and return matching probes.

    Includes paraphrase, exact-term, type-scoped, thread-scoped and distractor
    cases so a ranking change shows up in the aggregate metrics.
    """
    A = index.add_chunk
    A("favourite_colour: Teal", {"key": "favourite_colour"}, "memory", user_id)
    A("pet: a corgi named Biscuit", {"key": "pet"}, "memory", user_id)
    A("quarterly budget spreadsheet and revenue forecast", {}, "memory", user_id)
    A("User: how do I register a company\n\nAssistant: lodge the form with ASIC and get an ABN",
      {}, "conversation", user_id, thread_id="t-biz")
    A("User: what about ongoing costs\n\nAssistant: annual review fee plus your accountant",
      {}, "conversation", user_id, thread_id="t-biz")
    A("User: debug help\n\nAssistant: the asyncio task deadlocked on the shared lock",
      {}, "conversation", user_id, thread_id="t-dev")
    A("pasta carbonara recipe with eggs, guanciale and pecorino", {}, "memory", user_id)
    A("dentist appointment next tuesday at 3pm", {}, "todo", user_id, thread_id="t-cal")
    A("renew passport before the trip", {}, "todo", user_id, thread_id="t-cal")
    A("User: weather?\n\nAssistant: Sydney is 21C and sunny today", {}, "conversation",
      user_id, thread_id="t-misc")

    # Recency-discriminating pair: the relevant chunk ("final") is old, a
    # lexically similar distractor ("cancelled") is recent. With recency ON the
    # recent distractor outranks the relevant one (lower MRR); with recency OFF
    # the relevant one wins on pure BM25. Gives --compare a measurable delta.
    now = utc_now()
    A("project report final version", {}, "conversation", user_id,
      thread_id="t-rep", event_time=now - timedelta(days=200))
    A("project report meeting was cancelled", {}, "conversation", user_id,
      thread_id="t-rep", event_time=now - timedelta(days=1))

    # Near-duplicate pair: two memory chunks whose prose cores differ only by a
    # trailing token, so their token-set Jaccard clears the dedup threshold.
    # Both survive ingest (distinct content hashes), but with result dedup ON
    # the search collapses them to one (distinct_at_k -> 1.0); with dedup OFF
    # both come back (distinct_at_k drops). Drives the dedup delta in --compare.
    A("the garage door keypad code is four seven one two", {}, "memory", user_id)
    A("the garage door keypad code is four seven one two now", {}, "memory", user_id)

    # Multi-fact thread: several related turns under one thread, for scoped
    # retrieval (a thread_id filter must still surface an in-scope match).
    A("User: when is my flight\n\nAssistant: your flight to Tokyo departs June 12 at 9am",
      {}, "conversation", user_id, thread_id="t-trip")
    A("User: which hotel did I book\n\nAssistant: you booked the Shinjuku Granbell for four nights",
      {}, "conversation", user_id, thread_id="t-trip")
    A("User: what should I pack\n\nAssistant: bring a universal power adapter and a light jacket",
      {}, "conversation", user_id, thread_id="t-trip")

    # Vocabulary-mismatch chunk: the matching probe shares ZERO tokens with it,
    # so BM25 cannot bridge the gap. In seeded (BM25-only) mode that probe
    # MISSES on purpose, exposing the lexical ceiling; with real embeddings
    # (live mode) the vector branch closes it. An honest "harder eval" signal.
    A("User: I cannot stand spiders\n\nAssistant: understood, your arachnophobia is noted",
      {}, "conversation", user_id, thread_id="t-fears")

    return [
        Probe("what colour do I like", contains_any=["teal"], chunk_type="memory",
              note="paraphrase + spelling (colour/color)"),
        Probe("what is my pet", contains_any=["corgi", "biscuit"], note="known fact"),
        Probe("how do I start a business in australia",
              contains_any=["asic", "abn", "register"], note="paraphrase -> biz thread"),
        Probe("carbonara", contains_any=["carbonara"], note="exact term"),
        Probe("python concurrency bug", contains_any=["asyncio", "deadlock"],
              note="paraphrase -> dev thread"),
        Probe("upcoming appointments", contains_any=["dentist", "passport"],
              chunk_type="todo", note="type-scoped"),
        Probe("company costs", contains_any=["fee", "accountant", "cost"],
              thread_id="t-biz", search={"thread_id": "t-biz"},
              note="thread filter passthrough"),
        Probe("project report", contains_all=["final"], thread_id="t-rep",
              note="recency vs relevance (recency ON should hurt MRR here)"),
        Probe("garage door code", contains_any=["garage door keypad"],
              note="near-dup pair: dedup collapses the twin (distinct_at_k)"),
        Probe("tokyo trip details", contains_any=["flight", "hotel", "tokyo", "shinjuku"],
              thread_id="t-trip", search={"thread_id": "t-trip"},
              note="multi-fact thread, scoped retrieval"),
        Probe("creepy crawly insects terrify me", contains_any=["spider", "arachno"],
              note="vocabulary mismatch; BM25-only seeded misses, vectors bridge it live"),
        Probe("what is the airspeed of an unladen swallow", expect_empty=True,
              contains_any=["swallow", "airspeed", "velocity"],
              note="no-answer probe: nothing in the corpus; feeds false_positive_rate"),
    ]


# --- LongMemEval dataset loader --------------------------------------------

# Native embedding width per known model. The harness defaults --embedding-dim
# to this so each model is benchmarked at its optimal configuration (3-large at
# its full 3072, not truncated to fit a fixed slot). Unknown models fall back to
# 1536; pass --embedding-dim to override.
NATIVE_EMBEDDING_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


def native_dim(model: Optional[str]) -> int:
    """Native vector width for a model, defaulting to 1536 when unknown."""
    return NATIVE_EMBEDDING_DIMS.get(model or "", 1536)


def _parse_lme_date(raw: Optional[str]) -> Optional[datetime]:
    """Parse a LongMemEval timestamp like ``2023/05/20 (Sat) 02:21``.

    The ``(Day)`` token is stripped before parsing. Returns None on any format
    we do not recognise, so the loader falls back to ingest time for that turn.
    """
    if not raw:
        return None
    cleaned = re.sub(r"\([^)]*\)", "", raw).strip()
    for fmt in ("%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def load_longmemeval(
    index: MemoryIndex,
    data_path: str,
    user_id: str = "eval",
    limit: Optional[int] = None,
) -> List[Probe]:
    """Index a LongMemEval file's chat haystack and return one Probe per question.

    LongMemEval (ICLR 2025, MIT) ships ``query -> evidence session`` gold, so it
    maps onto the predicate harness with no schema change: every haystack turn is
    indexed as a ``conversation`` chunk whose ``thread_id`` is its session id and
    whose ``event_time`` is the session date (so the recency multiplier and the
    since/until filters are genuinely exercised), and each question becomes a
    Probe whose ``relevant_thread_ids`` are its ``answer_session_ids``.

    Abstention in LongMemEval is a generation-side judgement: the oracle still
    labels and includes evidence sessions for ``_abs`` questions, so retrieval is
    not penalised for surfacing them. A question is treated as a no-answer
    (``expect_empty``) probe only when its ``answer_session_ids`` is empty (no
    evidence in the haystack); otherwise its labelled sessions are the gold and
    it is scored like any answerable question. ``false_positive_rate`` therefore
    stays meaningful for a genuine no-gold probe and is simply not exercised by an
    oracle file where every question carries evidence.

    Args:
        index: target MemoryIndex (use a real embedding provider to benchmark the
            vector branch; ``embedding_provider="none"`` only exercises BM25).
        data_path: path to a longmemeval_*.json file.
        user_id: user namespace to index under.
        limit: cap the number of questions (for a cheap partial run).
    """
    entries = json.loads(Path(data_path).read_text())
    if limit:
        entries = entries[:limit]

    probes: List[Probe] = []
    for entry in entries:
        qid = entry.get("question_id", "")
        sessions = entry.get("haystack_sessions", []) or []
        session_ids = entry.get("haystack_session_ids", []) or []
        session_dates = entry.get("haystack_dates", []) or []

        for s_idx, session in enumerate(sessions):
            sid = session_ids[s_idx] if s_idx < len(session_ids) else f"{qid}-s{s_idx}"
            event_time = _parse_lme_date(
                session_dates[s_idx] if s_idx < len(session_dates) else None
            )
            for t_idx, turn in enumerate(session or []):
                content = (turn.get("content") or "").strip()
                if not content:
                    continue
                role = turn.get("role", "")
                index.add_chunk(
                    f"{role}: {content}" if role else content,
                    {
                        "session_id": sid,
                        "role": role,
                        "has_answer": bool(turn.get("has_answer")),
                        "question_id": qid,
                        "turn_index": t_idx,
                    },
                    "conversation",
                    user_id,
                    thread_id=sid,
                    event_time=event_time,
                )

        gold = list(entry.get("answer_session_ids") or [])
        # A no-answer probe is one with no evidence session in the haystack.
        # _abs questions in the oracle still carry labelled gold sessions, so
        # they are scored as answerable retrieval (abstention is judged at
        # generation time, not here).
        probes.append(Probe(
            query=entry.get("question", ""),
            relevant_thread_ids=gold,
            expect_empty=not gold,
            note=f"{entry.get('question_type', '?')} {qid}",
        ))
    return probes


# --- CLI -------------------------------------------------------------------

def _print_metrics(name: str, m: Dict[str, Any], verbose: bool = False) -> None:
    print(f"\n[{name}]  n={m['n']} k={m['k']}")
    print(f"  hit_rate@{m['k']}:   {m['hit_rate']:.3f}")
    print(f"  MRR:          {m['mrr']:.3f}")
    print(f"  precision@{m['k']}: {m['precision_at_k']:.3f}")
    print(f"  nDCG@{m['k']}:      {m['ndcg_at_k']:.3f}")
    print(f"  distinct@{m['k']}:  {m['distinct_at_k']:.3f}  (higher = fewer near-dup results)")
    if m.get("n_empty"):
        print(f"  false_pos_rate: {m['false_positive_rate']:.3f}  "
              f"(over {m['n_empty']} no-answer probe(s); lower is better)")
    if verbose:
        for row in m["per_query"]:
            if row.get("expect_empty"):
                mark = "FP  " if row["false_positive"] else "ok  "
                print(f"    [{mark}] no-answer distinct={row['distinct']:.2f}  {row['query']}")
            else:
                mark = "ok  " if row["hit"] else "MISS"
                print(f"    [{mark}] rr={row['rr']:.2f} ndcg={row['ndcg']:.2f} "
                      f"distinct={row['distinct']:.2f} ranks={row['ranks']}  {row['query']}")


def _open_index(
    db: Optional[str],
    *,
    embedding_provider: Optional[str] = None,
    embedding_model: Optional[str] = None,
    embedding_dimensions: Optional[int] = None,
    embedding_base_url: Optional[str] = None,
) -> MemoryIndex:
    """Open or create a MemoryIndex for a run.

    With ``db`` and no embedding overrides this is the live/production config
    (model and key come from settings). A throwaway DB defaults to BM25-only
    (``provider="none"``, no API key) unless a provider is given, which is how
    the seeded and LongMemEval modes diverge.
    """
    kwargs: Dict[str, Any] = {}
    if embedding_model is not None:
        kwargs["embedding_model"] = embedding_model
    if embedding_dimensions is not None:
        kwargs["embedding_dimensions"] = embedding_dimensions
    if embedding_base_url is not None:
        kwargs["embedding_base_url"] = embedding_base_url
    if db:
        if embedding_provider is not None:
            kwargs["embedding_provider"] = embedding_provider
        return MemoryIndex(Path(db), **kwargs)
    kwargs["embedding_provider"] = embedding_provider or "none"
    tmp = TemporaryDirectory()
    _open_index._tmp = tmp  # keep alive for the process lifetime
    return MemoryIndex(Path(tmp.name) / "rag_eval.db", **kwargs)


def _dump_run(
    path: str,
    *,
    dataset: str,
    embedding_model: Optional[str],
    embedding_dimensions: int,
    embedding_provider: str,
    metrics: Dict[str, Any],
) -> None:
    """Write a run's config + headline metrics for later ``--compare-runs``."""
    keys = ("hit_rate", "mrr", "precision_at_k", "ndcg_at_k",
            "distinct_at_k", "false_positive_rate", "n", "n_empty")
    payload = {
        "dataset": dataset,
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "embedding_provider": embedding_provider,
        "k": metrics["k"],
        "metrics": {key: metrics[key] for key in keys},
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {path}")


def _print_run_comparison(paths: List[str]) -> None:
    """Print a side-by-side table of dumped runs (one per embedding model)."""
    print(f"\n{'model':<26}{'dim':>6}{'k':>4}{'hit':>8}{'mrr':>8}"
          f"{'ndcg':>8}{'distinct':>10}{'fp':>7}")
    print("-" * 77)
    for p in paths:
        d = json.loads(Path(p).read_text())
        m = d.get("metrics", {})
        print(f"{(d.get('embedding_model') or '?'):<26}"
              f"{d.get('embedding_dimensions', '?'):>6}"
              f"{d.get('k', '?'):>4}"
              f"{m.get('hit_rate', 0.0):>8.3f}"
              f"{m.get('mrr', 0.0):>8.3f}"
              f"{m.get('ndcg_at_k', 0.0):>8.3f}"
              f"{m.get('distinct_at_k', 0.0):>10.3f}"
              f"{m.get('false_positive_rate', 0.0):>7.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="RAG retrieval-quality eval harness")
    ap.add_argument("--db", help="Path to a memory.db (live probes, or where to build a dataset index)")
    ap.add_argument("--user", default="eval", help="user_id to search within")
    ap.add_argument("--probes", help="Path to a probes JSON file (live mode)")
    ap.add_argument("--dataset", choices=["seeded", "longmemeval"], default="seeded",
                    help="probe source when --probes is not given")
    ap.add_argument("--data", help="dataset file path (--dataset longmemeval)")
    ap.add_argument("--limit", type=int, help="cap dataset questions (cheap partial run)")
    ap.add_argument("--embedding-model", help="embedding model (default: from settings)")
    ap.add_argument("--embedding-provider", default="openai",
                    help="embedding provider for dataset runs (default openai)")
    ap.add_argument("--embedding-dim", type=int,
                    help="vector width; defaults to the model's native dim")
    ap.add_argument("--embedding-base-url", help="OpenAI-compatible embeddings base URL")
    ap.add_argument("--k", type=int, default=5, help="top-k cutoff")
    ap.add_argument("--compare", action="store_true",
                    help="compare rrf/weighted x recency x dedup within one run")
    ap.add_argument("--out", help="dump headline metrics JSON for --compare-runs")
    ap.add_argument("--compare-runs", nargs="+", metavar="FILE",
                    help="print a side-by-side table of --out dumps and exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="per-query detail")
    args = ap.parse_args()

    # BM25-only seeded mode logs an expected "provider: none" warning per call;
    # quiet the index logger so eval output stays readable.
    logging.getLogger("nymeria.core.memory_index").setLevel(logging.ERROR)

    # Pure reporting mode: diff previously-dumped runs, no indexing needed.
    if args.compare_runs:
        _print_run_comparison(args.compare_runs)
        return

    dim = args.embedding_dim
    if args.dataset == "longmemeval" and dim is None:
        dim = native_dim(args.embedding_model)

    if args.dataset == "longmemeval":
        if not args.data:
            ap.error("--dataset longmemeval requires --data <path>")
            return
        index = _open_index(
            args.db,
            embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model,
            embedding_dimensions=dim,
            embedding_base_url=args.embedding_base_url,
        )
        print(f"indexing LongMemEval haystack from {args.data} "
              f"(model={index.embedding_model}, dim={index.embedding_dimensions}) ...")
        probes = load_longmemeval(index, args.data, user_id=args.user, limit=args.limit)
        print(f"built {len(probes)} probes")
    elif args.probes:
        index = _open_index(args.db)
        raw = json.loads(Path(args.probes).read_text())
        probes = [Probe.from_dict(d) for d in raw]
    elif not args.db:
        index = _open_index(None)
        probes = build_seeded(index, user_id=args.user)
    else:
        ap.error("--db requires --probes or --dataset (no synthetic corpus to seed a live DB)")
        return

    if args.compare:
        configs = {
            "rrf+recency+dedup": {"fusion": "rrf", "apply_recency": True, "dedup": True},
            "rrf, dedup off":    {"fusion": "rrf", "apply_recency": True, "dedup": False},
            "rrf, no recency":   {"fusion": "rrf", "apply_recency": False},
            "weighted+recency":  {"fusion": "weighted", "apply_recency": True},
        }
        results = compare_configs(index, probes, configs, user_id=args.user, k=args.k)
        for name, m in results.items():
            _print_metrics(name, m, verbose=args.verbose)
        primary = next(iter(results.values()))
    else:
        primary = evaluate(index, probes, user_id=args.user, k=args.k)
        _print_metrics(args.dataset, primary, verbose=True)

    if args.out:
        _dump_run(
            args.out,
            dataset=args.dataset,
            embedding_model=index.embedding_model,
            embedding_dimensions=index.embedding_dimensions,
            embedding_provider=index.embedding_provider,
            metrics=primary,
        )


if __name__ == "__main__":
    main()
