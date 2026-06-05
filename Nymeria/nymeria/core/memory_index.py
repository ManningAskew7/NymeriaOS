"""Vector store for semantic memory retrieval using sqlite-vec.

Implements RAG (Retrieval Augmented Generation) for Nymeria by:
- Embedding and storing conversation turns, memories, and TODO outcomes
- Hybrid search combining vector similarity and BM25 full-text search
- Sentence-aware chunking for optimal retrieval
"""

import hashlib
import json
import logging
import re
import sqlite3
import struct
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

# Chunking configuration (same as Clawdbot)
DEFAULT_CHUNK_SIZE = 400  # tokens (~1600 chars)
DEFAULT_CHUNK_OVERLAP = 80  # tokens (~320 chars)
CHARS_PER_TOKEN = 4  # Approximate

# OpenAI embedding dimensions
EMBEDDING_DIMENSIONS = 1536  # text-embedding-3-small
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"

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


class MemoryIndex:
    """Vector store for semantic memory retrieval using sqlite-vec."""

    def __init__(
        self,
        db_path: Path,
        embedding_provider: str = "openai",
        embedding_api_key: Optional[str] = None,
        embedding_base_url: Optional[str] = None,
        embedding_model: Optional[str] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        embedding_dimensions: Optional[int] = None,
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
        self.embedding_provider = embedding_provider
        if embedding_api_key is None or embedding_base_url is None or embedding_model is None:
            try:
                from ..config import get_settings
                settings = get_settings()
                if embedding_api_key is None:
                    embedding_api_key = settings.embedding_api_key
                if embedding_base_url is None:
                    embedding_base_url = settings.embedding_base_url
                if embedding_model is None:
                    embedding_model = settings.embedding_model
            except Exception:
                logger.warning("Failed to load settings for embedding config", exc_info=True)
        self.embedding_api_key = embedding_api_key
        self.embedding_base_url = embedding_base_url
        self.embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
        # Vector width. Defaults to the module constant so production is
        # unchanged; the eval harness passes a model's native width per run.
        self.embedding_dimensions = embedding_dimensions or EMBEDDING_DIMENSIONS
        self._dimensions_explicit = embedding_dimensions is not None
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._lock = threading.RLock()
        self._openai_client = None

        # Ensure directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with sqlite-vec loaded."""
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

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._lock:
            conn = self._get_connection()
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

                conn.commit()
                logger.info(f"Memory index initialized at {self.db_path}")

            finally:
                conn.close()

    def _get_openai_client(self):
        """Lazily initialize OpenAI client."""
        if self._openai_client is None:
            if not self.embedding_api_key:
                raise RuntimeError("EMBEDDING_API_KEY not configured")
            if self.embedding_api_key.startswith("cpx-"):
                raise RuntimeError("EMBEDDING_API_KEY looks like a CLIProxy gatekeeper key")
            try:
                from openai import OpenAI
                kwargs = {"api_key": self.embedding_api_key}
                if self.embedding_base_url:
                    kwargs["base_url"] = self.embedding_base_url
                self._openai_client = OpenAI(**kwargs)
            except ImportError:
                raise ImportError("openai package required for embeddings. Install with: pip install openai")
        return self._openai_client

    def _embed_kwargs(self) -> Dict[str, Any]:
        """Model-side kwargs for an OpenAI ``embeddings.create`` call.

        When an explicit ``embedding_dimensions`` was requested and the model is
        a text-embedding-3-* model (which supports Matryoshka truncation), pass
        ``dimensions`` so the model emits exactly that width. Production builds
        the index without ``embedding_dimensions``, so nothing extra is sent and
        the live embedding path is unchanged.
        """
        kwargs: Dict[str, Any] = {"model": self.embedding_model}
        if self._dimensions_explicit and "text-embedding-3" in (self.embedding_model or ""):
            kwargs["dimensions"] = self.embedding_dimensions
        return kwargs

    def embed_text(self, text: str) -> Optional[List[float]]:
        """
        Get embedding vector for text.

        Args:
            text: Text to embed

        Returns:
            List of floats representing the embedding, or None if embedding fails
        """
        if not text.strip():
            return None

        if self.embedding_provider == "openai":
            try:
                client = self._get_openai_client()
                response = client.embeddings.create(
                    input=text[:8000],  # Truncate to model's limit
                    **self._embed_kwargs(),
                )
                embedding = response.data[0].embedding
                if len(embedding) != self.embedding_dimensions:
                    logger.error(
                        "Embedding dimension mismatch: expected %s, got %s",
                        self.embedding_dimensions,
                        len(embedding),
                    )
                    return None
                return embedding
            except Exception as e:
                logger.error(f"Failed to get OpenAI embedding: {e}")
                return None
        else:
            logger.warning(f"Unknown embedding provider: {self.embedding_provider}")
            return None

    def _embed_texts(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Embed a batch of texts in one API call.

        Returns a list aligned to ``texts`` (None for any item that fails or has
        the wrong dimension). Used by ``backfill_embeddings`` to re-embed a whole
        corpus efficiently instead of one request per chunk.
        """
        if self.embedding_provider != "openai":
            logger.warning(f"Unknown embedding provider: {self.embedding_provider}")
            return [None] * len(texts)
        if not texts:
            return []
        # OpenAI rejects empty strings in a batch; substitute a single space.
        inputs = [(t[:8000] if t and t.strip() else " ") for t in texts]
        out: List[Optional[List[float]]] = [None] * len(texts)
        try:
            client = self._get_openai_client()
            response = client.embeddings.create(input=inputs, **self._embed_kwargs())
            for item in response.data:
                emb = item.embedding
                if len(emb) == self.embedding_dimensions:
                    out[item.index] = emb
                else:
                    logger.error(
                        "Embedding dimension mismatch: expected %s, got %s",
                        self.embedding_dimensions, len(emb),
                    )
        except Exception as e:
            logger.error(f"Batch embedding failed: {e}")
        return out

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
            conn = self._get_connection()
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

    def add_chunk(
        self,
        content: str,
        metadata: Dict[str, Any],
        chunk_type: str,
        user_id: str,
        thread_id: Optional[str] = None,
        event_time: Optional[datetime] = None,
        context: Optional[str] = None,
    ) -> List[str]:
        """
        Embed and store a chunk (or multiple chunks if content is long).

        Args:
            content: Text content to index
            metadata: Additional metadata (key, todo_id, etc.)
            chunk_type: Type of chunk ('conversation', 'memory', 'todo')
            user_id: User ID for isolation
            thread_id: Optional thread ID for conversation chunks
            event_time: Real-world time of the event. Defaults to ingest time.
                Pass an earlier time when back-dating (e.g. re-indexing old turns).
            context: Optional contextual-retrieval blurb. When provided it is
                prepended to the text that gets embedded (not shown to the user),
                situating the chunk for better recall.

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

                    # Get embedding and store in vector table. When a contextual
                    # blurb is present, embed context + content so the vector
                    # captures the situating context (Anthropic contextual
                    # retrieval); the stored ``content`` stays clean for display.
                    embed_input = f"{context}\n\n{chunk_content}" if context else chunk_content
                    embedding = self.embed_text(embed_input)
                    if embedding:
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
            finally:
                conn.close()

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
        return " OR ".join(f'"{t}"' for t in terms)

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

    def search(
        self,
        query: str,
        user_id: str,
        limit: int = 5,
        chunk_types: Optional[List[str]] = None,
        thread_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        fusion: str = "rrf",
        apply_recency: bool = True,
        recency_half_lives: Optional[Dict[str, float]] = None,
        now: Optional[datetime] = None,
        rrf_k: int = RRF_K,
        vec_weight: float = 1.0,
        bm25_weight: float = 1.0,
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

        Args:
            query: Search query text
            user_id: User ID to search within
            limit: Maximum results to return
            chunk_types: Optional filter for chunk types
            thread_id: Optional filter to a single source thread
            since: Optional lower bound on event_time (inclusive)
            until: Optional upper bound on event_time (inclusive)
            fusion: "rrf" (default) or "weighted" (legacy-style rank blend)
            apply_recency: Apply the recency soft-multiplier after fusion
            recency_half_lives: Per-chunk_type half-life (days) override
            now: Reference time for recency (defaults to current UTC time)
            rrf_k: RRF constant
            vec_weight: RRF weight on the vector branch (default 1.0)
            bm25_weight: RRF weight on the BM25 branch (default 1.0); raise it
                above vec_weight to bias fusion toward lexical retrieval
            apply_prose_priority: Demote tool-text-heavy chunks below prose
            prose_priority_weight: Strength of the prose-priority demotion
            dedup: Suppress near-duplicate results so twins do not consume
                several of the top-k slots (greedy, highest-scored kept)
            dedup_threshold: Token-set Jaccard at/above which two results are
                treated as near-duplicates

        Returns:
            List of ChunkResult objects sorted by relevance
        """
        if not query or not query.strip():
            return []

        now = ensure_aware_utc(now) if now else utc_now()
        half_lives = recency_half_lives or DEFAULT_RECENCY_HALF_LIVES
        candidate_pool = max(limit * 5, 30)
        # The vector branch cannot filter in SQL (vec0 has no metadata columns),
        # so on a selective thread/time scope most of its nearest-N fall out of
        # scope at the fetch step. Lift the vector LIMIT to a floor in that case
        # so enough in-scope vector candidates survive. chunk_types is excluded
        # deliberately: the tool sets it on nearly every call, so it is not
        # "selective" in the sense that would starve the vector branch.
        selective = bool(thread_id or since is not None or until is not None)
        vector_limit = (
            max(candidate_pool, VECTOR_FILTER_POOL) if selective else candidate_pool
        )

        # Shared filter fragment (user + optional type/thread/time) applied
        # wherever we read chunks, so both branches enforce the same scope.
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
            if since is not None:
                clauses.append(f"COALESCE({alias}.event_time, {alias}.created_at) >= ?")
                params.append(ensure_aware_utc(since).isoformat())
            if until is not None:
                clauses.append(f"COALESCE({alias}.event_time, {alias}.created_at) <= ?")
                params.append(ensure_aware_utc(until).isoformat())
            return " AND ".join(clauses), params

        results: List[ChunkResult] = []

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()

                # Vector branch: candidate ids ranked by ascending distance.
                # Filters are enforced at the fetch step below, so unfiltered
                # vector candidates that fall outside scope drop out there.
                vector_ids: List[str] = []
                query_embedding = self.embed_text(query)
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

                # BM25 branch: candidate ids ranked by ascending bm25 score,
                # with filters applied in SQL. The query is sanitized into a
                # safe FTS5 MATCH expression first; a query with no usable terms
                # skips this branch entirely (vector search still runs).
                bm25_ids: List[str] = []
                fts_query = self._fts_match_query(query)
                if fts_query:
                    where_sql, where_params = _filters("c")
                    try:
                        cursor.execute(f"""
                            SELECT c.id
                            FROM chunks_fts fts
                            JOIN chunks c ON c.rowid = fts.rowid
                            WHERE chunks_fts MATCH ?
                            AND {where_sql}
                            ORDER BY bm25(chunks_fts)
                            LIMIT ?
                        """, (fts_query, *where_params, candidate_pool))
                        bm25_ids = [row['id'] for row in cursor.fetchall()]
                    except Exception as e:
                        logger.warning(f"BM25 search failed: {e}")

                candidate_ids = list(dict.fromkeys([*vector_ids, *bm25_ids]))
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

                # Rank maps restricted to surviving (filtered) candidates.
                vec_rank = {cid: i for i, cid in enumerate(
                    [c for c in vector_ids if c in rows_by_id])}
                bm_rank = {cid: i for i, cid in enumerate(
                    [c for c in bm25_ids if c in rows_by_id])}

                final_scores: Dict[str, float] = {}
                for cid, row in rows_by_id.items():
                    if fusion == "weighted":
                        base = 0.0
                        if cid in vec_rank:
                            base += 0.7 / (1 + vec_rank[cid])
                        if cid in bm_rank:
                            base += 0.3 / (1 + bm_rank[cid])
                    else:  # rrf
                        base = 0.0
                        if cid in vec_rank:
                            base += vec_weight / (rrf_k + vec_rank[cid] + 1)
                        if cid in bm_rank:
                            base += bm25_weight / (rrf_k + bm_rank[cid] + 1)

                    if apply_recency:
                        ev = (self._parse_ts(row.get('event_time'))
                              or self._parse_ts(row.get('created_at')) or now)
                        age_days = max(0.0, (now - ev).total_seconds() / 86400.0)
                        hl = half_lives.get(row['chunk_type'], DEFAULT_RECENCY_HALF_LIFE)
                        base *= 0.5 ** (age_days / hl) if hl > 0 else 1.0

                    if apply_prose_priority and prose_priority_weight:
                        frac = self._tool_text_fraction(row.get('content'))
                        base *= 1.0 - prose_priority_weight * frac

                    final_scores[cid] = base

                # Greedy near-duplicate-aware top-k. Walk the fully-scored
                # candidates best-first and keep a result only if it is not a
                # near-duplicate of one already kept, so overlapping-window
                # chunks and live-vs-flush twins do not occupy several slots.
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

            finally:
                conn.close()

        return results

    def delete_chunks(self, chunk_ids: List[str]) -> int:
        """
        Remove chunks from index.

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

                for chunk_id in chunk_ids:
                    # Delete from chunks table (triggers handle FTS deletion)
                    cursor.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))
                    deleted += cursor.rowcount

                    # Delete from vector table
                    try:
                        cursor.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,))
                    except sqlite3.OperationalError:
                        pass  # Vector table may not exist

                conn.commit()
                logger.debug(f"Deleted {deleted} chunk(s)")

            except Exception as e:
                conn.rollback()
                logger.error(f"Failed to delete chunks: {e}")
                raise
            finally:
                conn.close()

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
            conn = self._get_connection()
            try:
                cursor = conn.cursor()

                # Get chunk IDs first
                cursor.execute("""
                    SELECT id FROM chunks
                    WHERE user_id = ? AND thread_id = ?
                """, (user_id, thread_id))

                chunk_ids = [row['id'] for row in cursor.fetchall()]

            finally:
                conn.close()

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
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id FROM chunks
                    WHERE user_id = ? AND chunk_type = ?
                """, (user_id, chunk_type))
                chunk_ids = [row['id'] for row in cursor.fetchall()]
            finally:
                conn.close()

        return self.delete_chunks(chunk_ids)

    def delete_memory_key(self, user_id: str, key: str) -> int:
        """
        Delete memory chunks for a specific profile memory key.

        Args:
            user_id: User ID
            key: Profile memory key

        Returns:
            Number of chunks deleted
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id, metadata FROM chunks
                    WHERE user_id = ? AND chunk_type = 'memory'
                """, (user_id,))

                chunk_ids = []
                for row in cursor.fetchall():
                    try:
                        metadata = json.loads(row['metadata'] or "{}")
                    except json.JSONDecodeError:
                        metadata = {}
                    if metadata.get("key") == key:
                        chunk_ids.append(row['id'])
            finally:
                conn.close()

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
            conn = self._get_connection()
            try:
                cursor = conn.cursor()

                # Get chunk IDs first
                cursor.execute("SELECT id FROM chunks WHERE user_id = ?", (user_id,))
                chunk_ids = [row['id'] for row in cursor.fetchall()]

            finally:
                conn.close()

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
            conn = self._get_connection()
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
