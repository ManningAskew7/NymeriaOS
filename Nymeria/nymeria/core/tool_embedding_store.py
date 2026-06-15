"""Persistent embedding cache for the tool-search catalog.

``ToolSearchIndex`` ranks the tool catalog (~1300 tools) semantically using
cosine similarity over in-memory vectors. Those vectors used to live only in a
process-local dict, so every API restart dropped them and the next
``tool_search`` re-embedded the whole catalog inline (one HTTP call per tool),
which could exceed the 300s tool-execution timeout and hang the turn. This
store persists the vectors to a small sqlite file keyed by each tool's content
hash, so a restart reloads them from disk instead of re-embedding.

It is intentionally a plain sqlite blob cache, NOT a sqlite-vec store: cosine
ranking stays in Python over the resident vectors (the catalog is small enough
to keep in memory), so we only need to persist and reload the vectors, not run
on-disk vector search. Vectors are tagged with the embedding model and
dimensions; if either changes, the cache is wiped and rebuilt so a model swap
never mixes incompatible vector spaces.

Pure persistence: this class never makes network calls. Every operation is
best-effort and degrades to a no-op on any sqlite error, so a disk problem can
never break tool search (the in-memory cache and keyword fallbacks still work).
"""

from __future__ import annotations

import logging
import sqlite3
import struct
import threading
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


class ToolEmbeddingStore:
    """Disk-backed cache of tool embeddings keyed by content hash."""

    def __init__(self, db_path: Path, *, model: str, dimensions: int) -> None:
        self.db_path = Path(db_path)
        self._model = model or ""
        self._dimensions = int(dimensions)
        self._lock = threading.RLock()
        self._usable = False
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self._usable = True
        except Exception as exc:  # noqa: BLE001 - the cache is optional.
            logger.warning("tool embedding store unavailable (%s); search stays in-memory", exc)

    @property
    def usable(self) -> bool:
        return self._usable

    # ------------------------------------------------------------------
    # connection + schema
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        # WAL + a busy timeout let the background warm write while request
        # threads read without "database is locked"; the store is single-writer
        # in practice (one API process), so this is just defensive.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tool_embeddings (
                        content_hash TEXT PRIMARY KEY,
                        dim INTEGER NOT NULL,
                        vec BLOB NOT NULL
                    )
                    """
                )
                c.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                # Reconcile against the configured (model, dimensions). A
                # mismatch means stored vectors are from a different embedding
                # space, so drop them rather than mixing incompatible vectors.
                stored_model = self._meta_get(c, "model")
                stored_dim = self._meta_get(c, "dim")
                if stored_model != self._model or stored_dim != str(self._dimensions):
                    c.execute("DELETE FROM tool_embeddings")
                    self._meta_set(c, "model", self._model)
                    self._meta_set(c, "dim", str(self._dimensions))
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _meta_get(cursor: sqlite3.Cursor, key: str) -> Optional[str]:
        row = cursor.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    @staticmethod
    def _meta_set(cursor: sqlite3.Cursor, key: str, value: str) -> None:
        cursor.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )

    @staticmethod
    def _pack(vec: List[float]) -> bytes:
        return struct.pack(f"{len(vec)}f", *vec)

    @staticmethod
    def _unpack(blob: bytes, dim: int) -> List[float]:
        return list(struct.unpack(f"{dim}f", blob))

    # ------------------------------------------------------------------
    # read / write
    # ------------------------------------------------------------------

    def get_many(self, hashes: Iterable[str]) -> dict[str, List[float]]:
        """Load stored vectors for the given content hashes.

        Returns ``{content_hash: vector}`` for rows that exist and match the
        configured dimension. Missing or mismatched rows are simply absent so
        the caller re-embeds them.
        """
        out: dict[str, List[float]] = {}
        if not self._usable:
            return out
        hash_list = [h for h in hashes if h]
        if not hash_list:
            return out
        with self._lock:
            conn = self._connect()
            try:
                c = conn.cursor()
                # Chunk to stay under SQLite's bound-parameter ceiling (999).
                for i in range(0, len(hash_list), 900):
                    chunk = hash_list[i:i + 900]
                    placeholders = ",".join("?" * len(chunk))
                    rows = c.execute(
                        "SELECT content_hash, dim, vec FROM tool_embeddings "
                        f"WHERE content_hash IN ({placeholders})",
                        chunk,
                    ).fetchall()
                    for row in rows:
                        if row["dim"] != self._dimensions:
                            continue
                        try:
                            out[row["content_hash"]] = self._unpack(row["vec"], row["dim"])
                        except Exception:  # noqa: BLE001 - skip a corrupt row.
                            continue
            except Exception as exc:  # noqa: BLE001 - cache read is best-effort.
                logger.warning("tool embedding store read failed: %s", exc)
            finally:
                conn.close()
        return out

    def put_many(self, items: List[Tuple[str, List[float]]]) -> None:
        """Persist ``(content_hash, vector)`` pairs in one transaction."""
        if not self._usable or not items:
            return
        rows = [
            (h, len(vec), self._pack(vec))
            for h, vec in items
            if h and vec and len(vec) == self._dimensions
        ]
        if not rows:
            return
        with self._lock:
            conn = self._connect()
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO tool_embeddings (content_hash, dim, vec) "
                    "VALUES (?, ?, ?)",
                    rows,
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - cache write is best-effort.
                logger.warning("tool embedding store write failed: %s", exc)
            finally:
                conn.close()

    def count(self) -> int:
        """Number of stored embeddings (0 if the store is unusable)."""
        if not self._usable:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute("SELECT COUNT(*) AS n FROM tool_embeddings").fetchone()
                return int(row["n"]) if row else 0
            except Exception:  # noqa: BLE001 - best-effort.
                return 0
            finally:
                conn.close()
