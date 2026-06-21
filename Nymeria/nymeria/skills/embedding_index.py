"""Skill embedding index — semantic search over installed + marketplace skills.

Fallback chain (highest quality first):
    1. Configured OpenAI-compatible embeddings via sqlite-vec cosine similarity
    2. SQLite FTS5 / BM25 over name+description (keyword, no model)
    3. Substring match (final safety net)

The index is namespaced so the same store can hold `installed` plus one
namespace per marketplace source (e.g. `marketplace:anthropic`). The tool
layer is responsible for triggering rebuilds when the underlying data
changes — this class is a passive store.

Uses the same embedding configuration as Nymeria's memory index, so no new
model weight is shipped.
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

logger = logging.getLogger(__name__)

# Must match memory_index.py (same provider, same model).
EMBEDDING_DIMENSIONS = 1536
EMBEDDING_MODEL = "text-embedding-3-small"

# Each _embed() call sends a single short text, on both the search-query path
# (latency-sensitive: a hang stalls the agent turn) and the rebuild path. The
# OpenAI SDK otherwise defaults to a 600s timeout with 2 retries, so a hung
# socket can block for minutes; bound it so _embed() degrades to FTS5/keyword
# search instead. A rebuild that hits the timeout costs exactly one stall: the
# first failure latches _semantic_available off, so later items skip the embed.
EMBED_REQUEST_TIMEOUT_SECONDS = 10.0

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
        openai_api_key: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        embedding_model: str = EMBEDDING_MODEL,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._openai_key = openai_api_key
        self._openai_base_url = openai_base_url
        self._embedding_model = embedding_model
        self._openai_client = None
        self._semantic_available: Optional[bool] = None  # lazy-probed
        self._last_error: Optional[str] = None
        self._vec_available = False
        self._lock = threading.RLock()

        self._init_db()

    # ------------------------------------------------------------------
    # connection + schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
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

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
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

                # Vector table via sqlite-vec.
                if self._vec_available:
                    try:
                        c.execute(f"""
                            CREATE VIRTUAL TABLE IF NOT EXISTS skills_vec USING vec0(
                                skill_key TEXT PRIMARY KEY,
                                embedding FLOAT[{EMBEDDING_DIMENSIONS}]
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

    def _get_openai(self):
        if self._openai_client is None:
            if not self._openai_key:
                raise RuntimeError("EMBEDDING_API_KEY not configured")
            from openai import OpenAI
            kwargs = {
                "api_key": self._openai_key,
                "timeout": EMBED_REQUEST_TIMEOUT_SECONDS,
                "max_retries": 0,
            }
            if self._openai_base_url:
                kwargs["base_url"] = self._openai_base_url
            self._openai_client = OpenAI(**kwargs)
        return self._openai_client

    def is_semantic_available(self) -> bool:
        """True when OpenAI embeddings AND sqlite-vec are both usable."""
        if self._semantic_available is not None:
            return self._semantic_available
        if not self._vec_available:
            self._semantic_available = False
            self._last_error = "sqlite-vec extension not loaded"
            return False
        if not self._openai_key:
            self._semantic_available = False
            self._last_error = "EMBEDDING_API_KEY not set; semantic search disabled"
            return False
        if self._openai_key.startswith("cpx-"):
            self._semantic_available = False
            self._last_error = (
                "EMBEDDING_API_KEY looks like a CLIProxy gatekeeper key; "
                "set a real embeddings key or base URL"
            )
            return False
        # Lazy-probe on first real use via embed_text() instead of upfront.
        self._semantic_available = True
        return True

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def _embed(self, text: str) -> Optional[List[float]]:
        if not text.strip():
            return None
        if not self.is_semantic_available():
            return None
        try:
            resp = self._get_openai().embeddings.create(
                model=self._embedding_model, input=text[:8000],
            )
            embedding = resp.data[0].embedding
            if len(embedding) != EMBEDDING_DIMENSIONS:
                raise ValueError(
                    f"embedding dimension mismatch: expected {EMBEDDING_DIMENSIONS}, got {len(embedding)}"
                )
            return embedding
        except Exception as e:
            # Permanently degrade for this process so we don't retry on every search.
            self._semantic_available = False
            self._last_error = f"embedding call failed: {type(e).__name__}: {e}"
            logger.warning("skills semantic search degrading: %s", self._last_error)
            return None

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
            conn = self._connect()
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

                    if self._vec_available and self.is_semantic_available():
                        text = self._indexable_text(name, description, extra)
                        vec = self._embed(text)
                        if vec is not None:
                            c.execute(
                                "INSERT INTO skills_vec(skill_key, embedding) VALUES (?, ?)",
                                (skill_key, self._pack(vec)),
                            )
                            embed_count += 1

                conn.commit()
                return {
                    "namespace": namespace,
                    "fts_indexed": fts_count,
                    "semantic_indexed": embed_count,
                    "semantic_available": self.is_semantic_available(),
                    "warning": self._last_error if not self.is_semantic_available() else None,
                }
            finally:
                conn.close()

    def clear_namespace(self, namespace: str) -> None:
        with self._lock:
            conn = self._connect()
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
        """
        with self._lock:
            conn = self._connect()
            try:
                warning: Optional[str] = None
                # Attempt semantic.
                if self._vec_available and self.is_semantic_available():
                    q_vec = self._embed(query)
                    if q_vec is not None:
                        hits = self._vec_search(conn, q_vec, namespace, top_k)
                        if hits:
                            return SearchResponse(results=hits, mode="semantic")
                        # Empty semantic result — fall through to keyword to catch edge cases.
                if not self.is_semantic_available():
                    warning = (
                        f"semantic search unavailable ({self._last_error}); "
                        "falling back to keyword search. "
                        "Set EMBEDDING_API_KEY on the server for better skill discovery."
                    )

                # Attempt BM25 / FTS5.
                hits = self._fts_search(conn, query, namespace, top_k)
                if hits:
                    return SearchResponse(results=hits, mode="bm25", warning=warning)

                # Final: substring match.
                hits = self._substring_search(conn, query, namespace, top_k)
                return SearchResponse(results=hits, mode="substring", warning=warning)
            finally:
                conn.close()


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
