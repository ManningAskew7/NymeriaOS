"""Vector store for semantic memory retrieval using sqlite-vec.

Implements RAG (Retrieval Augmented Generation) for Nymeria by:
- Embedding and storing conversation turns, memories, and TODO outcomes
- Hybrid search combining vector similarity and BM25 full-text search
- Sentence-aware chunking for optimal retrieval
"""

import hashlib
import json
import logging
import math
import re
import sqlite3
import struct
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .embedding_client import EmbeddingClient
from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

# Chunking configuration (same as Clawdbot)
DEFAULT_CHUNK_SIZE = 400  # tokens (~1600 chars)
DEFAULT_CHUNK_OVERLAP = 80  # tokens (~320 chars)
CHARS_PER_TOKEN = 4  # Approximate

# OpenAI embedding dimensions
EMBEDDING_DIMENSIONS = 1536  # text-embedding-3-small
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

# Bound the OpenAI-compatible embedding client so a slow/unreachable endpoint
# cannot block on the SDK default (~600s x 2 retries). The client is shared by
# the latency-sensitive search query path and batch indexing, so the base client
# gets a generous ceiling (batches of up to 128 short chunks still finish well
# inside it) and the query path overrides to a tighter ceiling so a stalled
# endpoint degrades rag_search to BM25 in seconds instead of hanging the turn.
# The native cohere/gemini providers bound themselves inside EmbeddingClient.
EMBED_CLIENT_TIMEOUT_SECONDS = 60.0
EMBED_QUERY_TIMEOUT_SECONDS = 12.0


def _canonical_json(text: str) -> Optional[str]:
    """Canonical (sorted-key, no-whitespace) form of a JSON document, or None if
    ``text`` is not JSON. Lets two tool-result payloads that differ only in key
    order or whitespace collapse to the same ingest dedup hash."""
    s = text.strip()
    if not s or s[0] not in "{[":
        return None
    try:
        return json.dumps(json.loads(s), sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError):
        return None


# Reciprocal Rank Fusion constant. Higher values flatten the contribution of
# rank position; 60 is the value from the original RRF paper and the common
# default. RRF is rank-based, so it needs no score normalization and avoids the
# unbounded-BM25 failure mode of a weighted sum.
RRF_K = 60

# Per-chunk_type half-life in days for the recency soft-multiplier applied after
# fusion: score *= 0.5 ** (age_days / half_life). Older chunks are gently
# down-ranked, never filtered. These are deliberately LONG (years) so recency is
# only a faint tiebreaker, never enough to demote a clearly-better older match.
# Measured on a real corpus: at 14-45 day half-lives recency cost ~19% MRR by
# aging curated facts below recent chatter; at these values MRR recovers to ~1.0
# while a 6-week chunk still decays only ~4% (see tools/rag_eval.py --compare).
# Memories are long-lived facts; conversation/TODO next; tool activity shortest.
DEFAULT_RECENCY_HALF_LIVES = {
    "memory": 1825.0,
    "conversation": 730.0,
    "todo": 730.0,
    "tool": 365.0,
}
DEFAULT_RECENCY_HALF_LIFE = 730.0

# Prose-priority soft-multiplier. Conversation chunks embed the user/assistant
# prose followed by a templated "Tools used:" section (see agent_prompt). Tool
# output is useful but noisier and should not outrank the human/model prose, so
# after fusion a chunk's score is multiplied by (1 - weight * tool_fraction),
# where tool_fraction is the share of the chunk that is tool-result text. A
# prose-only chunk is unaffected; a tool-dump-heavy chunk is gently demoted.
TOOL_ACTIVITY_MARKER = "\n\nTools used:"
PROSE_PRIORITY_WEIGHT = 0.4

# Cap on distinct OR-terms in a single FTS5 MATCH expression. An unbounded union
# (e.g. a multi-thousand-token code blob or a whole user turn) makes FTS5 scan
# postings for every term across the entire table, costing seconds per query.
# Normal queries fall well under this, so the cap is a no-op for them; it only
# bounds pathological long queries. See _fts_match_query.
FTS_MAX_TERMS = 60

# Result-dedup threshold. Two results whose prose cores overlap by at least this
# token-set Jaccard fraction are treated as near-duplicates, so the second is
# skipped when selecting the final top-k. High (0.9) so only genuine twins
# collapse, never merely topical neighbours.
DEDUP_THRESHOLD = 0.9

# Vector candidate floor for selective (thread/time-scoped) searches. The vec0
# table carries no metadata columns, so the vector branch cannot filter in SQL;
# it fetches the nearest-N by distance and the scope filter is applied at the
# fetch step. On a narrow scope most of the nearest-N fall out of scope, so we
# lift the vector LIMIT to this floor to keep enough in-scope vector candidates.
VECTOR_FILTER_POOL = 200

# Date/time anchor biasing. When a search supplies an anchor (a date/time the
# agent inferred from "last April", "that report last week", etc.) retrieval is
# softly biased toward it WITHOUT excluding strong matches from other times. Two
# mechanisms: (1) a recall branch that fetches the chunks nearest in time to the
# anchor and fuses them as a third RRF signal beside vector and BM25, so a
# content-weak on-date chunk still enters the candidate pool; (2) a post-fusion
# multiplier shaped as a flat plateau across the anchor interval with a Gaussian
# falloff outside it, never dropping below ANCHOR_FLOOR so a strongly-relevant
# far-off chunk keeps most of its score. The whole mechanism reads only
# event_time, never embeddings, so it is unaffected by embedding-model swaps.
DEFAULT_ANCHOR_WEIGHT = 0.5  # RRF weight of the recall branch (vs 1.0 for vec/bm25)
ANCHOR_FLOOR = 0.4  # minimum kernel multiplier; "guide, not filter"
ANCHOR_FETCH_N = 100  # recall-branch rows fetched per side of the interval

# Softness (sigma, in days) of the Gaussian falloff OUTSIDE the anchor interval,
# keyed by the precision of the supplied date. Coarser precision = looser bias.
# The interval itself is a flat plateau (full score); sigma only shapes the edges.
ANCHOR_EDGE_SIGMA_DAYS = {
    "year": 60.0,
    "month": 10.0,
    "day": 3.0,
    "hour": 0.5,
    "minute": 0.25,
    "second": 0.25,
}


@dataclass
class AnchorSpec:
    """A parsed date/time anchor: a granularity interval plus edge softness.

    ``start``/``end`` bound the unit the agent named (the whole year/month/day),
    forming a flat plateau where every chunk scores the same on time. Outside the
    interval the score falls off with a Gaussian of width ``edge_sigma_days``.
    """
    start: datetime
    end: datetime
    edge_sigma_days: float


# ISO 8601 at decreasing precision. Ordered most-specific first so the first
# match wins. Fractional seconds / explicit offsets are handled by fromisoformat.
_ISO_ANCHOR_RES = [
    ("second", re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})")),
    ("minute", re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})$")),
    ("hour", re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2})$")),
    ("day", re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")),
    ("month", re.compile(r"^(\d{4})-(\d{2})$")),
    ("year", re.compile(r"^(\d{4})$")),
]

# Day-first local fallbacks: DD/MM/YYYY [HH:MM[:SS]] and the bare MM/YYYY month.
_LOCAL_ANCHOR_RE = re.compile(
    r"^(\d{1,2})/(\d{1,2})/(\d{4})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)
_LOCAL_MONTH_RE = re.compile(r"^(\d{1,2})/(\d{4})$")


def _anchor_interval(
    precision: str, year: int, month: int, day: int,
    hour: int, minute: int, second: int,
) -> Optional[AnchorSpec]:
    """Build the [start, end] plateau for a precision and its components."""
    try:
        start = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None
    if precision == "year":
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(microseconds=1)
    elif precision == "month":
        nxt = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12
               else datetime(year, month + 1, 1, tzinfo=timezone.utc))
        end = nxt - timedelta(microseconds=1)
    elif precision == "day":
        end = start + timedelta(days=1) - timedelta(microseconds=1)
    elif precision == "hour":
        end = start + timedelta(hours=1) - timedelta(microseconds=1)
    elif precision == "minute":
        end = start + timedelta(minutes=1) - timedelta(microseconds=1)
    else:  # second
        end = start + timedelta(seconds=1) - timedelta(microseconds=1)
    return AnchorSpec(start, end, ANCHOR_EDGE_SIGMA_DAYS[precision])


def parse_anchor_string(value: Optional[str]) -> Optional[AnchorSpec]:
    """Parse a date/time anchor into an :class:`AnchorSpec`, or None if unusable.

    The precision of the input sets the spread: a bare year biases across the
    whole year, ``YYYY-MM`` across the month, a full timestamp tightly around the
    instant. Accepts ISO 8601 at any precision (the agent's primary contract) and
    common day-first local forms (``15/04/2026``, ``15/04/2026 14:30``,
    ``04/2026``). Returns None for empty or unparseable input.
    """
    if not value or not value.strip():
        return None
    text = value.strip()

    for precision, rx in _ISO_ANCHOR_RES:
        m = rx.match(text)
        if not m:
            continue
        if precision in ("second", "minute", "hour"):
            # Let fromisoformat absorb fractional seconds / offsets, then re-key
            # to UTC; the interval is still anchored at the matched precision.
            try:
                dt = ensure_aware_utc(datetime.fromisoformat(text))
            except ValueError:
                g = [int(x) for x in m.groups()]
                while len(g) < 6:
                    g.append(0)
                return _anchor_interval(precision, *g[:6])
            return _anchor_interval(
                precision, dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second
            )
        g = [int(x) for x in m.groups()]
        while len(g) < 6:
            g.append(1 if len(g) < 3 else 0)  # missing month/day -> 1, time -> 0
        return _anchor_interval(precision, *g[:6])

    m = _LOCAL_ANCHOR_RE.match(text)
    if m:
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour = int(m.group(4)) if m.group(4) else 0
        minute = int(m.group(5)) if m.group(5) else 0
        second = int(m.group(6)) if m.group(6) else 0
        precision = ("second" if m.group(6) else "minute" if m.group(4) else "day")
        return _anchor_interval(precision, year, month, day, hour, minute, second)

    m = _LOCAL_MONTH_RE.match(text)
    if m:
        return _anchor_interval("month", int(m.group(2)), int(m.group(1)), 1, 0, 0, 0)

    return None


@dataclass
class ChunkResult:
    """Result from a search query."""
    id: str
    content: str
    chunk_type: str
    thread_id: Optional[str]
    created_at: datetime  # ingest time (when this chunk was written to the index)
    metadata: Dict[str, Any]
    score: float
    # Real-world time of the event the chunk records. Equals created_at for
    # live-indexed turns; differs for back-dated re-indexing or consolidation.
    event_time: Optional[datetime] = None
    # Contextual-retrieval blurb prepended before embedding (see add_chunk).
    context: Optional[str] = None


@dataclass(frozen=True)
class _ScoringConfig:
    """Post-fusion scoring inputs for `MemoryIndex._fuse_scores` (internal).

    A read-only bundle of the `search()` scoring params so the fusion helper
    takes one argument instead of fourteen. Not part of any public surface.
    """
    fusion: str
    vec_weight: float
    bm25_weight: float
    anchor_weight: float
    rrf_k: int
    anchor_lo: Optional[datetime]
    anchor_hi: Optional[datetime]
    anchor_sigma: float
    anchor_floor: float
    apply_recency: bool
    half_lives: Dict[str, float]
    now: datetime
    apply_prose_priority: bool
    prose_priority_weight: float


class MemoryIndex:
    """Vector store for semantic memory retrieval using sqlite-vec."""

    def __init__(
        self,
        db_path: Path,
        embedding_provider: Optional[str] = None,
        embedding_api_key: Optional[str] = None,
        embedding_base_url: Optional[str] = None,
        embedding_model: Optional[str] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        embedding_dimensions: Optional[int] = None,
        embedding_input_type: Optional[str] = None,
    ):
        """
        Initialize the memory index.

        Args:
            db_path: Path to SQLite database (e.g., data/users/{user_id}/memory.db)
            embedding_provider: Embedding provider ('openai' for now)
            embedding_api_key: Dedicated API key for embeddings
            embedding_base_url: Optional OpenAI-compatible embeddings base URL
            embedding_model: Embedding model name
            chunk_size: Maximum tokens per chunk
            chunk_overlap: Token overlap between chunks
            embedding_dimensions: Width of the vec0 vector column and the
                accepted embedding length. Defaults to EMBEDDING_DIMENSIONS
                (1536). Production omits it, so the live index keeps the 1536
                slot and the embedding path is unchanged. The eval harness sets
                it to a model's native width (each model in its own throwaway DB)
                so models are benchmarked at their optimal configuration. When
                explicit and the model is a text-embedding-3-* model, the OpenAI
                ``dimensions`` param is sent so the model emits exactly that width.
        """
        self.db_path = Path(db_path)
        # Track whether the dimension was explicitly requested (constructor arg or
        # a non-None setting) before settings fill in the rest, so the OpenAI
        # ``dimensions`` param is only sent when intended.
        dims_explicit = embedding_dimensions is not None
        needs_settings = (
            embedding_api_key is None or embedding_base_url is None
            or embedding_model is None or embedding_provider is None
            or embedding_dimensions is None or embedding_input_type is None
        )
        if needs_settings:
            try:
                from ..config import get_settings
                settings = get_settings()
                if embedding_api_key is None:
                    embedding_api_key = settings.embedding_api_key
                if embedding_base_url is None:
                    embedding_base_url = settings.embedding_base_url
                if embedding_model is None:
                    embedding_model = settings.embedding_model
                if embedding_provider is None:
                    embedding_provider = settings.embedding_provider
                if embedding_dimensions is None and settings.embedding_dimensions is not None:
                    embedding_dimensions = settings.embedding_dimensions
                    dims_explicit = True
                if embedding_input_type is None:
                    embedding_input_type = settings.embedding_input_type
            except Exception:
                logger.warning("Failed to load settings for embedding config", exc_info=True)
        self.embedding_provider = embedding_provider or "openai"
        self.embedding_api_key = embedding_api_key
        self.embedding_base_url = embedding_base_url
        self.embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
        self.embedding_input_type = embedding_input_type
        # Vector width. Defaults to the module constant so production is unchanged;
        # an explicit setting (or the eval harness) selects a model's native width.
        self.embedding_dimensions = embedding_dimensions or EMBEDDING_DIMENSIONS
        self._dimensions_explicit = dims_explicit
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._lock = threading.RLock()
        # Shared provider-dispatching embedding client (openai/cohere/gemini/
        # local); the same implementation serves the skills and tool-search
        # indexes, so provider behavior cannot drift between the three. Built
        # lazily per config (see _get_embedding_client): callers like the RAG
        # eval harness mutate the embedding_* instance attrs post-construction
        # (e.g. embedding_provider="none" to defer vectors during a bulk load),
        # and those mutations must keep taking effect exactly as they did when
        # the provider dispatch read the attrs per call.
        self._embedding_client: Optional[EmbeddingClient] = None
        self._embedding_client_cfg: Optional[tuple] = None
        self._dim_mismatch = False
        # Cached SQLite connection (lazily opened on first use, sqlite-vec loaded
        # once). The instance is held per user by the agent and the memory tools,
        # so reusing one connection avoids re-opening the DB and re-loading the
        # sqlite-vec C extension on every search/ingest/delete. All DB access is
        # serialized by ``self._lock`` (an RLock), so a single shared connection
        # is safe. ``close()`` releases it; the next call transparently reopens.
        self._conn: Optional[sqlite3.Connection] = None
        # Diagnostics from the most recent search() (which branches actually ran,
        # candidate counts, embedder identity). rag_search surfaces it so the
        # configured embedder/reranker stack can be confirmed live in testing.
        self._last_search_diag: Optional[Dict[str, Any]] = None

        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()

    def _open_connection(self) -> sqlite3.Connection:
        """Open a NEW SQLite connection with sqlite-vec loaded.

        Used by one-shot / occasional methods that manage their own short-lived
        connection (``_init_db`` at construction, ``backfill_embeddings``,
        ``rebuild_vectors``, ``get_stats``). The repeated hot paths (ingest,
        search, deletes) reuse the cached ``_get_connection()`` instead.
        """
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # Try to load sqlite-vec extension
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except ImportError:
            logger.warning("sqlite-vec not installed. Vector search will be disabled.")
        except Exception as e:
            logger.warning(f"Failed to load sqlite-vec: {e}. Vector search will be disabled.")

        return conn

    def _get_connection(self) -> sqlite3.Connection:
        """Return the cached database connection with sqlite-vec loaded, opening
        it on first use.

        The connection is created once per instance and reused across the
        repeated operations (search, ingest, delete). Callers always hold
        ``self._lock`` (an RLock; see each method), so the single connection is
        never touched concurrently. This pays the sqlite-vec extension load once
        instead of on every call. ``close()`` clears the cache so the next call
        reopens transparently.
        """
        if self._conn is None:
            self._conn = self._open_connection()
        return self._conn

    def close(self) -> None:
        """Close the cached connection if open. Idempotent; the next DB call
        reopens transparently. Used for explicit teardown (e.g. tests) and to
        release the file handle when an index is no longer needed."""
        with self._lock:
            conn = self._conn
            self._conn = None
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.debug("Error closing memory-index connection", exc_info=True)

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._lock:
            conn = self._open_connection()
            try:
                cursor = conn.cursor()

                # Main chunks table. ``created_at`` is ingest time; ``event_time``
                # is the real-world time of the recorded event (bi-temporal-lite).
                # ``context`` holds the contextual-retrieval blurb embedded with
                # the chunk (nullable; populated only when contextual retrieval is
                # enabled).
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS chunks (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        chunk_type TEXT NOT NULL,
                        thread_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        event_time TIMESTAMP,
                        metadata TEXT DEFAULT '{}',
                        context TEXT,
                        content_hash TEXT
                    )
                """)

                # Lazy column migration for pre-existing per-user DBs created
                # before event_time/context existed. Add the columns if missing
                # and backfill event_time from created_at so recency ranking and
                # time filters work on historical chunks.
                existing_cols = {
                    row["name"]
                    for row in cursor.execute("PRAGMA table_info(chunks)").fetchall()
                }
                if "event_time" not in existing_cols:
                    cursor.execute("ALTER TABLE chunks ADD COLUMN event_time TIMESTAMP")
                    cursor.execute(
                        "UPDATE chunks SET event_time = created_at WHERE event_time IS NULL"
                    )
                if "context" not in existing_cols:
                    cursor.execute("ALTER TABLE chunks ADD COLUMN context TEXT")
                if "content_hash" not in existing_cols:
                    cursor.execute("ALTER TABLE chunks ADD COLUMN content_hash TEXT")
                    # Backfill the prose-core hash for historical rows so the
                    # ingest dedup guard and the content_hash index cover them
                    # too. The hash needs Python (sqlite has no equivalent), so
                    # read id+content and UPDATE in one batched transaction.
                    legacy = cursor.execute(
                        "SELECT id, content FROM chunks WHERE content_hash IS NULL"
                    ).fetchall()
                    if legacy:
                        cursor.executemany(
                            "UPDATE chunks SET content_hash = ? WHERE id = ?",
                            [(self._content_hash(r["content"]), r["id"]) for r in legacy],
                        )

                # Indexes for efficient querying
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chunks_user
                    ON chunks(user_id)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chunks_type
                    ON chunks(chunk_type)
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chunks_thread
                    ON chunks(thread_id)
                """)
                # Narrows the exact-duplicate guard in add_chunk to one user's
                # chunks of a given type before the content equality check.
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chunks_user_type
                    ON chunks(user_id, chunk_type)
                """)
                # Backs the ingest dedup guard in add_chunk: locate an existing
                # chunk with the same prose-core hash for this user/type fast.
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chunks_content_hash
                    ON chunks(user_id, chunk_type, content_hash)
                """)
                # Backs the date-anchor recall branch in search(): two index-only
                # range scans (event_time < / >= the anchor) find the chunks
                # nearest in time. event_time is non-NULL everywhere after the
                # lazy backfill below, so the branch reads it directly.
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_chunks_user_event_time
                    ON chunks(user_id, event_time)
                """)

                # FTS5 table for BM25 search
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                        content,
                        content='chunks',
                        content_rowid='rowid'
                    )
                """)

                # Triggers to keep FTS in sync
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                        INSERT INTO chunks_fts(rowid, content)
                        VALUES (new.rowid, new.content);
                    END
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                        INSERT INTO chunks_fts(chunks_fts, rowid, content)
                        VALUES('delete', old.rowid, old.content);
                    END
                """)
                cursor.execute("""
                    CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                        INSERT INTO chunks_fts(chunks_fts, rowid, content)
                        VALUES('delete', old.rowid, old.content);
                        INSERT INTO chunks_fts(rowid, content)
                        VALUES (new.rowid, new.content);
                    END
                """)

                # Vector table (sqlite-vec) - create only if extension is available
                try:
                    cursor.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                            chunk_id TEXT PRIMARY KEY,
                            embedding FLOAT[{self.embedding_dimensions}]
                        )
                    """)
                    logger.debug("Vector table created/verified")
                except sqlite3.OperationalError as e:
                    if "no such module: vec0" in str(e):
                        logger.warning("sqlite-vec module not available. Vector search disabled.")
                    else:
                        raise

                # Detect a dimension change on a pre-existing index: the vec0 width
                # is fixed at creation, so a now-mismatched configured width would
                # have new embeddings silently rejected. Surface it loudly instead.
                self._dim_mismatch = False
                stored_dim = self._stored_vec_dim(cursor)
                if stored_dim is not None and stored_dim != self.embedding_dimensions:
                    self._dim_mismatch = True
                    logger.error(
                        "Memory index %s holds %s-d vectors but is configured for "
                        "%s-d (provider=%s, model=%s). New embeddings will be "
                        "rejected; re-embed into a fresh index after a dimension "
                        "change (delete the DB to rebuild, or backfill a new one).",
                        self.db_path, stored_dim, self.embedding_dimensions,
                        self.embedding_provider, self.embedding_model,
                    )

                conn.commit()
                logger.info(f"Memory index initialized at {self.db_path}")

            finally:
                conn.close()

    def embed_text(self, text: str, input_type: str = "document") -> Optional[List[float]]:
        """Get an embedding vector for ``text``.

        ``input_type`` is ``"query"`` for a search query or ``"document"`` for an
        ingested chunk. It only matters for asymmetric providers (native cohere /
        gemini, or an OpenAI-compatible model with ``embedding_input_type`` set,
        e.g. Voyage); symmetric OpenAI models ignore it. Returns None on failure.
        """
        if not text.strip():
            return None
        out = self._embed_batch([text], input_type)
        return out[0] if out else None

    def _embed_texts(
        self, texts: List[str], input_type: str = "document"
    ) -> List[Optional[List[float]]]:
        """Embed a batch of texts. Returns a list aligned to ``texts`` (None for
        any item that fails or has the wrong dimension). Used by
        ``backfill_embeddings`` to re-embed a whole corpus efficiently.
        """
        if not texts:
            return []
        return self._embed_batch(texts, input_type)

    def _get_embedding_client(self) -> EmbeddingClient:
        """Return the shared ``EmbeddingClient`` for the CURRENT embedding
        config, rebuilding it when any ``embedding_*`` instance attr changed
        since the last call (the config used to be read per call, and eval
        tooling mutates these attrs on a live index)."""
        cfg = (
            self.embedding_provider,
            self.embedding_api_key,
            self.embedding_base_url,
            self.embedding_model,
            self.embedding_dimensions,
            self._dimensions_explicit,
            self.embedding_input_type,
        )
        if self._embedding_client is None or self._embedding_client_cfg != cfg:
            self._embedding_client = EmbeddingClient(
                provider=self.embedding_provider,
                api_key=self.embedding_api_key,
                base_url=self.embedding_base_url,
                model=self.embedding_model,
                dimensions=self.embedding_dimensions,
                dimensions_explicit=self._dimensions_explicit,
                input_type=self.embedding_input_type,
                timeout=EMBED_CLIENT_TIMEOUT_SECONDS,
            )
            self._embedding_client_cfg = cfg
        return self._embedding_client

    def _embed_batch(
        self, texts: List[str], input_type: str
    ) -> List[Optional[List[float]]]:
        """Embed a batch via the shared provider-dispatching ``EmbeddingClient``
        (``core/embedding_client.py``). ``input_type`` is ``"query"`` or
        ``"document"`` (honored only by asymmetric providers). The search query
        path fails fast (a stalled endpoint degrades to BM25 in seconds); batch
        indexing keeps the generous base-client ceiling."""
        timeout = EMBED_QUERY_TIMEOUT_SECONDS if input_type == "query" else None
        return self._get_embedding_client().embed_batch(
            texts, input_type=input_type, timeout=timeout
        )

    def _stored_vec_dim(self, cursor) -> Optional[int]:
        """Width of the vectors already stored in vec_chunks, or None if the table
        is empty or unavailable. Used to detect a dimension change on an existing
        index (the vec0 width is fixed at creation)."""
        try:
            row = cursor.execute(
                "SELECT embedding FROM vec_chunks LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if not row or row["embedding"] is None:
            return None
        blob = row["embedding"]
        if isinstance(blob, (bytes, bytearray)):
            return len(blob) // 4  # float32
        return None

    def backfill_embeddings(
        self,
        user_id: str,
        batch_size: int = 128,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, int]:
        """Embed and store vectors for this user's chunks that lack them.

        Use after enabling embeddings on a corpus indexed BM25-only, or when
        switching embedding models. Additive: it never modifies chunk content,
        only inserts into the vector table. Embeds ``context + content`` when a
        contextual blurb is present (matching ``add_chunk``). Returns counts.
        """
        embedded = failed = 0
        with self._lock:
            conn = self._open_connection()
            try:
                cursor = conn.cursor()
                try:
                    rows = cursor.execute(
                        "SELECT id, content, context FROM chunks "
                        "WHERE user_id = ? "
                        "AND id NOT IN (SELECT chunk_id FROM vec_chunks)",
                        (user_id,),
                    ).fetchall()
                except sqlite3.OperationalError as e:
                    if "no such table: vec_chunks" in str(e):
                        logger.warning("Vector table unavailable; cannot backfill embeddings")
                        return {"embedded": 0, "failed": 0, "total": 0}
                    raise

                total = len(rows)
                for start in range(0, total, batch_size):
                    batch = rows[start:start + batch_size]
                    texts = [
                        (f"{r['context']}\n\n{r['content']}" if r['context'] else r['content'])
                        for r in batch
                    ]
                    vectors = self._embed_texts(texts)
                    for row, vec in zip(batch, vectors):
                        if vec is None:
                            failed += 1
                            continue
                        try:
                            cursor.execute(
                                "INSERT INTO vec_chunks (chunk_id, embedding) VALUES (?, ?)",
                                (row['id'], self._serialize_embedding(vec)),
                            )
                            embedded += 1
                        except sqlite3.OperationalError as e:
                            logger.warning(f"Failed to store backfilled embedding: {e}")
                            failed += 1
                    conn.commit()
                    if progress:
                        progress(min(start + batch_size, total), total)
                return {"embedded": embedded, "failed": failed, "total": total}
            finally:
                conn.close()

    def rebuild_vectors(
        self,
        batch_size: int = 128,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, int]:
        """Drop the vector table, recreate it at the current embedding width, then
        re-embed every chunk in this DB (all users).

        Use after changing the embedding model, provider, or dimensions: the vec0
        width is fixed at table creation, so a dimension change needs a rebuild
        rather than a backfill. Chunk content rows and the FTS index are left
        untouched (BM25 search keeps working throughout). Returns summed counts.
        """
        with self._lock:
            conn = self._open_connection()
            try:
                cursor = conn.cursor()
                try:
                    cursor.execute("DROP TABLE IF EXISTS vec_chunks")
                    cursor.execute(f"""
                        CREATE VIRTUAL TABLE vec_chunks USING vec0(
                            chunk_id TEXT PRIMARY KEY,
                            embedding FLOAT[{self.embedding_dimensions}]
                        )
                    """)
                    conn.commit()
                except sqlite3.OperationalError as e:
                    if "no such module: vec0" in str(e):
                        logger.warning("sqlite-vec unavailable; cannot rebuild vectors")
                        return {"embedded": 0, "failed": 0, "total": 0}
                    raise
                user_ids = [
                    row["user_id"]
                    for row in cursor.execute(
                        "SELECT DISTINCT user_id FROM chunks"
                    ).fetchall()
                ]
            finally:
                conn.close()
        # The table now matches the configured width, so clear the guard so the
        # backfill inserts vectors instead of skipping them.
        self._dim_mismatch = False
        totals = {"embedded": 0, "failed": 0, "total": 0}
        for user_id in user_ids:
            result = self.backfill_embeddings(
                user_id, batch_size=batch_size, progress=progress
            )
            for key in totals:
                totals[key] += result.get(key, 0)
        return totals

    def _serialize_embedding(self, embedding: List[float]) -> bytes:
        """Serialize embedding to bytes for sqlite-vec."""
        return struct.pack(f'{len(embedding)}f', *embedding)

    def _chunk_text(self, text: str) -> List[str]:
        """
        Split text into overlapping chunks with sentence-aware boundaries.

        Args:
            text: Text to chunk

        Returns:
            List of text chunks
        """
        if not text:
            return []

        # Convert token limits to approximate character limits
        max_chars = self.chunk_size * CHARS_PER_TOKEN
        overlap_chars = self.chunk_overlap * CHARS_PER_TOKEN

        # If text fits in one chunk, return as-is
        if len(text) <= max_chars:
            return [text]

        # Split into sentences (simple heuristic)
        sentence_endings = re.compile(r'(?<=[.!?])\s+')
        sentences = sentence_endings.split(text)

        chunks = []
        current_chunk = []
        current_length = 0

        for sentence in sentences:
            sentence_len = len(sentence)

            # If single sentence is too long, force split it
            if sentence_len > max_chars:
                # Finish current chunk if we have content
                if current_chunk:
                    chunks.append(' '.join(current_chunk))
                    current_chunk = []
                    current_length = 0

                # Split long sentence by character limit with overlap
                for i in range(0, sentence_len, max_chars - overlap_chars):
                    chunk_end = min(i + max_chars, sentence_len)
                    chunks.append(sentence[i:chunk_end])
                continue

            # Check if adding this sentence would exceed limit
            if current_length + sentence_len + 1 > max_chars:
                # Save current chunk
                if current_chunk:
                    chunks.append(' '.join(current_chunk))

                # Start new chunk with overlap from previous sentences
                overlap_start = []
                overlap_length = 0
                for prev_sentence in reversed(current_chunk):
                    if overlap_length + len(prev_sentence) + 1 <= overlap_chars:
                        overlap_start.insert(0, prev_sentence)
                        overlap_length += len(prev_sentence) + 1
                    else:
                        break

                current_chunk = overlap_start + [sentence]
                current_length = sum(len(s) + 1 for s in current_chunk)
            else:
                current_chunk.append(sentence)
                current_length += sentence_len + 1

        # Don't forget the last chunk
        if current_chunk:
            chunks.append(' '.join(current_chunk))

        return chunks

    def _has_near_dup_chunk(self, cursor, user_id: str, chunk_type: str,
                            embedding: List[float], threshold: float) -> bool:
        """True when an existing same-(user, type) chunk is within `threshold`
        cosine of `embedding`.

        Embeddings are L2-normalized and vec0 ranks by L2 distance, so cosine T
        maps to a distance bound sqrt(2*(1-T)). vec0 MATCH cannot filter on joined
        columns, so scan the K nearest overall, then keep the closest one whose
        chunk matches this user + type.
        """
        dist_bound = (2.0 * (1.0 - threshold)) ** 0.5
        try:
            rows = cursor.execute(
                "SELECT chunk_id, distance FROM vec_chunks "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT 10",
                (self._serialize_embedding(embedding),),
            ).fetchall()
        except sqlite3.OperationalError:
            return False
        if not rows:
            return False
        dist_by_id = {r["chunk_id"]: r["distance"] for r in rows}
        placeholders = ",".join("?" * len(dist_by_id))
        same = cursor.execute(
            f"SELECT id FROM chunks WHERE id IN ({placeholders}) "
            "AND user_id = ? AND chunk_type = ?",
            (*dist_by_id.keys(), user_id, chunk_type),
        ).fetchall()
        return any(dist_by_id.get(r["id"], 9.9) <= dist_bound for r in same)

    def add_chunk(
        self,
        content: str,
        metadata: Dict[str, Any],
        chunk_type: str,
        user_id: str,
        thread_id: Optional[str] = None,
        event_time: Optional[datetime] = None,
        context: Optional[str] = None,
        dedup_near: bool = False,
        dedup_threshold: float = 0.97,
    ) -> List[str]:
        """
        Embed and store a chunk (or multiple chunks if content is long).

        Args:
            content: Text content to index
            metadata: Additional metadata (key, todo_id, etc.)
            chunk_type: Type of chunk ('conversation', 'memory', 'todo', 'tool')
            user_id: User ID for isolation
            thread_id: Optional thread ID for conversation chunks
            event_time: Real-world time of the event. Defaults to ingest time.
                Pass an earlier time when back-dating (e.g. re-indexing old turns).
            context: Optional contextual-retrieval blurb. When provided it is
                prepended to the text that gets embedded (not shown to the user),
                situating the chunk for better recall.
            dedup_near: When True, skip the whole add if the first chunk is a
                semantic near-duplicate (cosine >= dedup_threshold) of an
                existing same-(user, type) chunk. Generalizes the exact prose-core
                hash guard to paraphrased / re-embedded content. Callers opt in
                (e.g. the conversation indexer); memory upserts and todos do not.
            dedup_threshold: Cosine bound for dedup_near (default 0.97).

        Returns:
            List of chunk IDs created
        """
        if not content or not content.strip():
            return []

        event_iso = ensure_aware_utc(event_time or utc_now()).isoformat()

        # Split into chunks if necessary
        text_chunks = self._chunk_text(content)
        chunk_ids = []

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()

                # Duplicate guard keyed on the prose-core hash. The same turn is
                # indexed both live (at turn completion) and again when it is
                # later flushed before a context trim, and a window can be
                # re-flushed on successive trims, so duplicates pile up. The live
                # copy carries a templated "Tools used:" suffix the flushed copy
                # lacks, so a byte-equality check missed that pair; hashing the
                # prose core (text before the marker, whitespace-collapsed) makes
                # the twins collide while still catching exact duplicates. If the
                # first chunk's core hash already exists for this
                # user/type/thread, treat the whole add as a duplicate and skip.
                if text_chunks:
                    first_hash = self._content_hash(text_chunks[0])
                    if chunk_type == "tool":
                        # Tool outputs repeat across threads (same call, same
                        # payload), so dedup per-user cross-thread, not per-thread.
                        cursor.execute(
                            "SELECT 1 FROM chunks "
                            "WHERE user_id = ? AND chunk_type = ? "
                            "AND content_hash = ? LIMIT 1",
                            (user_id, chunk_type, first_hash),
                        )
                    else:
                        cursor.execute(
                            "SELECT 1 FROM chunks "
                            "WHERE user_id = ? AND chunk_type = ? "
                            "AND IFNULL(thread_id, '') = IFNULL(?, '') "
                            "AND content_hash = ? LIMIT 1",
                            (user_id, chunk_type, thread_id, first_hash),
                        )
                    if cursor.fetchone():
                        logger.debug(
                            "Skipping duplicate %s chunk for user %s", chunk_type, user_id
                        )
                        return []

                # Semantic near-duplicate guard (opt-in via dedup_near). The hash
                # guard above only catches verbatim cores; this skips the add when
                # the first chunk is near-identical (cosine >= dedup_threshold) to
                # an existing same-(user, type) chunk, so paraphrased or
                # re-embedded content (e.g. a rag_search result or memory text
                # restated in a turn) does not accumulate near-duplicate chunks
                # that crowd the first-stage pool. The embedding is reused for the
                # first chunk's insert below, so this costs one extra vec KNN, not
                # an extra embed.
                first_embedding = None
                if text_chunks and dedup_near:
                    embed0 = (f"{context}\n\n{text_chunks[0]}"
                              if context else text_chunks[0])
                    first_embedding = self.embed_text(embed0)
                    if first_embedding and self._has_near_dup_chunk(
                            cursor, user_id, chunk_type, first_embedding,
                            dedup_threshold):
                        logger.debug(
                            "Skipping near-duplicate %s chunk for user %s",
                            chunk_type, user_id)
                        return []

                # Pre-compute each chunk's embedding. Long content splits into N
                # chunks; embedding chunks 2..N one-by-one would issue N-1
                # sequential round-trips, so they go through a single _embed_texts
                # batch. Index 0 stays on the per-item path (reusing the dedup_near
                # embedding when present, else embed_text) so it remains the
                # single-chunk entry point. When a contextual blurb is present the
                # embedded text is context + content (the stored ``content`` stays
                # clean for display), mirroring the per-chunk behaviour replaced.
                embed_inputs = [
                    (f"{context}\n\n{c}" if context else c) for c in text_chunks
                ]
                embeddings: List[Optional[List[float]]] = [None] * len(text_chunks)
                if text_chunks:
                    embeddings[0] = (
                        first_embedding if first_embedding is not None
                        else self.embed_text(embed_inputs[0])
                    )
                    if len(embed_inputs) > 1:
                        for offset, vec in enumerate(
                            self._embed_texts(embed_inputs[1:]), start=1
                        ):
                            embeddings[offset] = vec

                for i, chunk_content in enumerate(text_chunks):
                    # Generate unique ID
                    chunk_id = str(uuid.uuid4())

                    # Store chunk metadata
                    chunk_metadata = {
                        **metadata,
                        "chunk_index": i,
                        "total_chunks": len(text_chunks),
                    }

                    # Insert into chunks table
                    cursor.execute("""
                        INSERT INTO chunks (id, user_id, content, chunk_type, thread_id, event_time, metadata, context, content_hash)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        chunk_id,
                        user_id,
                        chunk_content,
                        chunk_type,
                        thread_id,
                        event_iso,
                        json.dumps(chunk_metadata),
                        context,
                        self._content_hash(chunk_content),
                    ))

                    # Store the pre-batched embedding for this chunk (computed
                    # above) in the vector table.
                    embedding = embeddings[i]
                    # Skip the vector insert on a width mismatch: the vec would be
                    # the configured width, not the table's, so vec0 would reject it
                    # per chunk. _init_db already logged the mismatch once loudly.
                    if embedding and not self._dim_mismatch:
                        try:
                            cursor.execute("""
                                INSERT INTO vec_chunks (chunk_id, embedding)
                                VALUES (?, ?)
                            """, (chunk_id, self._serialize_embedding(embedding)))
                        except sqlite3.OperationalError as e:
                            if "no such table: vec_chunks" not in str(e):
                                logger.warning(f"Failed to store embedding: {e}")

                    chunk_ids.append(chunk_id)

                conn.commit()
                logger.debug(f"Added {len(chunk_ids)} chunk(s) for user {user_id}, type={chunk_type}")

            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to add chunk: {e}")
                raise

        return chunk_ids

    @staticmethod
    def _fts_match_query(query: str) -> Optional[str]:
        """Build a safe FTS5 MATCH expression from arbitrary user text.

        FTS5 treats characters like ``'``, ``"``, ``-``, ``*``, ``(`` ``)`` and
        the bare words AND/OR/NOT/NEAR as query syntax. Passing raw natural
        language (e.g. ``what is my pet's name``) raises ``fts5: syntax error``
        and the whole BM25 branch silently returns nothing. We extract word
        tokens, double-quote each as a literal term (so operator-words and
        punctuation cannot be interpreted as syntax), and OR them so any term
        may match; BM25 still ranks by term rarity and frequency. Returns None
        when no usable token remains, so the caller can skip the BM25 branch.
        """
        if not query:
            return None
        tokens = re.findall(r"\w+", query, flags=re.UNICODE)
        # Drop single-character noise (e.g. the "s" left from "pet's") but keep
        # lone digits, which can be meaningful (years, counts).
        terms = [t for t in tokens if len(t) >= 2 or t.isdigit()]
        if not terms:
            return None
        # Dedupe, preserving first-seen order: a repeated OR-term is redundant in
        # FTS5 (matching and bm25 scoring are unchanged by it) but still costs a
        # postings scan. seen[t] records first position for a deterministic cap.
        seen: Dict[str, int] = {}
        for t in terms:
            if t not in seen:
                seen[t] = len(seen)
        unique = list(seen)
        # Bound the union for pathological long queries. OR and bm25() are
        # order-insensitive, so we keep the longest terms (a crude IDF proxy:
        # long tokens are rarer and more discriminative; dropping common short
        # words barely moves ranking), ties broken by first occurrence.
        if len(unique) > FTS_MAX_TERMS:
            unique = sorted(unique, key=lambda t: (-len(t), seen[t]))[:FTS_MAX_TERMS]
        return " OR ".join(f'"{t}"' for t in unique)

    @staticmethod
    def _tool_text_fraction(content: Optional[str]) -> float:
        """Share of a chunk that is templated tool-result text (0.0 - 1.0).

        Keys on the ``Tools used:`` section appended by the turn indexer; a chunk
        with no tool activity returns 0.0 (no penalty).
        """
        if not content:
            return 0.0
        idx = content.find(TOOL_ACTIVITY_MARKER)
        if idx < 0:
            return 0.0
        return (len(content) - idx) / len(content)

    @staticmethod
    def _content_core(content: Optional[str]) -> str:
        """Normalized prose core of a chunk for duplicate detection.

        Takes the text before ``TOOL_ACTIVITY_MARKER`` (so a turn indexed live,
        with its templated ``Tools used:`` suffix, matches the same turn flushed
        later without it) and collapses all whitespace. Returns "" for empty
        input.
        """
        if not content:
            return ""
        idx = content.find(TOOL_ACTIVITY_MARKER)
        core = content[:idx] if idx >= 0 else content
        # Canonicalize a JSON payload (e.g. a tool result) so key-order and
        # whitespace variants of the same data dedup to one chunk; fall back to
        # whitespace-collapsed prose otherwise.
        canon = _canonical_json(core)
        if canon is not None:
            return canon
        return " ".join(core.split())

    @classmethod
    def _content_hash(cls, content: Optional[str]) -> str:
        """Stable hash of a chunk's normalized prose core (see _content_core).

        Used as the ingest-time duplicate key: byte-identical chunks and
        live-vs-flush twins (same prose core, differing tool suffix) collide.
        """
        return hashlib.sha1(cls._content_core(content).encode("utf-8")).hexdigest()

    @classmethod
    def _is_near_duplicate(
        cls, a: Optional[str], b: Optional[str], threshold: float
    ) -> bool:
        """True when two chunks' prose cores overlap by >= threshold (Jaccard).

        Token-set Jaccard over the normalized cores: ``|A & B| / |A | B|``. Pure
        and embedding-free; used to collapse near-duplicate search results. Two
        empty cores are treated as duplicates (both carry no prose signal).
        """
        ta = set(cls._content_core(a).split())
        tb = set(cls._content_core(b).split())
        if not ta and not tb:
            return True
        union = ta | tb
        if not union:
            return True
        return len(ta & tb) / len(union) >= threshold

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        """Parse a stored timestamp (ISO string or datetime) to aware UTC."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return ensure_aware_utc(value)
        if isinstance(value, str):
            try:
                return ensure_aware_utc(datetime.fromisoformat(value))
            except ValueError:
                return None
        return None

    def _vector_candidates(
        self,
        cursor: sqlite3.Cursor,
        query_embedding: Optional[List[float]],
        vector_limit: int,
    ) -> List[str]:
        """Vector branch: candidate ids ranked by ascending distance.

        Filters are enforced at the fetch step in ``search``, so unfiltered
        vector candidates that fall outside scope drop out there. A falsy
        embedding (None, or the empty list an embedder may return) skips the
        SELECT and yields no ids; ``search`` still reports ``vector_used`` from
        the embedding's ``is not None``, which deliberately differs from this
        truthy guard for the empty-list case.
        """
        vector_ids: List[str] = []
        if query_embedding:
            try:
                cursor.execute("""
                    SELECT chunk_id
                    FROM vec_chunks
                    WHERE embedding MATCH ?
                    ORDER BY distance
                    LIMIT ?
                """, (self._serialize_embedding(query_embedding), vector_limit))
                vector_ids = [row['chunk_id'] for row in cursor.fetchall()]
            except sqlite3.OperationalError as e:
                if "no such table: vec_chunks" not in str(e):
                    logger.warning(f"Vector search failed: {e}")
        return vector_ids

    def _bm25_candidates(
        self,
        cursor: sqlite3.Cursor,
        fts_query: Optional[str],
        filters: Callable[[str], tuple[str, List[Any]]],
        pool: int,
    ) -> List[str]:
        """BM25 branch: candidate ids ranked by ascending bm25 score, with
        filters applied in SQL. A falsy ``fts_query`` (vector-only mode, or a
        query with no usable terms) skips this branch entirely; vector search
        still runs.
        """
        bm25_ids: List[str] = []
        if fts_query:
            where_sql, where_params = filters("c")
            try:
                cursor.execute(f"""
                    SELECT c.id
                    FROM chunks_fts fts
                    JOIN chunks c ON c.rowid = fts.rowid
                    WHERE chunks_fts MATCH ?
                    AND {where_sql}
                    ORDER BY bm25(chunks_fts)
                    LIMIT ?
                """, (fts_query, *where_params, pool))
                bm25_ids = [row['id'] for row in cursor.fetchall()]
            except Exception as e:
                logger.warning(f"BM25 search failed: {e}")
        return bm25_ids

    def _anchor_candidates(
        self,
        cursor: sqlite3.Cursor,
        filters: Callable[[str], tuple[str, List[Any]]],
        anchor_lo: Optional[datetime],
        anchor_hi: Optional[datetime],
    ) -> List[str]:
        """Anchor recall branch: the chunks nearest in time to the anchor
        interval, fused as a third signal so a content-weak on-date chunk still
        enters the pool. Two index-backed scans (before / at-or-after the
        interval start) reuse the scope filter but apply NO time WHERE bound; the
        split is a ranking device, not a filter. event_time is non-NULL
        everywhere (lazy backfill), so we read it directly to use
        idx_chunks_user_event_time. Returns [] when no anchor interval is set.
        """
        anchor_ids: List[str] = []
        if anchor_lo is not None and anchor_hi is not None:
            lo, hi = anchor_lo, anchor_hi
            where_sql, where_params = filters("c")
            start_iso = lo.isoformat()
            rows: List[Dict[str, Any]] = []
            try:
                cursor.execute(f"""
                    SELECT c.id, c.event_time AS et
                    FROM chunks c
                    WHERE {where_sql} AND c.event_time < ?
                    ORDER BY c.event_time DESC
                    LIMIT ?
                """, (*where_params, start_iso, ANCHOR_FETCH_N))
                rows.extend(dict(r) for r in cursor.fetchall())
                cursor.execute(f"""
                    SELECT c.id, c.event_time AS et
                    FROM chunks c
                    WHERE {where_sql} AND c.event_time >= ?
                    ORDER BY c.event_time ASC
                    LIMIT ?
                """, (*where_params, start_iso, ANCHOR_FETCH_N))
                rows.extend(dict(r) for r in cursor.fetchall())
            except Exception as e:
                logger.warning(f"Anchor search failed: {e}")
            # Order by distance to the interval (0 inside the plateau).
            def _interval_distance(et_raw) -> float:
                ev = self._parse_ts(et_raw)
                if ev is None:
                    return float("inf")
                if ev < lo:
                    return (lo - ev).total_seconds()
                if ev > hi:
                    return (ev - hi).total_seconds()
                return 0.0
            rows.sort(key=lambda r: _interval_distance(r.get('et')))
            anchor_ids = list(dict.fromkeys(r['id'] for r in rows))
        return anchor_ids

    def _fuse_scores(
        self,
        rows_by_id: Dict[str, Dict[str, Any]],
        vector_ids: List[str],
        bm25_ids: List[str],
        anchor_ids: List[str],
        scoring: _ScoringConfig,
    ) -> Dict[str, float]:
        """Fuse the three branch rankings into a per-candidate score.

        Rank maps are restricted to surviving (filtered) candidates, in each
        branch's original order. RRF (or the legacy weighted blend) gives the
        base; then either the anchor plateau/Gaussian multiplier (when an anchor
        interval is set) or the per-chunk_type recency multiplier refines it, and
        a final prose-priority multiplier gently demotes tool-text-heavy chunks.
        """
        vec_rank = {cid: i for i, cid in enumerate(
            [c for c in vector_ids if c in rows_by_id])}
        bm_rank = {cid: i for i, cid in enumerate(
            [c for c in bm25_ids if c in rows_by_id])}
        anchor_rank = {cid: i for i, cid in enumerate(
            [c for c in anchor_ids if c in rows_by_id])}

        final_scores: Dict[str, float] = {}
        for cid, row in rows_by_id.items():
            if scoring.fusion == "weighted":
                base = 0.0
                if cid in vec_rank:
                    base += 0.7 / (1 + vec_rank[cid])
                if cid in bm_rank:
                    base += 0.3 / (1 + bm_rank[cid])
            else:  # rrf
                base = 0.0
                if cid in vec_rank:
                    base += scoring.vec_weight / (scoring.rrf_k + vec_rank[cid] + 1)
                if cid in bm_rank:
                    base += scoring.bm25_weight / (scoring.rrf_k + bm_rank[cid] + 1)
            # Anchor recall branch contributes a third RRF term, so an
            # on-date chunk that vector and BM25 both miss gets a positive
            # floor instead of base 0 (which no multiplier could rescue).
            if cid in anchor_rank:
                base += scoring.anchor_weight / (scoring.rrf_k + anchor_rank[cid] + 1)

            ev = (self._parse_ts(row.get('event_time'))
                  or self._parse_ts(row.get('created_at')) or scoring.now)
            if scoring.anchor_lo is not None and scoring.anchor_hi is not None:
                # Flat plateau across the interval (every in-window chunk
                # scores the same on time), Gaussian falloff outside,
                # floored so a strong far match is never excluded.
                if scoring.anchor_lo <= ev <= scoring.anchor_hi:
                    d_days = 0.0
                elif ev < scoring.anchor_lo:
                    d_days = (scoring.anchor_lo - ev).total_seconds() / 86400.0
                else:
                    d_days = (ev - scoring.anchor_hi).total_seconds() / 86400.0
                gauss = (math.exp(-0.5 * (d_days / scoring.anchor_sigma) ** 2)
                         if scoring.anchor_sigma > 0 else (1.0 if d_days == 0 else 0.0))
                base *= scoring.anchor_floor + (1.0 - scoring.anchor_floor) * gauss
            elif scoring.apply_recency:
                age_days = max(0.0, (scoring.now - ev).total_seconds() / 86400.0)
                hl = scoring.half_lives.get(row['chunk_type'], DEFAULT_RECENCY_HALF_LIFE)
                base *= 0.5 ** (age_days / hl) if hl > 0 else 1.0

            if scoring.apply_prose_priority and scoring.prose_priority_weight:
                frac = self._tool_text_fraction(row.get('content'))
                base *= 1.0 - scoring.prose_priority_weight * frac

            final_scores[cid] = base
        return final_scores

    def _select_ranked_ids(
        self,
        final_scores: Dict[str, float],
        rows_by_id: Dict[str, Dict[str, Any]],
        limit: int,
        dedup: bool,
        dedup_threshold: float,
    ) -> List[str]:
        """Greedy near-duplicate-aware top-k. Walk the fully-scored candidates
        best-first and keep a result only if it is not a near-duplicate of one
        already kept, so overlapping-window chunks and live-vs-flush twins do not
        occupy several slots.
        """
        ordered = sorted(
            final_scores, key=lambda c: final_scores[c], reverse=True
        )
        if dedup:
            ranked: List[str] = []
            for cid in ordered:
                content = rows_by_id[cid].get('content')
                if any(
                    self._is_near_duplicate(
                        content, rows_by_id[kept].get('content'), dedup_threshold
                    )
                    for kept in ranked
                ):
                    continue
                ranked.append(cid)
                if len(ranked) >= limit:
                    break
        else:
            ranked = ordered[:limit]
        return ranked

    def search(
        self,
        query: str,
        user_id: str,
        limit: int = 5,
        chunk_types: Optional[List[str]] = None,
        thread_id: Optional[str] = None,
        anchor_start: Optional[datetime] = None,
        anchor_end: Optional[datetime] = None,
        anchor_edge_sigma_days: Optional[float] = None,
        anchor_weight: float = DEFAULT_ANCHOR_WEIGHT,
        anchor_floor: float = ANCHOR_FLOOR,
        fusion: str = "rrf",
        apply_recency: bool = True,
        recency_half_lives: Optional[Dict[str, float]] = None,
        now: Optional[datetime] = None,
        rrf_k: int = RRF_K,
        vec_weight: float = 1.0,
        bm25_weight: float = 1.0,
        retrieval_mode: str = "hybrid",
        apply_prose_priority: bool = True,
        prose_priority_weight: float = PROSE_PRIORITY_WEIGHT,
        dedup: bool = True,
        dedup_threshold: float = DEDUP_THRESHOLD,
    ) -> List[ChunkResult]:
        """
        Hybrid search combining vector similarity and BM25 full-text search.

        Fusion is Reciprocal Rank Fusion by default (rank-based, no tuning, no
        score normalization). After fusion two soft-multipliers refine the
        order without filtering: a per-chunk_type recency factor (gently
        down-ranks older chunks) and a prose-priority factor (gently demotes
        chunks dominated by tool-result text so human/model prose ranks first).

        When an anchor interval is supplied (``anchor_start``/``anchor_end``,
        typically from :func:`parse_anchor_string`), retrieval is softly biased
        toward that date: a third recall branch fuses the chunks nearest in time
        to the interval (so a content-weak on-date chunk still surfaces), and the
        recency factor is replaced by a flat plateau across the interval with a
        Gaussian falloff outside it, floored at ``anchor_floor`` so strong matches
        from other times are never excluded. Anchor biasing reads only event_time
        and is independent of the embedding model.

        Args:
            query: Search query text
            user_id: User ID to search within
            limit: Maximum results to return
            chunk_types: Optional filter for chunk types
            thread_id: Optional filter to a single source thread
            anchor_start: Inclusive start of the anchor plateau (None disables
                anchor biasing and falls back to recency)
            anchor_end: Inclusive end of the anchor plateau
            anchor_edge_sigma_days: Gaussian sigma (days) of the falloff outside
                the plateau; from the anchor's precision
            anchor_weight: RRF weight of the recall branch (default 0.5)
            anchor_floor: Lower bound of the anchor multiplier (default 0.4); a
                far-off chunk keeps at least this fraction of its fused score
            fusion: "rrf" (default) or "weighted" (legacy-style rank blend)
            apply_recency: Apply the recency soft-multiplier after fusion (only
                when no anchor is supplied)
            recency_half_lives: Per-chunk_type half-life (days) override
            now: Reference time for recency (defaults to current UTC time)
            rrf_k: RRF constant
            vec_weight: RRF weight on the vector branch (default 1.0)
            bm25_weight: RRF weight on the BM25 branch (default 1.0); raise it
                above vec_weight to bias fusion toward lexical retrieval
            retrieval_mode: "hybrid" (default, BM25 + vector) or "vector"
                (vector-only; the BM25 branch is skipped). Hybrid keeps lexical
                retrieval as a failsafe when embeddings underperform or fail.
            apply_prose_priority: Demote tool-text-heavy chunks below prose
            prose_priority_weight: Strength of the prose-priority demotion
            dedup: Suppress near-duplicate results so twins do not consume
                several of the top-k slots (greedy, highest-scored kept)
            dedup_threshold: Token-set Jaccard at/above which two results are
                treated as near-duplicates

        Returns:
            List of ChunkResult objects sorted by relevance
        """
        self._last_search_diag = None
        if not query or not query.strip():
            return []

        now = ensure_aware_utc(now) if now else utc_now()
        half_lives = recency_half_lives or DEFAULT_RECENCY_HALF_LIVES
        # Narrowed, aware-UTC anchor bounds (None unless both were supplied), so
        # every downstream use is unambiguously a datetime.
        anchor_lo: Optional[datetime] = None
        anchor_hi: Optional[datetime] = None
        if anchor_start is not None and anchor_end is not None:
            anchor_lo = ensure_aware_utc(anchor_start)
            anchor_hi = ensure_aware_utc(anchor_end)
        anchor_sigma = anchor_edge_sigma_days or 1.0
        candidate_pool = max(limit * 5, 30)
        # The vector branch cannot filter in SQL (vec0 has no metadata columns),
        # so on a selective thread scope most of its nearest-N fall out of scope
        # at the fetch step. Lift the vector LIMIT to a floor in that case so
        # enough in-scope vector candidates survive. chunk_types is excluded
        # deliberately: the tool sets it on nearly every call, so it is not
        # "selective" in the sense that would starve the vector branch. An anchor
        # alone does not trigger the floor: its own recall branch already injects
        # in-window candidates, which is what the floor would otherwise supply.
        selective = bool(thread_id)
        vector_limit = (
            max(candidate_pool, VECTOR_FILTER_POOL) if selective else candidate_pool
        )

        # Shared filter fragment (user + optional type/thread) applied wherever we
        # read chunks, so every branch enforces the same scope. Time is no longer
        # a hard filter; the anchor biases softly instead.
        def _filters(alias):
            clauses = [f"{alias}.user_id = ?"]
            params: List[Any] = [user_id]
            if chunk_types:
                ph = ", ".join("?" for _ in chunk_types)
                clauses.append(f"{alias}.chunk_type IN ({ph})")
                params.extend(chunk_types)
            if thread_id:
                clauses.append(f"{alias}.thread_id = ?")
                params.append(thread_id)
            return " AND ".join(clauses), params

        results: List[ChunkResult] = []

        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()

            # Three recall branches over the shared cursor. The vector branch
            # cannot filter in SQL (filters are enforced at the fetch step
            # below); BM25 and the anchor recall branch apply the scope filter
            # themselves. `query_embedding`/`fts_query` are computed here so the
            # diagnostics below can report which branches actually ran (the
            # branch helpers each no-op on a falsy input).
            query_embedding = self.embed_text(query, input_type="query")
            vector_ids = self._vector_candidates(cursor, query_embedding, vector_limit)

            # Vector-only mode skips the BM25 branch entirely; hybrid (default)
            # keeps it as a lexical signal and a failsafe when embeddings fail.
            fts_query = (
                self._fts_match_query(query)
                if retrieval_mode != "vector" else None
            )
            bm25_ids = self._bm25_candidates(
                cursor, fts_query, _filters, candidate_pool
            )

            anchor_ids = self._anchor_candidates(
                cursor, _filters, anchor_lo, anchor_hi
            )

            # Record which retrieval branches actually ran this search so
            # rag_search can confirm the live embedder/retrieval stack.
            self._last_search_diag = {
                "retrieval_mode": retrieval_mode,
                "embedding_provider": self.embedding_provider,
                "embedding_model": self.embedding_model,
                "embedding_dimensions": self.embedding_dimensions,
                "vector_used": query_embedding is not None,
                "vector_candidates": len(vector_ids),
                "bm25_used": bool(fts_query),
                "bm25_candidates": len(bm25_ids),
                "anchor_candidates": len(anchor_ids),
                "candidate_pool": candidate_pool,
                "vector_limit": vector_limit,
            }

            candidate_ids = list(dict.fromkeys([*vector_ids, *bm25_ids, *anchor_ids]))
            if not candidate_ids:
                return []

            # Fetch full rows for all candidates, enforcing filters uniformly.
            placeholders = ", ".join("?" for _ in candidate_ids)
            fetch_where, fetch_params = _filters("chunks")
            cursor.execute(f"""
                SELECT id, content, chunk_type, thread_id, created_at,
                       event_time, metadata, context
                FROM chunks
                WHERE id IN ({placeholders})
                AND {fetch_where}
            """, (*candidate_ids, *fetch_params))
            rows_by_id = {row['id']: dict(row) for row in cursor.fetchall()}
            if not rows_by_id:
                return []

            # Fuse the three branch rankings, then select the near-duplicate-
            # aware top-k. Both helpers are pure (no DB), operating on the
            # already-fetched rows under the same lock acquisition.
            scoring = _ScoringConfig(
                fusion=fusion,
                vec_weight=vec_weight,
                bm25_weight=bm25_weight,
                anchor_weight=anchor_weight,
                rrf_k=rrf_k,
                anchor_lo=anchor_lo,
                anchor_hi=anchor_hi,
                anchor_sigma=anchor_sigma,
                anchor_floor=anchor_floor,
                apply_recency=apply_recency,
                half_lives=half_lives,
                now=now,
                apply_prose_priority=apply_prose_priority,
                prose_priority_weight=prose_priority_weight,
            )
            final_scores = self._fuse_scores(
                rows_by_id, vector_ids, bm25_ids, anchor_ids, scoring
            )
            ranked = self._select_ranked_ids(
                final_scores, rows_by_id, limit, dedup, dedup_threshold
            )

            for cid in ranked:
                row = rows_by_id[cid]
                try:
                    metadata = json.loads(row['metadata']) if row['metadata'] else {}
                except json.JSONDecodeError:
                    metadata = {}
                created_at = self._parse_ts(row.get('created_at')) or now
                event_time = self._parse_ts(row.get('event_time')) or created_at
                results.append(ChunkResult(
                    id=row['id'],
                    content=row['content'],
                    chunk_type=row['chunk_type'],
                    thread_id=row['thread_id'],
                    created_at=created_at,
                    metadata=metadata,
                    score=final_scores[cid],
                    event_time=event_time,
                    context=row.get('context'),
                ))

        return results

    # Max bound parameters per DELETE statement. SQLite's compiled
    # SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds; 500 stays well under it.
    _DELETE_BATCH = 500

    def delete_chunks(self, chunk_ids: List[str]) -> int:
        """
        Remove chunks (and their vectors) from the index.

        Args:
            chunk_ids: List of chunk IDs to delete

        Returns:
            Number of chunks deleted
        """
        if not chunk_ids:
            return 0

        deleted = 0

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()

                # Delete in batched IN(...) statements rather than one DELETE per
                # id. vec_chunks has no FTS-style trigger (the chunks_ad trigger
                # only syncs the FTS index), so its matching rows are deleted
                # explicitly to avoid orphaned vectors.
                for start in range(0, len(chunk_ids), self._DELETE_BATCH):
                    batch = chunk_ids[start:start + self._DELETE_BATCH]
                    placeholders = ",".join("?" for _ in batch)
                    cursor.execute(
                        f"DELETE FROM chunks WHERE id IN ({placeholders})", batch
                    )
                    deleted += cursor.rowcount
                    try:
                        cursor.execute(
                            f"DELETE FROM vec_chunks WHERE chunk_id IN ({placeholders})",
                            batch,
                        )
                    except sqlite3.OperationalError:
                        pass  # Vector table may not exist

                conn.commit()
                logger.debug(f"Deleted {deleted} chunk(s)")

            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to delete chunks: {e}")
                raise

        return deleted

    def delete_by_thread(self, user_id: str, thread_id: str) -> int:
        """
        Delete all chunks for a specific thread.

        Args:
            user_id: User ID
            thread_id: Thread ID

        Returns:
            Number of chunks deleted
        """
        with self._lock:
            cursor = self._get_connection().cursor()
            cursor.execute(
                "SELECT id FROM chunks WHERE user_id = ? AND thread_id = ?",
                (user_id, thread_id),
            )
            chunk_ids = [row['id'] for row in cursor.fetchall()]

        return self.delete_chunks(chunk_ids)

    def delete_by_type(self, user_id: str, chunk_type: str) -> int:
        """
        Delete all chunks of a specific type for a user.

        Args:
            user_id: User ID
            chunk_type: Chunk type to remove ('conversation', 'memory', 'todo')

        Returns:
            Number of chunks deleted
        """
        with self._lock:
            cursor = self._get_connection().cursor()
            cursor.execute(
                "SELECT id FROM chunks WHERE user_id = ? AND chunk_type = ?",
                (user_id, chunk_type),
            )
            chunk_ids = [row['id'] for row in cursor.fetchall()]

        return self.delete_chunks(chunk_ids)

    def delete_memory_key(
        self, user_id: str, key: str, chunk_type: str = "memory"
    ) -> int:
        """
        Delete memory chunks for a specific memory key.

        Args:
            user_id: User ID
            key: The chunk metadata key ("memory" chunks use the profile
                memory key; "team_memory" chunks use the "<team_id>:<key>"
                composite so keys stay unique per team)
            chunk_type: Keyed memory chunk type ('memory' or 'team_memory')

        Returns:
            Number of chunks deleted
        """
        with self._lock:
            cursor = self._get_connection().cursor()
            # Filter by the JSON memory key in SQL (json_extract) instead of
            # loading every memory row and parsing metadata in Python. The
            # json_valid guard short-circuits before json_extract, so a row with
            # malformed metadata is skipped rather than aborting the whole query
            # (preserving the old per-row try/except json.loads behaviour);
            # metadata is normally always json.dumps-written. A row whose metadata
            # has no "key" yields NULL and is excluded.
            cursor.execute(
                "SELECT id FROM chunks "
                "WHERE user_id = ? AND chunk_type = ? "
                "AND json_valid(metadata) "
                "AND json_extract(metadata, '$.key') = ?",
                (user_id, chunk_type, key),
            )
            chunk_ids = [row['id'] for row in cursor.fetchall()]

        return self.delete_chunks(chunk_ids)

    def delete_team_memory_chunks(self, user_id: str, team_id: str) -> int:
        """
        Delete every team_memory chunk belonging to one team (team deletion).

        Args:
            user_id: Owner user ID (teams are per-user entities)
            team_id: The callable team id stamped in chunk metadata

        Returns:
            Number of chunks deleted
        """
        with self._lock:
            cursor = self._get_connection().cursor()
            cursor.execute(
                "SELECT id FROM chunks "
                "WHERE user_id = ? AND chunk_type = 'team_memory' "
                "AND json_valid(metadata) "
                "AND json_extract(metadata, '$.team_id') = ?",
                (user_id, team_id),
            )
            chunk_ids = [row['id'] for row in cursor.fetchall()]

        return self.delete_chunks(chunk_ids)

    def clear_index(self, user_id: str) -> int:
        """
        Clear all chunks for a user.

        Args:
            user_id: User ID

        Returns:
            Number of chunks deleted
        """
        with self._lock:
            cursor = self._get_connection().cursor()
            cursor.execute("SELECT id FROM chunks WHERE user_id = ?", (user_id,))
            chunk_ids = [row['id'] for row in cursor.fetchall()]

        return self.delete_chunks(chunk_ids)

    def get_stats(self, user_id: str) -> Dict[str, Any]:
        """
        Get indexing statistics for a user.

        Args:
            user_id: User ID

        Returns:
            Dict with stats: chunk_count, by_type, last_indexed, etc.
        """
        with self._lock:
            conn = self._open_connection()
            try:
                cursor = conn.cursor()

                # Total count
                cursor.execute("""
                    SELECT COUNT(*) as total FROM chunks WHERE user_id = ?
                """, (user_id,))
                total = cursor.fetchone()['total']

                # Count by type
                cursor.execute("""
                    SELECT chunk_type, COUNT(*) as count
                    FROM chunks
                    WHERE user_id = ?
                    GROUP BY chunk_type
                """, (user_id,))
                by_type = {row['chunk_type']: row['count'] for row in cursor.fetchall()}

                # Last indexed
                cursor.execute("""
                    SELECT MAX(created_at) as last_indexed
                    FROM chunks
                    WHERE user_id = ?
                """, (user_id,))
                last_row = cursor.fetchone()
                last_indexed = last_row['last_indexed'] if last_row else None

                # Vector count
                vector_count = 0
                try:
                    cursor.execute("""
                        SELECT COUNT(*) as count FROM vec_chunks vc
                        JOIN chunks c ON c.id = vc.chunk_id
                        WHERE c.user_id = ?
                    """, (user_id,))
                    vector_count = cursor.fetchone()['count']
                except sqlite3.OperationalError:
                    pass  # sqlite-vec table may not exist yet

                return {
                    "total_chunks": total,
                    "by_type": by_type,
                    "last_indexed": last_indexed,
                    "vector_count": vector_count,
                    "embedding_provider": self.embedding_provider,
                }

            finally:
                conn.close()
