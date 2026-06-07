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

A probe may also carry ``search`` kwargs (e.g. ``{"thread_id": "..."}``) passed
straight through to ``MemoryIndex.search``, and an ``anchor_time`` (a date the
question is "as of") that is parsed into the date-anchor search kwargs, so scope
and time-bias behaviour can be evaluated too.

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

    # --retrieval-mode isolates one branch. ``all`` indexes once and scores the
    # hybrid, vector-only and bm25-only branches, so a model A/B reads cleanest
    # on the vector row (the embedding is the only signal there):
    python3 tools/rag_eval.py --dataset longmemeval --data <oracle.json> \
        --embedding-model text-embedding-3-small --retrieval-mode all \
        --out runs/small.json
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
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, List, Optional

# Allow running directly from Nymeria/ (script lives in Nymeria/tools/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nymeria.core.memory_index import (  # noqa: E402
    DEDUP_THRESHOLD,
    ChunkResult,
    MemoryIndex,
    parse_anchor_string,
)
from nymeria.core.time_utils import utc_now  # noqa: E402

# A browser User-Agent for managed APIs fronted by Cloudflare (Jina), which 403
# (error 1010) on urllib's / the openai client's default UA. Harmless elsewhere.
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


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
    # Date the question is asked "as of" (e.g. LongMemEval question_date). When
    # set, it is parsed into anchor search kwargs so the date-anchor biasing is
    # exercised, mirroring how the rag_search tool turns 'around' into an anchor.
    anchor_time: Optional[str] = None
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


# --- single-branch retrieval (decomposition) -------------------------------
#
# The production ``search`` fuses the vector and BM25 branches (RRF), then
# applies recency, prose-priority and dedup. To attribute a result to one
# signal (e.g. "which embedding model is best?"), these helpers run each branch
# in isolation, with no fusion/recency/dedup, ranked exactly as production ranks
# the branch: vector by ascending distance, BM25 by ascending bm25 score. They
# return the same ``ChunkResult`` shape so ``result_is_relevant`` scores them
# identically to a hybrid result.

def _fetch_chunk_results(
    index: MemoryIndex, conn: sqlite3.Connection, ids: List[str], user_id: str
) -> List[ChunkResult]:
    """Map retrieved chunk ids to ChunkResults, preserving retrieval order."""
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, content, chunk_type, thread_id, created_at, event_time, "
        f"metadata, context FROM chunks WHERE id IN ({placeholders}) AND user_id = ?",
        (*ids, user_id),
    ).fetchall()
    by_id = {row["id"]: row for row in rows}
    out: List[ChunkResult] = []
    for cid in ids:
        row = by_id.get(cid)
        if row is None:
            continue
        try:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        except json.JSONDecodeError:
            metadata = {}
        created_at = index._parse_ts(row["created_at"])
        out.append(ChunkResult(
            id=row["id"],
            content=row["content"],
            chunk_type=row["chunk_type"],
            thread_id=row["thread_id"],
            created_at=created_at,
            metadata=metadata,
            score=1.0,
            event_time=index._parse_ts(row["event_time"]) or created_at,
            context=row["context"],
        ))
    return out


def _embed_query(index: MemoryIndex, query: str) -> Optional[List[float]]:
    """Embed a query, applying the query-side ``input_type`` when one is set.

    Mirrors ``MemoryIndex.embed_text`` but, when the harness has configured an
    input_type scheme (Voyage 'query', see ``_open_index``), passes it via the
    OpenAI client's ``extra_body`` so a managed provider gets its asymmetric query
    prompt. With no scheme it defers to ``embed_text``, so the default and local
    paths are byte-for-byte unchanged and production memory_index is untouched.
    """
    it = getattr(index, "_eval_query_input_type", None)
    if not it:
        return index.embed_text(query)
    if not query.strip() or index.embedding_provider != "openai":
        return None
    try:
        client = index._get_openai_client()
        kwargs = index._embed_kwargs()
        param = getattr(index, "_eval_input_type_param", "input_type")
        kwargs["extra_body"] = {param: it}
        resp = client.embeddings.create(input=query[:8000], **kwargs)
        emb = resp.data[0].embedding
        return emb if len(emb) == index.embedding_dimensions else None
    except Exception:  # noqa: BLE001 - caller retries a None
        return None


def _retrieve_vector(
    index: MemoryIndex, query: str, user_id: str, k: int
) -> List[ChunkResult]:
    """Pure vector KNN: the embedding branch alone (no BM25, fusion or recency).

    A model A/B reads cleanest here, since the embedding is the only signal.
    """
    # embed_text swallows a 429 into None; retry a non-empty query (real provider
    # only) so a transient rate limit is not mis-scored as a vector miss. A
    # provider of "none" returns None structurally, so there is nothing to retry.
    emb = _embed_query(index, query)
    if not emb and query.strip() and index.embedding_provider != "none":
        delay = 2.0
        for _ in range(3):
            time.sleep(delay)
            delay = min(delay * 2, 16.0)
            emb = _embed_query(index, query)
            if emb:
                break
    if not emb:
        return []
    conn = index._get_connection()
    try:
        cur = conn.execute(
            "SELECT chunk_id FROM vec_chunks WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT ?",
            (index._serialize_embedding(emb), k),
        )
        ids = [row["chunk_id"] for row in cur.fetchall()]
        return _fetch_chunk_results(index, conn, ids, user_id)
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _retrieve_bm25(
    index: MemoryIndex, query: str, user_id: str, k: int
) -> List[ChunkResult]:
    """Pure BM25 keyword retrieval: the lexical floor, no embeddings involved."""
    fts_query = index._fts_match_query(query)
    if not fts_query:
        return []
    conn = index._get_connection()
    try:
        cur = conn.execute(
            "SELECT c.id FROM chunks_fts fts JOIN chunks c ON c.rowid = fts.rowid "
            "WHERE chunks_fts MATCH ? AND c.user_id = ? ORDER BY bm25(chunks_fts) LIMIT ?",
            (fts_query, user_id, k),
        )
        ids = [row["id"] for row in cur.fetchall()]
        return _fetch_chunk_results(index, conn, ids, user_id)
    finally:
        conn.close()


RETRIEVAL_MODES = ("hybrid", "vector", "bm25")


def _retrieve(
    index: MemoryIndex, query: str, user_id: str, k: int,
    mode: str, base_kwargs: Dict[str, Any],
) -> List[ChunkResult]:
    """Dispatch one probe to the requested retrieval branch."""
    if mode == "vector":
        return _retrieve_vector(index, query, user_id, k)
    if mode == "bm25":
        return _retrieve_bm25(index, query, user_id, k)
    return index.search(query, user_id, limit=k, **base_kwargs)


class CrossEncoderReranker:
    """Second-stage cross-encoder reranker for the eval A/B.

    Reorders a first-stage candidate pool by joint (query, passage) relevance,
    the standard "lexical/dense recall, cross-encoder precision" pattern. Wraps a
    sentence-transformers ``CrossEncoder`` on CPU; lazy-imports torch so the
    no-rerank and seeded/CI paths never pull it in. Use this to measure whether a
    reranker is worth packaging before wiring a real one into production
    (``core/rag_quality.py`` today ships only an LLM listwise reranker).
    """

    def __init__(
        self,
        model_name: str,
        max_length: int = 512,
        backend: str = "torch",
        onnx_file: Optional[str] = None,
        device: str = "cpu",
    ) -> None:
        from sentence_transformers import CrossEncoder

        self.model_name = model_name
        kwargs: Dict[str, Any] = {}
        if backend == "onnx":
            kwargs["backend"] = "onnx"
            if onnx_file:
                # Pick a specific exported graph, e.g. onnx/model_quint8_avx2.onnx
                # (the int8 build for an avx2-only CPU, our production target).
                kwargs["model_kwargs"] = {"file_name": onnx_file}
        self._model = CrossEncoder(
            model_name, max_length=max_length, device=device, **kwargs
        )

    def rerank(self, query: str, results: List[ChunkResult]) -> List[ChunkResult]:
        if len(results) < 2:
            return results
        pairs = [(query, r.content or "") for r in results]
        scores = self._model.predict(
            pairs, convert_to_numpy=True, show_progress_bar=False
        )
        order = sorted(
            range(len(results)), key=lambda i: float(scores[i]), reverse=True
        )
        return [results[i] for i in order]


class APIReranker:
    """Managed rerank-API client for the eval A/B (Cohere, ZeroEntropy, Voyage,
    Jina). Same contract as CrossEncoderReranker: reorder the first-stage pool by
    the API's relevance scores. Raw HTTP (no vendor SDK); the key is read from the
    provider's env var so it never lands in a file, log or run dump. 429/5xx back
    off and retry.
    """

    PROVIDERS = {
        # provider -> (endpoint, api-key env var)
        "cohere": ("https://api.cohere.com/v2/rerank", "COHERE_API_KEY"),
        "zeroentropy": ("https://api.zeroentropy.dev/v1/models/rerank",
                        "ZEROENTROPY_API_KEY"),
        "voyage": ("https://api.voyageai.com/v1/rerank", "VOYAGE_API_KEY"),
        "jina": ("https://api.jina.ai/v1/rerank", "JINA_API_KEY"),
    }

    def __init__(self, provider: str, model: str, doc_max_chars: int = 2000,
                 timeout: float = 60.0, min_interval: float = 0.0) -> None:
        if provider not in self.PROVIDERS:
            raise SystemExit(f"unknown --rerank-provider {provider}")
        self.provider = provider
        self.model = model
        # Pace calls to honour a tight trial rate limit (e.g. Cohere trial =
        # 10/min): sleep so consecutive calls are >= min_interval apart.
        self.min_interval = min_interval
        self._last_call = 0.0
        self.url, env = self.PROVIDERS[provider]
        key = os.environ.get(env)
        if not key:
            raise SystemExit(
                f"set {env} in the environment to use --rerank-provider {provider}"
            )
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # Jina sits behind Cloudflare, which 403s (error 1010) on the default
            # urllib User-Agent; a browser UA is required there and harmless for
            # Cohere/Voyage/ZeroEntropy.
            "User-Agent": _BROWSER_UA,
        }
        self.doc_max_chars = doc_max_chars
        self.timeout = timeout

    def rerank(self, query: str, results: List[ChunkResult]) -> List[ChunkResult]:
        if len(results) < 2:
            return results
        docs = [(r.content or "")[: self.doc_max_chars] for r in results]
        payload = {
            "model": self.model,
            "query": query,
            "documents": docs,
        }
        # Voyage's rerank API names this top_k; Cohere/Jina/ZeroEntropy use top_n.
        payload["top_k" if self.provider == "voyage" else "top_n"] = len(docs)
        if self.min_interval:
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
        order = self._post_with_retry(payload, len(docs))
        return [results[i] for i in order]

    def _post_with_retry(self, payload: Dict[str, Any], n: int) -> List[int]:
        import urllib.error
        import urllib.request

        data = json.dumps(payload).encode()
        delay = 2.0
        for attempt in range(6):
            try:
                req = urllib.request.Request(
                    self.url, data=data, headers=self._headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode())
                return self._parse_order(body, n)
            except urllib.error.HTTPError as e:
                retryable = e.code in (408, 429, 500, 502, 503, 529)
                if retryable and attempt < 5:
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                detail = e.read().decode()[:200] if hasattr(e, "read") else ""
                raise SystemExit(f"{self.provider} rerank HTTP {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError):
                if attempt < 5:
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                raise
        return list(range(n))

    @staticmethod
    def _parse_order(body: Dict[str, Any], n: int) -> List[int]:
        # Cohere/Jina/ZeroEntropy: {"results":[{"index","relevance_score"}...]}
        # (already sorted desc). Voyage: {"data":[...]}. Be defensive: if entries
        # carry a score but no index, argsort; always backfill missing indices.
        items = body.get("results")
        if items is None:
            items = body.get("data", [])
        if items and isinstance(items[0], dict) and "index" in items[0]:
            order = [it["index"] for it in items]
        elif items and isinstance(items[0], dict):
            scored = [(i, it.get("relevance_score", it.get("score", 0.0)))
                      for i, it in enumerate(items)]
            order = [i for i, _ in sorted(scored, key=lambda x: x[1], reverse=True)]
        else:
            order = list(range(n))
        seen = set(order)
        order += [i for i in range(n) if i not in seen]
        return order[:n]


class CohereEmbedder:
    """Native Cohere ``v2/embed`` client for the eval (Cohere embed-v4.0 is NOT
    OpenAI-compatible, so it cannot go through ``MemoryIndex``'s OpenAI client).
    Asymmetric: pass ``input_type`` ``search_query`` for queries and
    ``search_document`` for the corpus. ``output_dimension`` truncates the
    Matryoshka vector (1536 default/best, also 1024/512/256). Raw HTTP, key from
    ``COHERE_API_KEY``, 429/5xx backoff. Returns vectors aligned to ``texts``
    (None per text only after retries are exhausted)."""

    URL = "https://api.cohere.com/v2/embed"
    MAX_BATCH = 96  # Cohere v2/embed cap

    def __init__(self, model: str = "embed-v4.0", dim: int = 1536,
                 timeout: float = 120.0) -> None:
        key = os.environ.get("COHERE_API_KEY")
        if not key:
            raise SystemExit("set COHERE_API_KEY to use CohereEmbedder")
        self.model = model
        self.dim = dim
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": _BROWSER_UA,
        }

    def embed(self, texts: List[str], input_type: str) -> List[Optional[List[float]]]:
        out: List[Optional[List[float]]] = []
        for start in range(0, len(texts), self.MAX_BATCH):
            batch = [(t[:8000] if t and t.strip() else " ")
                     for t in texts[start:start + self.MAX_BATCH]]
            vecs = self._post_with_retry({
                "model": self.model,
                "input_type": input_type,
                "embedding_types": ["float"],
                "output_dimension": self.dim,
                "texts": batch,
            }, len(batch))
            out.extend(vecs)
        return out

    def embed_one(self, text: str, input_type: str) -> Optional[List[float]]:
        return self.embed([text], input_type)[0]

    def _post_with_retry(self, payload: Dict[str, Any], n: int) -> List[Optional[List[float]]]:
        import urllib.error
        import urllib.request

        data = json.dumps(payload).encode()
        delay = 2.0
        for attempt in range(7):
            try:
                req = urllib.request.Request(
                    self.URL, data=data, headers=self._headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode())
                floats = (body.get("embeddings") or {}).get("float") or []
                vecs: List[Optional[List[float]]] = [
                    (v if len(v) == self.dim else None) for v in floats
                ]
                vecs += [None] * (n - len(vecs))  # defensive: align to batch
                return vecs[:n]
            except urllib.error.HTTPError as e:
                retryable = e.code in (408, 429, 500, 502, 503, 529)
                if retryable and attempt < 6:
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                detail = e.read().decode()[:200] if hasattr(e, "read") else ""
                raise SystemExit(f"cohere embed HTTP {e.code}: {detail}")
            except (urllib.error.URLError, TimeoutError):
                if attempt < 6:
                    time.sleep(delay)
                    delay = min(delay * 2, 30.0)
                    continue
                raise
        return [None] * n


def _force_ua_client(index: MemoryIndex, ua: str = _BROWSER_UA) -> MemoryIndex:
    """Pre-seed the index's OpenAI client with a browser User-Agent (harness-only).

    Jina's OpenAI-compatible endpoint is behind Cloudflare, which 403s (error
    1010) on the openai client's default UA. Building the client here (instead of
    the lazy ``_get_openai_client``) injects ``default_headers`` without touching
    production ``memory_index.py``. Requires ``embedding_api_key`` (EMBEDDING_API_KEY)
    to be set."""
    from openai import OpenAI

    kwargs: Dict[str, Any] = {"api_key": index.embedding_api_key,
                              "default_headers": {"User-Agent": ua}}
    if index.embedding_base_url:
        kwargs["base_url"] = index.embedding_base_url
    index._openai_client = OpenAI(**kwargs)
    return index


def evaluate(
    index: MemoryIndex,
    probes: List[Probe],
    user_id: str = "eval",
    k: int = 5,
    base_kwargs: Optional[Dict[str, Any]] = None,
    distinct_threshold: float = DEDUP_THRESHOLD,
    retrieval_mode: str = "hybrid",
    reranker: Optional["CrossEncoderReranker"] = None,
    rerank_pool: int = 30,
) -> Dict[str, Any]:
    """Run probes and return aggregate + per-query metrics.

    ``base_kwargs`` are search kwargs applied to every probe (e.g. a fusion or
    recency override for a config comparison); a probe's own ``search`` kwargs
    take precedence on conflict. ``retrieval_mode`` picks the branch: ``hybrid``
    (production fused search), ``vector`` (embedding KNN alone) or ``bm25``
    (keyword alone); the single-branch modes ignore ``base_kwargs`` knobs.

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
        # Turn the probe's question date into anchor kwargs (explicit search/
        # base_kwargs anchor settings still win). base_kwargs may pass
        # anchor_off=True to suppress it for an A/B comparison.
        if p.anchor_time and not kwargs.pop("anchor_off", False):
            spec = parse_anchor_string(p.anchor_time)
            if spec is not None:
                kwargs.setdefault("anchor_start", spec.start)
                kwargs.setdefault("anchor_end", spec.end)
                kwargs.setdefault("anchor_edge_sigma_days", spec.edge_sigma_days)
        else:
            kwargs.pop("anchor_off", None)
        # When reranking, pull a wider first-stage pool so the cross-encoder can
        # promote a relevant chunk from beyond the top-k before the k-cut.
        retrieve_k = max(k, rerank_pool) if reranker is not None else k
        results = _retrieve(index, p.query, user_id, retrieve_k, retrieval_mode, kwargs)
        if reranker is not None:
            results = reranker.rerank(p.query, results)
        results = results[:k]
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
    "gemini-embedding-001": 3072,
    "gemini-embedding-2": 3072,
    "voyage-3-large": 1024,
    "voyage-3.5": 1024,
    "voyage-3.5-lite": 1024,
    "voyage-4-large": 1024,
    "voyage-4": 1024,
    "voyage-4-lite": 1024,
    "voyage-context-3": 1024,
    "embeddinggemma": 768,
    # Local CPU candidates served via tools/local_embed_server.py (the model name
    # is the HuggingFace id; dim is its native sentence-embedding width). Static
    # retrieval is Matryoshka-truncatable (1024 -> 256) via the server's
    # --truncate-dim, in which case pass --embedding-dim 256 to match.
    "ibm-granite/granite-embedding-small-english-r2": 384,
    "ibm-granite/granite-embedding-english-r2": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "Alibaba-NLP/gte-modernbert-base": 768,
    "sentence-transformers/static-retrieval-mrl-en-v1": 1024,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "intfloat/e5-small-v2": 384,
    "intfloat/multilingual-e5-small": 384,
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


# --- batched indexing ------------------------------------------------------
#
# Indexing a large haystack one embed-per-chunk (which is what add_chunk does)
# is thousands of API calls and trips free-tier rate limits. These helpers
# batch the vector backfill (~128 chunks/call). Unlike production's
# ``_embed_texts``, they map results by RESPONSE ORDER, not ``item.index``, so
# they tolerate OpenAI-compatible shims that leave ``index`` unset (Gemini's
# endpoint returns ``index=None`` for the first item). Order preservation is
# verified; a per-item fallback covers any batch that returns a mismatched count.

def _is_transient(e: Exception) -> bool:
    """A rate-limit / timeout / connection blip worth retrying (not a 400)."""
    name = type(e).__name__
    return ("RateLimit" in name or "Timeout" in name or "APIConnection" in name
            or "429" in str(e))


def _embed_call(client, payload, embed_kwargs, max_retries: int = 6):
    """``embeddings.create`` with exponential backoff on transient errors.

    A 429 must NOT silently drop a chunk's vector (that corrupts the vector
    branch), so we wait and retry the same payload. Non-transient errors (e.g. a
    400 batch-too-large) raise immediately so the caller can fall back.
    """
    delay = 2.0
    for attempt in range(max_retries + 1):
        try:
            return client.embeddings.create(input=payload, **embed_kwargs)
        except Exception as e:  # noqa: BLE001
            if attempt >= max_retries or not _is_transient(e):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


def _embed_texts_ordered(index: MemoryIndex, texts: List[str]) -> List[Optional[List[float]]]:
    """Batch-embed ``texts``, mapping by position. Returns a list aligned to
    ``texts`` (None only after retries are exhausted or the dimension is wrong).

    Transient 429s back off and retry; a hard error (e.g. Gemini's 400 for an
    over-large batch) drops to per-item, which itself retries each item."""
    if not texts:
        return []
    client = index._get_openai_client()
    inputs = [(t[:8000] if t and t.strip() else " ") for t in texts]
    dim = index.embedding_dimensions
    # Document-side input_type (Voyage 'document'), when a scheme is configured,
    # so the corpus is embedded with the provider's asymmetric document prompt.
    embed_kwargs = index._embed_kwargs()
    doc_it = getattr(index, "_eval_doc_input_type", None)
    if doc_it:
        param = getattr(index, "_eval_input_type_param", "input_type")
        embed_kwargs["extra_body"] = {param: doc_it}

    def _ok(emb: List[float]) -> Optional[List[float]]:
        return emb if len(emb) == dim else None

    try:
        resp = _embed_call(client, inputs, embed_kwargs)
        if len(resp.data) == len(inputs):
            return [_ok(item.embedding) for item in resp.data]
        print(f"  batch returned {len(resp.data)}/{len(inputs)}; per-item fallback",
              flush=True)
    except Exception as e:  # noqa: BLE001 - hard error -> per-item fallback
        print(f"  batch embed failed ({type(e).__name__}); per-item fallback",
              flush=True)

    out: List[Optional[List[float]]] = []
    for t in inputs:
        try:
            r = _embed_call(client, t, embed_kwargs)
            out.append(_ok(r.data[0].embedding))
        except Exception:  # noqa: BLE001
            out.append(None)
    return out


def _batch_backfill(index: MemoryIndex, user_id: str, batch_size: int = 128) -> Dict[str, int]:
    """Vector-backfill every chunk lacking an embedding, in batches.

    Order-tolerant copy of ``MemoryIndex.backfill_embeddings`` (same select,
    embed-text and insert), used after a deferred BM25-only load so a 5-model x
    500-question sweep does not hammer the embedding APIs one call per turn.
    """
    conn = index._get_connection()
    try:
        rows = conn.execute(
            "SELECT id, content, context FROM chunks WHERE user_id = ? "
            "AND id NOT IN (SELECT chunk_id FROM vec_chunks)",
            (user_id,),
        ).fetchall()
        total = len(rows)
        embedded = 0
        for start in range(0, total, batch_size):
            batch = rows[start:start + batch_size]
            texts = [
                (f"{r['context']}\n\n{r['content']}" if r["context"] else r["content"])
                for r in batch
            ]
            for row, vec in zip(batch, _embed_texts_ordered(index, texts)):
                if vec is None:
                    continue
                conn.execute(
                    "INSERT INTO vec_chunks (chunk_id, embedding) VALUES (?, ?)",
                    (row["id"], index._serialize_embedding(vec)),
                )
                embedded += 1
            conn.commit()
            print(f"  embedded {min(start + batch_size, total)}/{total}", flush=True)
        return {"embedded": embedded, "total": total}
    finally:
        conn.close()


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
    date anchor are genuinely exercised), and each question becomes a Probe whose
    ``relevant_thread_ids`` are its ``answer_session_ids`` and whose
    ``anchor_time`` is its ``question_date``.

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
        # The question's "as of" date, anchored at day precision so the
        # date-anchor path is exercised. Note this anchors on WHEN THE QUESTION
        # IS ASKED, not the (often earlier) time its evidence is from, so on
        # LongMemEval it mostly reproduces a bias toward the most recent
        # sessions; the clean signal for the feature is a purpose-built probe
        # whose anchor_time matches the time its evidence is from. Compare with
        # --compare (the "anchor off" config passes anchor_off=True).
        q_date = _parse_lme_date(entry.get("question_date"))
        anchor_time = q_date.date().isoformat() if q_date else None
        # A no-answer probe is one with no evidence session in the haystack.
        # _abs questions in the oracle still carry labelled gold sessions, so
        # they are scored as answerable retrieval (abstention is judged at
        # generation time, not here).
        probes.append(Probe(
            query=entry.get("question", ""),
            relevant_thread_ids=gold,
            expect_empty=not gold,
            anchor_time=anchor_time,
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


# input_type scheme -> (extra_body param name, query value, document value). Lets
# a managed provider embed queries and documents with its asymmetric retrieval
# prompts, which materially helps retrieval for Voyage/Jina-class models and is the
# fair config for a "best-in-class embeddings" headline. The param name differs by
# vendor: Voyage uses input_type=query/document, Jina uses task=retrieval.query/
# retrieval.passage (both over the OpenAI-compatible embeddings endpoint).
_INPUT_TYPE_SCHEMES = {
    "voyage": ("input_type", "query", "document"),
    "jina": ("task", "retrieval.query", "retrieval.passage"),
}


def _apply_input_type(index: MemoryIndex, scheme: Optional[str]) -> MemoryIndex:
    """Tag the index with the harness-only query/document input_type for ``scheme``.

    Attributes are read by ``_embed_query`` (queries) and ``_embed_texts_ordered``
    (documents); they exist only on the harness's throwaway index, so production
    memory_index is never touched.
    """
    if scheme:
        param, q, d = _INPUT_TYPE_SCHEMES.get(scheme, ("input_type", scheme, scheme))
        index._eval_query_input_type = q
        index._eval_doc_input_type = d
        index._eval_input_type_param = param
    return index


def _open_index(
    db: Optional[str],
    *,
    embedding_provider: Optional[str] = None,
    embedding_model: Optional[str] = None,
    embedding_dimensions: Optional[int] = None,
    embedding_base_url: Optional[str] = None,
    embedding_input_type: Optional[str] = None,
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
        return _apply_input_type(MemoryIndex(Path(db), **kwargs), embedding_input_type)
    kwargs["embedding_provider"] = embedding_provider or "none"
    tmp = TemporaryDirectory()
    _open_index._tmp = tmp  # keep alive for the process lifetime
    return _apply_input_type(
        MemoryIndex(Path(tmp.name) / "rag_eval.db", **kwargs), embedding_input_type
    )


_DUMP_KEYS = ("hit_rate", "mrr", "precision_at_k", "ndcg_at_k",
              "distinct_at_k", "false_positive_rate", "n", "n_empty")


def _dump_run(
    path: str,
    *,
    dataset: str,
    embedding_model: Optional[str],
    embedding_dimensions: int,
    embedding_provider: str,
    metrics: Dict[str, Any],
    retrieval_mode: str = "hybrid",
    rerank_model: Optional[str] = None,
) -> None:
    """Write a run's config + headline metrics for later ``--compare-runs``."""
    payload = {
        "dataset": dataset,
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "embedding_provider": embedding_provider,
        "retrieval_mode": retrieval_mode,
        "rerank_model": rerank_model,
        "k": metrics["k"],
        "metrics": {key: metrics[key] for key in _DUMP_KEYS},
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {path}")


def _dump_modes(
    path: str,
    *,
    dataset: str,
    embedding_model: Optional[str],
    embedding_dimensions: int,
    embedding_provider: str,
    k: int,
    mode_metrics: Dict[str, Dict[str, Any]],
    rerank_model: Optional[str] = None,
) -> None:
    """Write one ``--retrieval-mode all`` run (all branches over one index)."""
    payload = {
        "dataset": dataset,
        "embedding_model": embedding_model,
        "embedding_dimensions": embedding_dimensions,
        "embedding_provider": embedding_provider,
        "rerank_model": rerank_model,
        "k": k,
        "modes": {
            mode: {key: m[key] for key in _DUMP_KEYS}
            for mode, m in mode_metrics.items()
        },
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {path}")


def _print_run_comparison(paths: List[str]) -> None:
    """Print a side-by-side table of dumped runs.

    Handles both single-mode dumps (``--out``) and all-mode dumps
    (``--retrieval-mode all --out``); the latter expands to one row per branch.
    """
    print(f"\n{'model [mode]':<34}{'dim':>6}{'k':>4}{'hit':>8}{'mrr':>8}"
          f"{'ndcg':>8}{'distinct':>10}{'fp':>7}")
    print("-" * 85)

    def _row(label: str, dim, k, m: Dict[str, Any]) -> None:
        print(f"{label:<34}{dim:>6}{k:>4}"
              f"{m.get('hit_rate', 0.0):>8.3f}"
              f"{m.get('mrr', 0.0):>8.3f}"
              f"{m.get('ndcg_at_k', 0.0):>8.3f}"
              f"{m.get('distinct_at_k', 0.0):>10.3f}"
              f"{m.get('false_positive_rate', 0.0):>7.3f}")

    for p in paths:
        d = json.loads(Path(p).read_text())
        model = d.get("embedding_model") or "?"
        dim = d.get("embedding_dimensions", "?")
        k = d.get("k", "?")
        rr_model = d.get("rerank_model")
        rr = f" +rr:{os.path.basename(rr_model)}" if rr_model else ""
        if "modes" in d:
            for mode in RETRIEVAL_MODES:
                if mode in d["modes"]:
                    _row(f"{model} [{mode}]{rr}", dim, k, d["modes"][mode])
        else:
            mode = d.get("retrieval_mode", "hybrid")
            label = model if mode == "hybrid" else f"{model} [{mode}]"
            _row(f"{label}{rr}", dim, k, d.get("metrics", {}))


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
    ap.add_argument("--embedding-input-type", choices=sorted(_INPUT_TYPE_SCHEMES),
                    help="asymmetric query/document input_type scheme (e.g. voyage): "
                         "embeds queries and documents with the provider's retrieval "
                         "prompts via extra_body. Off (symmetric) when unset.")
    ap.add_argument("--k", type=int, default=5, help="top-k cutoff")
    ap.add_argument("--retrieval-mode", choices=[*RETRIEVAL_MODES, "all"],
                    default="hybrid",
                    help="retrieval branch: hybrid (default), vector (embedding "
                         "KNN alone), bm25 (keyword alone), or all (one index, "
                         "all three branches; isolates the embedding model)")
    ap.add_argument("--rerank-model",
                    help="cross-encoder model id to rerank the first-stage pool "
                         "(sentence-transformers CrossEncoder, CPU); off when unset")
    ap.add_argument("--rerank-pool", type=int, default=30,
                    help="first-stage candidates fetched and reranked before the "
                         "top-k cut (default 30)")
    ap.add_argument("--rerank-max-len", type=int, default=512,
                    help="cross-encoder max sequence length in tokens (default 512)")
    ap.add_argument("--rerank-backend", choices=["torch", "onnx"], default="torch",
                    help="reranker runtime: torch (default) or onnx (faster on CPU)")
    ap.add_argument("--rerank-onnx-file",
                    help="onnx graph to load when --rerank-backend onnx, e.g. "
                         "onnx/model_quint8_avx2.onnx (int8 for avx2 CPUs)")
    ap.add_argument("--rerank-provider",
                    choices=["local", "cohere", "zeroentropy", "voyage", "jina"],
                    default="local",
                    help="reranker source: local cross-encoder (default) or a "
                         "managed rerank API (key from <PROVIDER>_API_KEY env)")
    ap.add_argument("--rerank-doc-max-chars", type=int, default=2000,
                    help="truncate each candidate before an API rerank (cost/latency)")
    ap.add_argument("--rerank-min-interval", type=float, default=0.0,
                    help="min seconds between API rerank calls (pace a trial rate "
                         "limit, e.g. 6.2 for Cohere trial's 10/min)")
    ap.add_argument("--vec-weight", type=float,
                    help="RRF weight on the vector branch (hybrid only; default 1.0). "
                         "Raise above --bm25-weight to bias fusion toward dense "
                         "retrieval, e.g. for a strong embedding model.")
    ap.add_argument("--bm25-weight", type=float,
                    help="RRF weight on the BM25 branch (hybrid only; default 1.0)")
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
            embedding_input_type=args.embedding_input_type,
        )
        provider = index.embedding_provider
        print(f"indexing LongMemEval haystack from {args.data} "
              f"(model={index.embedding_model}, dim={index.embedding_dimensions}, "
              f"provider={provider}) ...")
        # Defer vectors: load BM25-only (no per-chunk embed), then batch-backfill
        # in ~128-chunk calls, so a multi-model sweep does not hit free-tier rate
        # limits with thousands of one-at-a-time embed calls.
        index.embedding_provider = "none"
        probes = load_longmemeval(index, args.data, user_id=args.user, limit=args.limit)
        index.embedding_provider = provider
        print(f"built {len(probes)} probes; haystack indexed (BM25)")
        if provider != "none":
            print("batch-embedding haystack vectors ...")
            stats = _batch_backfill(index, args.user)
            print(f"embedded {stats['embedded']}/{stats['total']} chunks")
    elif args.probes:
        # Embedding overrides let a live-probes run query a db embedded by a
        # non-default model (e.g. a locally re-embedded corpus for a model/rerank
        # A/B). Defaults reproduce the previous settings-driven behaviour.
        index = _open_index(
            args.db,
            embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model,
            embedding_dimensions=args.embedding_dim,
            embedding_base_url=args.embedding_base_url,
            embedding_input_type=args.embedding_input_type,
        )
        raw = json.loads(Path(args.probes).read_text())
        probes = [Probe.from_dict(d) for d in raw]
    elif not args.db:
        index = _open_index(None)
        probes = build_seeded(index, user_id=args.user)
    else:
        ap.error("--db requires --probes or --dataset (no synthetic corpus to seed a live DB)")
        return

    reranker = None
    if args.rerank_model:
        if args.rerank_provider == "local":
            print(f"loading reranker {args.rerank_model} "
                  f"(backend={args.rerank_backend}, max_len={args.rerank_max_len}) ...")
            reranker = CrossEncoderReranker(
                args.rerank_model,
                max_length=args.rerank_max_len,
                backend=args.rerank_backend,
                onnx_file=args.rerank_onnx_file,
            )
        else:
            print(f"using {args.rerank_provider} rerank API "
                  f"(model={args.rerank_model}) ...")
            reranker = APIReranker(
                args.rerank_provider,
                args.rerank_model,
                doc_max_chars=args.rerank_doc_max_chars,
                min_interval=args.rerank_min_interval,
            )
        print(f"reranking first-stage top-{args.rerank_pool} -> top-{args.k}")

    if args.compare:
        configs = {
            "rrf+recency+dedup": {"fusion": "rrf", "apply_recency": True, "dedup": True},
            "rrf, dedup off":    {"fusion": "rrf", "apply_recency": True, "dedup": False},
            "rrf, no recency":   {"fusion": "rrf", "apply_recency": False},
            "weighted+recency":  {"fusion": "weighted", "apply_recency": True},
            # Date-anchor A/B: "on" applies each probe's anchor_time, "off"
            # suppresses it. Meaningful only when probes carry anchor_time.
            "anchor on":         {"fusion": "rrf", "apply_recency": False},
            "anchor off":        {"fusion": "rrf", "apply_recency": False, "anchor_off": True},
        }
        results = compare_configs(index, probes, configs, user_id=args.user, k=args.k)
        for name, m in results.items():
            _print_metrics(name, m, verbose=args.verbose)
        primary = next(iter(results.values()))
    elif args.retrieval_mode == "all":
        # One index, all three branches. Vector is the headline for a model A/B.
        mode_metrics = {
            mode: evaluate(index, probes, user_id=args.user, k=args.k,
                           retrieval_mode=mode, reranker=reranker,
                           rerank_pool=args.rerank_pool)
            for mode in RETRIEVAL_MODES
        }
        for mode in RETRIEVAL_MODES:
            _print_metrics(f"{args.dataset} [{mode}]", mode_metrics[mode],
                           verbose=args.verbose)
        if args.out:
            _dump_modes(
                args.out,
                dataset=args.dataset,
                embedding_model=index.embedding_model,
                embedding_dimensions=index.embedding_dimensions,
                embedding_provider=index.embedding_provider,
                k=args.k,
                mode_metrics=mode_metrics,
                rerank_model=args.rerank_model,
            )
        return
    else:
        # RRF branch weights bias the hybrid fusion (no effect on the single-branch
        # vector/bm25 modes, which ignore base_kwargs).
        base_kwargs: Dict[str, Any] = {}
        if args.vec_weight is not None:
            base_kwargs["vec_weight"] = args.vec_weight
        if args.bm25_weight is not None:
            base_kwargs["bm25_weight"] = args.bm25_weight
        primary = evaluate(index, probes, user_id=args.user, k=args.k,
                           base_kwargs=base_kwargs or None,
                           retrieval_mode=args.retrieval_mode, reranker=reranker,
                           rerank_pool=args.rerank_pool)
        _print_metrics(f"{args.dataset} [{args.retrieval_mode}]", primary,
                       verbose=True)

    if args.out:
        _dump_run(
            args.out,
            dataset=args.dataset,
            embedding_model=index.embedding_model,
            embedding_dimensions=index.embedding_dimensions,
            embedding_provider=index.embedding_provider,
            metrics=primary,
            retrieval_mode=args.retrieval_mode,
            rerank_model=args.rerank_model,
        )


if __name__ == "__main__":
    main()
