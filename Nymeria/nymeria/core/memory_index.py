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
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Chunking configuration (same as Clawdbot)
DEFAULT_CHUNK_SIZE = 400  # tokens (~1600 chars)
DEFAULT_CHUNK_OVERLAP = 80  # tokens (~320 chars)
CHARS_PER_TOKEN = 4  # Approximate

# OpenAI embedding dimensions
EMBEDDING_DIMENSIONS = 1536  # text-embedding-3-small
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


@dataclass
class ChunkResult:
    """Result from a search query."""
    id: str
    content: str
    chunk_type: str
    thread_id: Optional[str]
    created_at: datetime
    metadata: Dict[str, Any]
    score: float


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
                pass
        self.embedding_api_key = embedding_api_key
        self.embedding_base_url = embedding_base_url
        self.embedding_model = embedding_model or DEFAULT_EMBEDDING_MODEL
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

                # Main chunks table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS chunks (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        content TEXT NOT NULL,
                        chunk_type TEXT NOT NULL,
                        thread_id TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        metadata TEXT DEFAULT '{}'
                    )
                """)

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
                            embedding FLOAT[{EMBEDDING_DIMENSIONS}]
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
                    model=self.embedding_model,
                    input=text[:8000],  # Truncate to model's limit
                )
                embedding = response.data[0].embedding
                if len(embedding) != EMBEDDING_DIMENSIONS:
                    logger.error(
                        "Embedding dimension mismatch: expected %s, got %s",
                        EMBEDDING_DIMENSIONS,
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
    ) -> List[str]:
        """
        Embed and store a chunk (or multiple chunks if content is long).

        Args:
            content: Text content to index
            metadata: Additional metadata (key, todo_id, etc.)
            chunk_type: Type of chunk ('conversation', 'memory', 'todo')
            user_id: User ID for isolation
            thread_id: Optional thread ID for conversation chunks

        Returns:
            List of chunk IDs created
        """
        if not content or not content.strip():
            return []

        # Split into chunks if necessary
        text_chunks = self._chunk_text(content)
        chunk_ids = []

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()

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
                        INSERT INTO chunks (id, user_id, content, chunk_type, thread_id, metadata)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        chunk_id,
                        user_id,
                        chunk_content,
                        chunk_type,
                        thread_id,
                        json.dumps(chunk_metadata),
                    ))

                    # Get embedding and store in vector table
                    embedding = self.embed_text(chunk_content)
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

    def search(
        self,
        query: str,
        user_id: str,
        limit: int = 5,
        chunk_types: Optional[List[str]] = None,
    ) -> List[ChunkResult]:
        """
        Hybrid search combining vector similarity and BM25 full-text search.

        Args:
            query: Search query text
            user_id: User ID to search within
            limit: Maximum results to return
            chunk_types: Optional filter for chunk types

        Returns:
            List of ChunkResult objects sorted by relevance
        """
        if not query or not query.strip():
            return []

        results = []

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()

                # Build type filter clause
                type_filter = ""
                type_params: List[Any] = []
                if chunk_types:
                    placeholders = ", ".join("?" for _ in chunk_types)
                    type_filter = f"AND chunk_type IN ({placeholders})"
                    type_params = list(chunk_types)

                # Try vector search first
                vector_results = {}
                query_embedding = self.embed_text(query)

                if query_embedding:
                    try:
                        # Find similar vectors
                        cursor.execute(f"""
                            SELECT chunk_id, distance
                            FROM vec_chunks
                            WHERE embedding MATCH ?
                            ORDER BY distance
                            LIMIT ?
                        """, (self._serialize_embedding(query_embedding), limit * 3))

                        for row in cursor.fetchall():
                            # Convert distance to similarity score (0-1, higher is better)
                            # sqlite-vec returns L2 distance, so we invert it
                            similarity = 1 / (1 + row['distance'])
                            vector_results[row['chunk_id']] = similarity

                    except sqlite3.OperationalError as e:
                        if "no such table: vec_chunks" not in str(e):
                            logger.warning(f"Vector search failed: {e}")

                # BM25 full-text search
                bm25_results = {}
                try:
                    cursor.execute(f"""
                        SELECT c.id, c.content, c.chunk_type, c.thread_id,
                               c.created_at, c.metadata, bm25(chunks_fts) as score
                        FROM chunks_fts fts
                        JOIN chunks c ON c.rowid = fts.rowid
                        WHERE chunks_fts MATCH ?
                        AND c.user_id = ?
                        {type_filter}
                        ORDER BY score
                        LIMIT ?
                    """, (query, user_id, *type_params, limit * 3))

                    for row in cursor.fetchall():
                        # BM25 scores are negative, lower is better
                        # Normalize to 0-1 range (approximate)
                        bm25_score = 1 / (1 - row['score']) if row['score'] < 0 else 0.5
                        bm25_results[row['id']] = {
                            'score': bm25_score,
                            'row': dict(row),
                        }
                except Exception as e:
                    logger.warning(f"BM25 search failed: {e}")

                # Combine results with hybrid scoring
                combined_scores = {}

                # Add vector results
                for chunk_id, vec_score in vector_results.items():
                    combined_scores[chunk_id] = vec_score * 0.7  # Weight vector higher

                # Add BM25 results
                for chunk_id, data in bm25_results.items():
                    current = combined_scores.get(chunk_id, 0)
                    combined_scores[chunk_id] = current + data['score'] * 0.3

                # If no vector results, just use BM25
                if not vector_results and bm25_results:
                    for chunk_id, data in bm25_results.items():
                        combined_scores[chunk_id] = data['score']

                # Fetch full chunk data for top results
                if combined_scores:
                    sorted_ids = sorted(
                        combined_scores.keys(),
                        key=lambda x: combined_scores[x],
                        reverse=True
                    )[:limit]

                    for chunk_id in sorted_ids:
                        # Get from BM25 results if available, otherwise fetch
                        if chunk_id in bm25_results:
                            row = bm25_results[chunk_id]['row']
                        else:
                            cursor.execute(f"""
                                SELECT id, content, chunk_type, thread_id, created_at, metadata
                                FROM chunks
                                WHERE id = ? AND user_id = ?
                                {type_filter}
                            """, (chunk_id, user_id, *type_params))
                            row_data = cursor.fetchone()
                            if not row_data:
                                continue
                            row = dict(row_data)

                        # Parse metadata
                        try:
                            metadata = json.loads(row['metadata']) if row['metadata'] else {}
                        except json.JSONDecodeError:
                            metadata = {}

                        # Parse created_at
                        created_at = row['created_at']
                        if isinstance(created_at, str):
                            try:
                                created_at = datetime.fromisoformat(created_at)
                            except ValueError:
                                created_at = datetime.utcnow()

                        results.append(ChunkResult(
                            id=row['id'],
                            content=row['content'],
                            chunk_type=row['chunk_type'],
                            thread_id=row['thread_id'],
                            created_at=created_at,
                            metadata=metadata,
                            score=combined_scores[chunk_id],
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
                    pass

                return {
                    "total_chunks": total,
                    "by_type": by_type,
                    "last_indexed": last_indexed,
                    "vector_count": vector_count,
                    "embedding_provider": self.embedding_provider,
                }

            finally:
                conn.close()
