#!/usr/bin/env python3
"""Retrieval-quality evaluation harness for the native RAG memory index.

Runs a set of ``query -> relevance predicate`` probes against a ``MemoryIndex``
and reports hit-rate@k, precision@k, MRR, and nDCG@k. Its purpose is to make
ranking changes (RRF vs weighted, recency, contextual retrieval, reranking,
consolidation) measurable instead of asserted: capture a baseline, change one
thing, re-run, compare.

Relevance is judged by PREDICATE, not by volatile chunk id, so a probe set
survives reindexing, scrubbing and re-embedding. A result counts as relevant
when it satisfies every predicate the probe specifies (unspecified predicates
are ignored):

    relevant_ids   result.id is in this list
    contains_any   result.content contains ANY of these substrings (ci)
    contains_all   result.content contains ALL of these substrings (ci)
    chunk_type     result.chunk_type equals this
    thread_id      result.thread_id equals this

A probe may also carry ``search`` kwargs (e.g. ``{"thread_id": "...", "since":
"2026-05-01"}``) passed straight through to ``MemoryIndex.search`` so filter
behaviour can be evaluated too.

Two modes:

    # Seeded synthetic corpus (no API key, deterministic, used by CI/tests):
    python3 tools/rag_eval.py
    python3 tools/rag_eval.py --compare        # rrf vs weighted, recency on/off

    # A real per-user corpus (needs a real EMBEDDING_API_KEY for the vectors):
    python3 tools/rag_eval.py --db /data/users/default/memory.db \
        --user default --probes data/rag_eval_probes.default.json --compare

The live corpus lives in the Docker ``nymeria_data`` volume and ``tools/`` is
not bind-mounted, so to run live: ``docker cp`` this file plus the probes JSON
into the container and run it there (the embedding key is already in its env).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional

# Allow running directly from Nymeria/ (script lives in Nymeria/tools/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nymeria.core.memory_index import MemoryIndex  # noqa: E402
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
    # A probe with no predicate at all matches nothing (avoid silent all-hits).
    has_predicate = any(
        x is not None for x in (
            probe.relevant_ids, probe.contains_any, probe.contains_all,
            probe.chunk_type, probe.thread_id,
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


def evaluate(
    index: MemoryIndex,
    probes: List[Probe],
    user_id: str = "eval",
    k: int = 5,
    base_kwargs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run probes and return aggregate + per-query metrics.

    ``base_kwargs`` are search kwargs applied to every probe (e.g. a fusion or
    recency override for a config comparison); a probe's own ``search`` kwargs
    take precedence on conflict.
    """
    base_kwargs = base_kwargs or {}
    hits = 0
    rr_total = 0.0
    p_total = 0.0
    ndcg_total = 0.0
    per_query: List[Dict[str, Any]] = []

    for p in probes:
        kwargs = {**base_kwargs, **p.search}
        results = index.search(p.query, user_id, limit=k, **kwargs)
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
            "ranks": [i for i, r in enumerate(rels, 1) if r],
            "note": p.note,
        })

    n = len(probes) or 1
    return {
        "k": k,
        "n": len(probes),
        "hit_rate": hits / n,
        "mrr": rr_total / n,
        "precision_at_k": p_total / n,
        "ndcg_at_k": ndcg_total / n,
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
    ]


# --- CLI -------------------------------------------------------------------

def _print_metrics(name: str, m: Dict[str, Any], verbose: bool = False) -> None:
    print(f"\n[{name}]  n={m['n']} k={m['k']}")
    print(f"  hit_rate@{m['k']}:   {m['hit_rate']:.3f}")
    print(f"  MRR:          {m['mrr']:.3f}")
    print(f"  precision@{m['k']}: {m['precision_at_k']:.3f}")
    print(f"  nDCG@{m['k']}:      {m['ndcg_at_k']:.3f}")
    if verbose:
        for row in m["per_query"]:
            mark = "ok  " if row["hit"] else "MISS"
            print(f"    [{mark}] rr={row['rr']:.2f} ndcg={row['ndcg']:.2f} "
                  f"ranks={row['ranks']}  {row['query']}")


def _open_index(db: Optional[str]) -> MemoryIndex:
    if db:
        return MemoryIndex(Path(db))
    # Seeded mode uses a throwaway BM25-only index (no API key needed).
    tmp = TemporaryDirectory()
    _open_index._tmp = tmp  # keep alive for the process lifetime
    return MemoryIndex(Path(tmp.name) / "rag_eval.db", embedding_provider="none")


def main() -> None:
    ap = argparse.ArgumentParser(description="RAG retrieval-quality eval harness")
    ap.add_argument("--db", help="Path to a per-user memory.db (live mode)")
    ap.add_argument("--user", default="eval", help="user_id to search within")
    ap.add_argument("--probes", help="Path to a probes JSON file (live mode)")
    ap.add_argument("--k", type=int, default=5, help="top-k cutoff")
    ap.add_argument("--compare", action="store_true",
                    help="compare rrf/weighted x recency on/off")
    ap.add_argument("-v", "--verbose", action="store_true", help="per-query detail")
    args = ap.parse_args()

    # BM25-only seeded mode logs an expected "provider: none" warning per call;
    # quiet the index logger so eval output stays readable.
    logging.getLogger("nymeria.core.memory_index").setLevel(logging.ERROR)

    index = _open_index(args.db)

    if args.probes:
        raw = json.loads(Path(args.probes).read_text())
        probes = [Probe.from_dict(d) for d in raw]
    elif not args.db:
        probes = build_seeded(index, user_id=args.user)
    else:
        ap.error("--db requires --probes (no synthetic corpus to seed a live DB)")
        return

    if args.compare:
        configs = {
            "rrf+recency":    {"fusion": "rrf", "apply_recency": True},
            "rrf, no recency": {"fusion": "rrf", "apply_recency": False},
            "weighted+recency": {"fusion": "weighted", "apply_recency": True},
        }
        for name, m in compare_configs(index, probes, configs,
                                       user_id=args.user, k=args.k).items():
            _print_metrics(name, m, verbose=args.verbose)
    else:
        _print_metrics("eval", evaluate(index, probes, user_id=args.user, k=args.k),
                       verbose=True)


if __name__ == "__main__":
    main()
