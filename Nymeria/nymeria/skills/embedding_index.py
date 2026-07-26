"""Skill embedding index — semantic search over installed + marketplace skills.

Fallback chain (highest quality first):
    1. Configured embeddings (shared ``core/embedding_client.py`` provider
       dispatch: openai-compatible, cohere, gemini, or local
       sentence-transformers) via sqlite-vec cosine similarity
    2. SQLite FTS5 / BM25 over name+description (keyword, no model)
    3. Substring match (final safety net)

The index is namespaced so the same store can hold `installed` plus one
namespace per marketplace source (e.g. `marketplace:anthropic`). The tool
layer is responsible for triggering rebuilds when the underlying data
changes — this class is a passive store.

Uses the same embedding configuration (and, for local models, the same
process-cached model weights) as Nymeria's memory index.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..core.embedding_client import EmbeddingClient, embedder_stamp

logger = logging.getLogger(__name__)

# Legacy defaults, kept for construction without explicit settings; the live
# values come from the EMBEDDING_* settings via the construction site.
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_MODEL = "text-embedding-3-small"

# Search-query embeds are latency-sensitive (a hang stalls the agent turn) and
# the rebuild path embeds each namespace in one batched call, so a hung remote
# endpoint costs at most one bounded stall per call before degrading to
# FTS5/keyword search. (The OpenAI SDK would otherwise default to a 600s
# timeout with 2 retries.)
EMBED_REQUEST_TIMEOUT_SECONDS = 10.0

# Rebuilds embed a namespace in capped batches (one request or local encode per
# batch), matching the tool-search warm and the memory backfill, so a large
# marketplace namespace cannot blow a provider's per-request ceiling or hand a
# single giant encode to torch.
EMBED_BATCH_SIZE = 128

# Per-request ceiling for those rebuild batches. The base client budget
# (EMBED_REQUEST_TIMEOUT_SECONDS) is query-shaped; a 128-document batch needs
# more headroom, on the native cohere/gemini path especially, where this
# override is the only bound that applies.
EMBED_BATCH_TIMEOUT_SECONDS = 60.0

# After a remote embed failure, the client skips remote calls for this long
# (fast keyword fallback, honest warning kept) instead of re-paying the timeout
# on every per-turn search against a dead endpoint. This replaces the old
# permanent process-wide latch.
EMBED_FAILURE_COOLDOWN_SECONDS = 60.0

# Skills we index — accepts either a Skill object or a MarketplaceSkillEntry-shaped object.
# Both have .name and .description; indexable attrs below are all optional.
SearchMode = str  # "semantic" | "bm25" | "substring"


@dataclass
class SearchHit:
    name: str
    description: str
    score: float
    extra: Dict


@dataclass
class SearchResponse:
    results: List[SearchHit]
    mode: SearchMode
    warning: Optional[str] = None

    def to_json(self) -> Dict:
        return {
            "count": len(self.results),
            "mode": self.mode,
            "warning": self.warning,
            "results": [
                {
                    "name": h.name,
                    "description": h.description,
                    "score": round(h.score, 4),
                    **h.extra,
                }
                for h in self.results
            ],
        }


class SkillEmbeddingIndex:
    """sqlite-vec backed, namespaced, with FTS5 + substring fallbacks."""

    def __init__(
        self,
        db_path: Path,
        embedding_provider: Optional[str] = None,
        embedding_api_key: Optional[str] = None,
        embedding_base_url: Optional[str] = None,
        embedding_model: str = EMBEDDING_MODEL,
        embedding_dimensions: Optional[int] = None,
        embedding_input_type: Optional[str] = None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._provider = (embedding_provider or "openai").strip().lower()
        self._embedding_model = embedding_model or EMBEDDING_MODEL
        self._dimensions = int(embedding_dimensions or EMBEDDING_DIMENSIONS)
        self._input_type = embedding_input_type
        self._client = EmbeddingClient(
            provider=self._provider,
            api_key=embedding_api_key,
            base_url=embedding_base_url,
            model=self._embedding_model,
            dimensions=self._dimensions,
            dimensions_explicit=embedding_dimensions is not None,
            input_type=embedding_input_type,
            timeout=EMBED_REQUEST_TIMEOUT_SECONDS,
            max_retries=0,
            native_timeout=EMBED_REQUEST_TIMEOUT_SECONDS,
            failure_cooldown_seconds=EMBED_FAILURE_COOLDOWN_SECONDS,
        )
        self._semantic_available: Optional[bool] = None  # lazy-probed
        self._last_error: Optional[str] = None
        self._vec_available = False
        self._lock = threading.RLock()
        # Cached SQLite connection (lazily opened on the first search, sqlite-vec
        # loaded once). The instance is long-lived (constructed once per agent
        # and held by SkillManager), and the latency-sensitive search() path runs
        # on every agent turn, so reusing one connection avoids re-opening the DB
        # and re-loading the sqlite-vec C extension on each search. All DB access
        # is serialized by ``self._lock`` (an RLock), so a single shared
        # connection is safe. ``close()`` releases it; the next search reopens.
        self._conn: Optional[sqlite3.Connection] = None

        self._init_db()

    # ------------------------------------------------------------------
    # connection + schema
    # ------------------------------------------------------------------

    def _open_connection(self) -> sqlite3.Connection:
        """Open a NEW SQLite connection with sqlite-vec loaded.

        Used by the one-shot / occasional methods that manage their own
        short-lived connection (``_init_db`` at construction, ``rebuild``,
        ``clear_namespace``). The repeated hot path (``search``) reuses the
        cached ``_get_connection()`` instead, so the sqlite-vec C extension is
        loaded once there rather than on every call. Sets ``self._vec_available``
        from whether the extension loaded.
        """
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._vec_available = True
        except Exception as e:
            self._vec_available = False
            logger.debug("sqlite-vec unavailable for skills index: %s", e)
        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """Return the cached connection with sqlite-vec loaded, opening it on
        first use.

        Reused across repeated ``search`` calls (the per-turn hot path). Callers
        always hold ``self._lock`` (an RLock), so the single connection is never
        touched concurrently. This pays the sqlite-vec extension load once
        instead of on every search. ``close()`` clears the cache so the next call
        reopens transparently. Searches read in autocommit, so this connection
        always observes rows committed by the short-lived rebuild/clear
        connections.
        """
        if self._conn is None:
            self._conn = self._open_connection()
        return self._conn

    def close(self) -> None:
        """Close the cached connection if open. Idempotent; the next search
        reopens transparently. Used for explicit teardown (e.g. tests) and to
        release the file handle when an index is no longer needed."""
        with self._lock:
            conn = self._conn
            self._conn = None
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.debug("Error closing skills-index connection", exc_info=True)

    def _init_db(self) -> None:
        with self._lock:
            conn = self._open_connection()
            try:
                c = conn.cursor()
                # Metadata — one row per (namespace, name).
                c.execute("""
                    CREATE TABLE IF NOT EXISTS skills_meta (
                        skill_key TEXT PRIMARY KEY,
                        namespace TEXT NOT NULL,
                        name TEXT NOT NULL,
                        description TEXT NOT NULL,
                        extra TEXT DEFAULT '{}'
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_skills_meta_ns ON skills_meta(namespace)")

                # FTS5 over name + description (keyword fallback).
                c.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
                        skill_key UNINDEXED,
                        namespace UNINDEXED,
                        name,
                        description,
                        tokenize='porter unicode61'
                    )
                """)

                # Embedder identity stamp (shared shape: embedder_stamp).
                # Stored vectors are only valid for the (provider, model,
                # dimensions, input_type) that produced them, and the vec0
                # column width is fixed at table creation, so on a mismatch the
                # vector table is dropped and recreated at the current width;
                # the next rebuild re-embeds (installed namespace on every
                # SkillManager construction, marketplace namespaces on their
                # TTL refresh). A legacy un-stamped DB whose config still
                # matches what could have produced it (the pre-stamp world was
                # always an OpenAI-compatible embedder at the 1536 slot with no
                # input_type) is ADOPTED, not wiped, mirroring the tool store's
                # legacy rule. Reconciled only when sqlite-vec is loaded
                # (dropping a vec0 virtual table needs the module registered);
                # the stamp is written in the same pass as the drop, and the
                # safe failure direction is drop-without-stamp (a later boot
                # reconciles again), never stamp-without-drop.
                c.execute("""
                    CREATE TABLE IF NOT EXISTS index_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                """)
                if self._vec_available:
                    current = embedder_stamp(
                        self._provider, self._embedding_model,
                        self._dimensions, self._input_type,
                    )
                    # Read the stamp by its named keys (drift-proof: a foreign
                    # row in the generically named index_meta table must not
                    # force a wipe on every boot).
                    rows = {
                        r["key"]: r["value"]
                        for r in c.execute(
                            "SELECT key, value FROM index_meta WHERE key IN "
                            "('provider', 'model', 'dim', 'input_type')"
                        ).fetchall()
                    }
                    stored = {k: rows.get(k) for k in current}
                    legacy_compatible = (
                        not rows
                        and self._provider == "openai"
                        and self._dimensions == 1536
                        and not self._input_type
                    )
                    if stored != current and not legacy_compatible:
                        c.execute("DROP TABLE IF EXISTS skills_vec")
                    if stored != current:
                        for key, value in current.items():
                            c.execute(
                                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                                (key, value),
                            )

                # Vector table via sqlite-vec.
                if self._vec_available:
                    try:
                        c.execute(f"""
                            CREATE VIRTUAL TABLE IF NOT EXISTS skills_vec USING vec0(
                                skill_key TEXT PRIMARY KEY,
                                embedding FLOAT[{self._dimensions}]
                            )
                        """)
                    except sqlite3.OperationalError as e:
                        if "no such module: vec0" in str(e):
                            self._vec_available = False
                        else:
                            raise
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # embedding provider
    # ------------------------------------------------------------------

    def is_semantic_available(self) -> bool:
        """True when the configured embedder AND sqlite-vec are both usable."""
        if self._semantic_available is not None:
            return self._semantic_available
        if not self._vec_available:
            self._semantic_available = False
            self._last_error = "sqlite-vec extension not loaded"
            return False
        err = self._client.availability_error()
        if err:
            self._semantic_available = False
            self._last_error = err
            return False
        # Lazy-probe on first real use via the embed path instead of upfront.
        self._semantic_available = True
        return True

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def _embed(self, text: str, input_type: str = "document") -> Optional[List[float]]:
        if not text.strip():
            return None
        if not self.is_semantic_available():
            return None
        vec = self._client.embed_text(text, input_type=input_type)
        if vec is None and self._client.last_error:
            # A transient failure degrades this call only; never latch semantic
            # off for the process (the next search or rebuild is free to
            # retry), mirroring the tool-search index.
            self._last_error = self._client.last_error
            logger.warning("skills semantic search degrading: %s", self._last_error)
        return vec

    @staticmethod
    def _pack(vec: List[float]) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec)

    # ------------------------------------------------------------------
    # rebuild
    # ------------------------------------------------------------------

    @staticmethod
    def _indexable_text(name: str, description: str, extra: Dict) -> str:
        """Compose the text that gets embedded — name + desc + allowed_tools."""
        parts = [name, description]
        allowed = extra.get("allowed_tools") or []
        if allowed:
            parts.append("allowed-tools: " + ", ".join(allowed))
        scope = extra.get("scope")
        if scope:
            parts.append(f"scope: {scope}")
        return "\n".join(parts)

    def rebuild(self, namespace: str, items: Iterable) -> Dict:
        """Replace all rows in *namespace* with *items*.

        Each item must have .name and .description. Optional: .allowed_tools,
        .scope, .source, .repo_url. Returns a summary dict with counts.

        If semantic embeddings are available this also refreshes the vector
        table. The FTS5 + metadata tables are always populated.
        """
        with self._lock:
            conn = self._open_connection()
            try:
                c = conn.cursor()
                c.execute("DELETE FROM skills_meta WHERE namespace = ?", (namespace,))
                c.execute("DELETE FROM skills_fts WHERE namespace = ?", (namespace,))
                if self._vec_available:
                    c.execute(
                        "DELETE FROM skills_vec WHERE skill_key LIKE ?",
                        (f"{namespace}::%",),
                    )

                embed_count = 0
                fts_count = 0
                to_embed: List[tuple] = []  # (skill_key, indexable text)
                for item in items:
                    name = getattr(item, "name", None)
                    description = getattr(item, "description", None) or ""
                    if not name:
                        continue
                    skill_key = f"{namespace}::{name}"
                    extra = {
                        "allowed_tools": list(getattr(item, "allowed_tools", None) or []),
                        "scope": getattr(item, "scope", None),
                        "source": getattr(item, "source", None),
                        "repo_url": getattr(item, "repo_url", None),
                    }
                    extra = {k: v for k, v in extra.items() if v is not None and v != []}
                    c.execute(
                        """
                        INSERT INTO skills_meta(skill_key, namespace, name, description, extra)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (skill_key, namespace, name, description, json.dumps(extra)),
                    )
                    c.execute(
                        """
                        INSERT INTO skills_fts(skill_key, namespace, name, description)
                        VALUES (?, ?, ?, ?)
                        """,
                        (skill_key, namespace, name, description),
                    )
                    fts_count += 1
                    to_embed.append(
                        (skill_key, self._indexable_text(name, description, extra))
                    )

                # Embed the namespace in capped batches (a handful of HTTP
                # round trips, or local encodes) instead of one call per
                # skill; a failure degrades this rebuild's vectors only.
                batch_error: Optional[str] = None
                if to_embed and self._vec_available and self.is_semantic_available():
                    for start in range(0, len(to_embed), EMBED_BATCH_SIZE):
                        chunk = to_embed[start:start + EMBED_BATCH_SIZE]
                        vecs = self._client.embed_batch(
                            [text for _, text in chunk], input_type="document",
                            timeout=EMBED_BATCH_TIMEOUT_SECONDS,
                        )
                        if self._client.last_error:
                            batch_error = self._client.last_error
                        for (skill_key, _), vec in zip(chunk, vecs):
                            if vec is not None:
                                c.execute(
                                    "INSERT INTO skills_vec(skill_key, embedding) VALUES (?, ?)",
                                    (skill_key, self._pack(vec)),
                                )
                                embed_count += 1
                    if batch_error:
                        self._last_error = batch_error
                        logger.warning(
                            "skills semantic search degrading: %s", batch_error
                        )

                conn.commit()
                warning = None
                if not self.is_semantic_available():
                    warning = self._last_error
                elif batch_error:
                    warning = batch_error
                return {
                    "namespace": namespace,
                    "fts_indexed": fts_count,
                    "semantic_indexed": embed_count,
                    "semantic_available": self.is_semantic_available(),
                    "warning": warning,
                }
            finally:
                conn.close()

    def clear_namespace(self, namespace: str) -> None:
        with self._lock:
            conn = self._open_connection()
            try:
                c = conn.cursor()
                c.execute("DELETE FROM skills_meta WHERE namespace = ?", (namespace,))
                c.execute("DELETE FROM skills_fts WHERE namespace = ?", (namespace,))
                if self._vec_available:
                    c.execute("DELETE FROM skills_vec WHERE skill_key LIKE ?", (f"{namespace}::%",))
                conn.commit()
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def _lookup_meta(self, conn: sqlite3.Connection, namespace: str, skill_keys: List[str]) -> Dict[str, sqlite3.Row]:
        if not skill_keys:
            return {}
        placeholders = ",".join(["?"] * len(skill_keys))
        rows = conn.execute(
            f"""
            SELECT skill_key, name, description, extra
            FROM skills_meta
            WHERE namespace = ? AND skill_key IN ({placeholders})
            """,
            (namespace, *skill_keys),
        ).fetchall()
        return {r["skill_key"]: r for r in rows}

    def _vec_search(self, conn: sqlite3.Connection, q_vec: List[float], namespace: str, top_k: int) -> List[SearchHit]:
        rows = conn.execute(
            """
            SELECT skill_key, distance
            FROM skills_vec
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (self._pack(q_vec), max(top_k * 3, 16)),  # overshoot; filter by namespace after
        ).fetchall()
        ns_prefix = f"{namespace}::"
        rows = [r for r in rows if r["skill_key"].startswith(ns_prefix)][:top_k]
        meta = self._lookup_meta(conn, namespace, [r["skill_key"] for r in rows])
        out: List[SearchHit] = []
        for r in rows:
            m = meta.get(r["skill_key"])
            if not m:
                continue
            # sqlite-vec returns L2 distance for float vecs by default.
            # Convert to a monotonic "score" where higher = better, in [0, 1].
            score = 1.0 / (1.0 + float(r["distance"]))
            extra = json.loads(m["extra"] or "{}")
            out.append(SearchHit(name=m["name"], description=m["description"], score=score, extra=extra))
        return out

    def _fts_search(self, conn: sqlite3.Connection, query: str, namespace: str, top_k: int) -> List[SearchHit]:
        # FTS5 MATCH wants prefix-escaped terms. Accept free text, tokenize naively.
        tokens = [t for t in _safe_tokens(query)]
        if not tokens:
            return []
        match_expr = " OR ".join(f'"{t}"*' for t in tokens)
        try:
            rows = conn.execute(
                """
                SELECT skill_key, name, description, bm25(skills_fts) AS rank
                FROM skills_fts
                WHERE skills_fts MATCH ? AND namespace = ?
                ORDER BY rank LIMIT ?
                """,
                (match_expr, namespace, top_k),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        # Batch the metadata lookup with one IN(...) query, matching _vec_search,
        # instead of issuing one SELECT per result row.
        meta = self._lookup_meta(conn, namespace, [r["skill_key"] for r in rows])
        hits: List[SearchHit] = []
        for r in rows:
            m = meta.get(r["skill_key"])
            extra = json.loads(m["extra"] if m and m["extra"] else "{}")
            # bm25() returns negative numbers; lower = better. Flip sign + normalize.
            score = max(0.0, -float(r["rank"]))
            hits.append(SearchHit(name=r["name"], description=r["description"], score=score, extra=extra))
        return hits

    def _substring_search(self, conn: sqlite3.Connection, query: str, namespace: str, top_k: int) -> List[SearchHit]:
        q = query.strip().lower()
        rows = conn.execute(
            """
            SELECT name, description, extra
            FROM skills_meta
            WHERE namespace = ?
            """,
            (namespace,),
        ).fetchall()
        hits: List[SearchHit] = []
        for r in rows:
            name = r["name"]
            description = r["description"] or ""
            if not q or q in name.lower() or q in description.lower():
                extra = json.loads(r["extra"] or "{}")
                hits.append(SearchHit(name=name, description=description, score=1.0, extra=extra))
        return hits[:top_k]

    def search(self, query: str, namespace: str, top_k: int = 8) -> SearchResponse:
        """Search a namespace with graceful degradation.

        Tries semantic first, then FTS5/BM25, then substring. Returns all
        matches it found plus a ``mode`` string and (when degraded) a
        ``warning`` suitable for surfacing to the agent / user.

        Runs on the cached connection (``_get_connection``): this is the per-turn
        hot path, so it must not re-open the DB and re-load sqlite-vec each call.
        Read-only and serialized by ``self._lock``; the connection is left open
        for reuse.
        """
        with self._lock:
            conn = self._get_connection()
            warning: Optional[str] = None
            # Attempt semantic.
            query_embed_failed = False
            if self._vec_available and self.is_semantic_available():
                q_vec = self._embed(query, input_type="query")
                if q_vec is not None:
                    hits = self._vec_search(conn, q_vec, namespace, top_k)
                    if hits:
                        return SearchResponse(results=hits, mode="semantic")
                    # Empty semantic result — fall through to keyword to catch edge cases.
                elif query.strip():
                    # The embed itself failed (semantic stays available since
                    # the de-latch); surface the degradation instead of
                    # silently serving keyword results with no warning.
                    query_embed_failed = True
            if not self.is_semantic_available() or query_embed_failed:
                warning = (
                    f"semantic search unavailable ({self._last_error}); "
                    "falling back to keyword search. "
                    "Configure server embeddings (the EMBEDDING_* settings) "
                    "for better skill discovery."
                )

            # Attempt BM25 / FTS5.
            hits = self._fts_search(conn, query, namespace, top_k)
            if hits:
                return SearchResponse(results=hits, mode="bm25", warning=warning)

            # Final: substring match.
            hits = self._substring_search(conn, query, namespace, top_k)
            return SearchResponse(results=hits, mode="substring", warning=warning)


def _safe_tokens(query: str) -> List[str]:
    """Tokenize a free-text query into FTS5-safe terms."""
    out = []
    for raw in query.split():
        clean = "".join(c for c in raw if c.isalnum() or c in "-_")
        if clean:
            out.append(clean.lower())
    return out


__all__ = [
    "SkillEmbeddingIndex",
    "SearchHit",
    "SearchResponse",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL",
]
