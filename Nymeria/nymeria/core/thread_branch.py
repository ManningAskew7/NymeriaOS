"""Thread branching helpers.

Branching creates a new thread that inherits the source thread's checkpoint
history and per-thread configuration. Checkpoints are copied at the persistence
layer so the new thread has the same historical context without mutating the
source thread.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import AbstractContextManager, closing, contextmanager
from pathlib import Path
from typing import Any

from .callable_names import dedupe_callable_name, safe_callable_base
from .checkpoint_cleanup import delete_thread_checkpoints
from .checkpoint_sql import postgres_table_exists, sqlite_table_exists
from .thread_config import ThreadConfig
from .time_utils import utc_now

logger = logging.getLogger(__name__)

CHECKPOINT_BRANCH_TABLES = (
    "checkpoints",
    "writes",
    "checkpoint_writes",
    "checkpoint_blobs",
)

# Walks the checkpoint parent chain from a target checkpoint back to the root. The
# SQLite and Postgres dialects render this with their own parameter placeholder
# (``?`` / ``%s``); the text is otherwise identical, so it lives once. The three
# placeholders bind, in order: source_thread_id, source_checkpoint_id, source_thread_id.
_SELECTED_CHECKPOINT_IDS_CTE = """
        WITH RECURSIVE selected(checkpoint_ns, checkpoint_id, parent_checkpoint_id) AS (
            SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id
            FROM checkpoints
            WHERE thread_id = {ph} AND checkpoint_ns = '' AND checkpoint_id = {ph}
            UNION ALL
            SELECT c.checkpoint_ns, c.checkpoint_id, c.parent_checkpoint_id
            FROM checkpoints c
            JOIN selected s
              ON c.thread_id = {ph}
             AND c.checkpoint_ns = s.checkpoint_ns
             AND c.checkpoint_id = s.parent_checkpoint_id
        )
        SELECT checkpoint_id FROM selected
        """


class ThreadBranchError(RuntimeError):
    """Raised when a thread cannot be branched."""


def branch_thread(
    *,
    agent: Any,
    settings: Any,
    user_id: str,
    source_thread_id: str,
    title: str | None = None,
    from_message_index: int | None = None,
    new_thread_id: str | None = None,
) -> dict[str, Any]:
    """Create a branch thread from ``source_thread_id``."""

    if from_message_index is not None and from_message_index < 1:
        raise ThreadBranchError("from_message_index must be 1 or greater")

    target_thread_id = new_thread_id or f"branch-{uuid.uuid4().hex[:12]}"
    source_title = _source_thread_title(agent, user_id, source_thread_id)
    branch_title = _branch_title(title, source_title)

    if _thread_owner(agent, target_thread_id) is not None:
        raise ThreadBranchError(f"Target thread already exists: {target_thread_id}")

    source_checkpoint_id = _checkpoint_id_for_message_index(
        agent,
        source_thread_id,
        from_message_index,
    )
    checkpoint_counts = clone_thread_checkpoints(
        settings,
        source_thread_id,
        target_thread_id,
        source_checkpoint_id=source_checkpoint_id,
    )

    config_cloned = False
    callable_name: str | None = None
    try:
        config_cloned, callable_name = _clone_thread_config(
            agent,
            user_id=user_id,
            source_thread_id=source_thread_id,
            target_thread_id=target_thread_id,
            title=branch_title,
        )
        owner = agent.accounts_repo.claim_thread(target_thread_id, user_id)
        if owner != user_id:
            raise ThreadBranchError(f"Target thread already owned by {owner}")

        metadata_title = callable_name or branch_title
        metadata_source = "callable" if callable_name else "user"
        metadata_platform = "callable" if callable_name else "desktop"
        meta = agent.thread_metadata_manager.upsert_thread(
            user_id,
            target_thread_id,
            title=metadata_title,
            title_source=metadata_source,
            platform=metadata_platform,
        )
    except Exception:
        _cleanup_failed_branch(agent, settings, user_id, target_thread_id)
        raise

    return {
        "status": "ok",
        "source_thread_id": source_thread_id,
        "thread_id": target_thread_id,
        "title": metadata_title,
        "requested_title": branch_title,
        "from_message_index": from_message_index,
        "source_checkpoint_id": source_checkpoint_id,
        "checkpoints": checkpoint_counts,
        "config_cloned": config_cloned,
        "callable_name": callable_name,
        "metadata": meta.model_dump(mode="json"),
    }


def clone_thread_checkpoints(
    settings: Any,
    source_thread_id: str,
    target_thread_id: str,
    *,
    source_checkpoint_id: str | None = None,
) -> dict[str, int]:
    """Copy checkpoint rows for one thread ID to another."""

    dialect = _resolve_branch_dialect(settings)
    if dialect is None:
        if source_checkpoint_id is not None:
            raise ThreadBranchError(
                "Branching from a message index requires a persistent checkpoint backend"
            )
        return {f"{table}_copied": 0 for table in CHECKPOINT_BRANCH_TABLES}
    return _clone_with_dialect(
        dialect,
        source_thread_id,
        target_thread_id,
        source_checkpoint_id,
    )


def _resolve_branch_dialect(settings: Any) -> _BranchDialect | None:
    """Pick the checkpoint dialect for the configured backend.

    Returns ``None`` for any backend that does not persist checkpoints; callers treat
    that as a zero-copy no-op (clone) or a skip (delete).
    """

    backend = getattr(settings, "database_backend", "sqlite")
    if backend == "sqlite":
        return _SqliteBranchDialect(Path(settings.db_path))
    if backend == "postgres":
        postgres_uri = getattr(settings, "postgres_uri", None)
        if not postgres_uri:
            raise ThreadBranchError("Postgres checkpoint backend selected without POSTGRES_URI")
        return _PostgresBranchDialect(postgres_uri)
    return None


def _clone_with_dialect(
    dialect: _BranchDialect,
    source_thread_id: str,
    target_thread_id: str,
    source_checkpoint_id: str | None,
) -> dict[str, int]:
    counts = {f"{table}_copied": 0 for table in CHECKPOINT_BRANCH_TABLES}
    with dialect.transaction() as handle:
        selected_ids = dialect.select_checkpoint_ids(
            handle,
            source_thread_id,
            source_checkpoint_id,
        )
        _raise_if_target_exists(dialect, handle, target_thread_id)
        for table in CHECKPOINT_BRANCH_TABLES:
            columns = dialect.table_columns(handle, table)
            if "thread_id" not in columns:
                continue
            counts[f"{table}_copied"] = dialect.copy_table_rows(
                handle,
                table,
                columns,
                source_thread_id,
                target_thread_id,
                selected_ids,
            )
    return counts


def _raise_if_target_exists(
    dialect: _BranchDialect,
    handle: Any,
    target_thread_id: str,
) -> None:
    for table in CHECKPOINT_BRANCH_TABLES:
        columns = dialect.table_columns(handle, table)
        if "thread_id" not in columns:
            continue
        if dialect.thread_has_rows(handle, table, target_thread_id):
            raise ThreadBranchError(f"Target thread already has checkpoint rows: {target_thread_id}")


class _BranchDialect:
    """Backend-specific checkpoint-branch SQL.

    The shared logic lives here and in the module-level orchestration: the
    ``select_checkpoint_ids`` scaffold (the required-columns guard and the single
    recursive-CTE template), plus the table loop and the raise/copy/delete control
    flow, are written and changed once. Subclasses own only the genuinely
    dialect-divergent pieces: the connection lifecycle, column introspection, the
    parameter placeholder (``?`` vs ``%s``), identifier quoting, ``IN (...)`` vs
    ``= ANY(...)``, the CTE execute/fetch shape, and the missing-``checkpoints``-table
    policy (SQLite short-circuits to an empty selection via
    ``_checkpoint_columns_for_selection`` returning ``None``; Postgres returns the
    empty column list so the guard raises).
    """

    def transaction(self) -> AbstractContextManager[Any]:
        raise NotImplementedError

    def table_columns(self, handle: Any, table: str) -> list[str]:
        raise NotImplementedError

    def select_checkpoint_ids(
        self,
        handle: Any,
        source_thread_id: str,
        source_checkpoint_id: str | None,
    ) -> set[str] | None:
        if source_checkpoint_id is None:
            return None
        columns = self._checkpoint_columns_for_selection(handle)
        if columns is None:
            return set()
        required = {"thread_id", "checkpoint_ns", "checkpoint_id", "parent_checkpoint_id"}
        if not required.issubset(columns):
            raise ThreadBranchError("Checkpoint table does not support message-index branching")
        return self._run_selected_checkpoint_ids_cte(
            handle,
            (source_thread_id, source_checkpoint_id, source_thread_id),
        )

    def _checkpoint_columns_for_selection(self, handle: Any) -> list[str] | None:
        """Columns of the ``checkpoints`` table, or ``None`` to short-circuit to an
        empty selection (the SQLite missing-table behaviour)."""
        raise NotImplementedError

    def _run_selected_checkpoint_ids_cte(self, handle: Any, params: tuple[str, ...]) -> set[str]:
        raise NotImplementedError

    def thread_has_rows(self, handle: Any, table: str, thread_id: str) -> bool:
        raise NotImplementedError

    def copy_table_rows(
        self,
        handle: Any,
        table: str,
        columns: list[str],
        source_thread_id: str,
        target_thread_id: str,
        selected_ids: set[str] | None,
    ) -> int:
        raise NotImplementedError

    def delete_table_rows(self, handle: Any, table: str, thread_id: str) -> None:
        raise NotImplementedError


class _SqliteBranchDialect(_BranchDialect):
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def transaction(self) -> Generator[Any, None, None]:
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            yield conn
            conn.commit()

    def table_columns(self, handle: Any, table: str) -> list[str]:
        # PRAGMA table_info returns an empty result for an absent table, so the loops
        # that gate on "thread_id" in columns skip missing tables without a separate
        # existence probe.
        return [
            str(row[1])
            for row in handle.execute(f"PRAGMA table_info({_quote_sqlite_identifier(table)})")
        ]

    def _checkpoint_columns_for_selection(self, handle: Any) -> list[str] | None:
        if not sqlite_table_exists(handle, "checkpoints"):
            return None
        return self.table_columns(handle, "checkpoints")

    def _run_selected_checkpoint_ids_cte(self, handle: Any, params: tuple[str, ...]) -> set[str]:
        rows = handle.execute(_SELECTED_CHECKPOINT_IDS_CTE.format(ph="?"), params).fetchall()
        return {str(row[0]) for row in rows if row[0]}

    def thread_has_rows(self, handle: Any, table: str, thread_id: str) -> bool:
        row = handle.execute(
            f"SELECT 1 FROM {_quote_sqlite_identifier(table)} WHERE thread_id = ? LIMIT 1",
            (thread_id,),
        ).fetchone()
        return row is not None

    def copy_table_rows(
        self,
        handle: Any,
        table: str,
        columns: list[str],
        source_thread_id: str,
        target_thread_id: str,
        selected_ids: set[str] | None,
    ) -> int:
        quoted_table = _quote_sqlite_identifier(table)
        quoted_columns = [_quote_sqlite_identifier(column) for column in columns]
        select_columns = [
            "? AS thread_id" if column == "thread_id" else _quote_sqlite_identifier(column)
            for column in columns
        ]
        where = ["thread_id = ?"]
        params: list[Any] = [target_thread_id, source_thread_id]
        if selected_ids is not None and "checkpoint_id" in columns:
            if not selected_ids:
                return 0
            placeholders = ", ".join("?" for _ in selected_ids)
            where.append(f"checkpoint_id IN ({placeholders})")
            params.extend(sorted(selected_ids))

        cursor = handle.execute(
            f"INSERT INTO {quoted_table} ({', '.join(quoted_columns)}) "
            f"SELECT {', '.join(select_columns)} FROM {quoted_table} "
            f"WHERE {' AND '.join(where)}",
            tuple(params),
        )
        return _rowcount(cursor.rowcount)

    def delete_table_rows(self, handle: Any, table: str, thread_id: str) -> None:
        handle.execute(
            f"DELETE FROM {_quote_sqlite_identifier(table)} WHERE thread_id = ?",
            (thread_id,),
        )


class _PostgresBranchDialect(_BranchDialect):
    def __init__(self, postgres_uri: str) -> None:
        self.postgres_uri = postgres_uri

    @contextmanager
    def transaction(self) -> Generator[Any, None, None]:
        import psycopg  # type: ignore[import-untyped]

        with psycopg.connect(self.postgres_uri) as conn:
            with conn.cursor() as cur:
                yield cur
            conn.commit()

    def table_columns(self, handle: Any, table: str) -> list[str]:
        if not postgres_table_exists(handle, table):
            return []
        handle.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = %s
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [str(row[0]) for row in handle.fetchall()]

    def _checkpoint_columns_for_selection(self, handle: Any) -> list[str] | None:
        # An absent table yields [], so the required-columns guard raises (Postgres has
        # no SQLite-style empty-selection short-circuit).
        return self.table_columns(handle, "checkpoints")

    def _run_selected_checkpoint_ids_cte(self, handle: Any, params: tuple[str, ...]) -> set[str]:
        handle.execute(_SELECTED_CHECKPOINT_IDS_CTE.format(ph="%s"), params)
        return {str(row[0]) for row in handle.fetchall() if row[0]}

    def thread_has_rows(self, handle: Any, table: str, thread_id: str) -> bool:
        handle.execute(f"SELECT 1 FROM {table} WHERE thread_id = %s LIMIT 1", (thread_id,))
        return handle.fetchone() is not None

    def copy_table_rows(
        self,
        handle: Any,
        table: str,
        columns: list[str],
        source_thread_id: str,
        target_thread_id: str,
        selected_ids: set[str] | None,
    ) -> int:
        from psycopg import sql  # type: ignore[import-untyped]

        select_columns = [
            sql.SQL("%s AS thread_id") if column == "thread_id" else sql.Identifier(column)
            for column in columns
        ]
        params: list[Any] = [target_thread_id, source_thread_id]
        where = [sql.SQL("thread_id = %s")]
        if selected_ids is not None and "checkpoint_id" in columns:
            if not selected_ids:
                return 0
            where.append(sql.SQL("checkpoint_id = ANY(%s)"))
            params.append(sorted(selected_ids))
        query = sql.SQL(
            "INSERT INTO {table} ({columns}) SELECT {select_columns} FROM {table} WHERE {where}"
        ).format(
            table=sql.Identifier(table),
            columns=sql.SQL(", ").join(sql.Identifier(column) for column in columns),
            select_columns=sql.SQL(", ").join(select_columns),
            where=sql.SQL(" AND ").join(where),
        )
        handle.execute(query, tuple(params))
        return _rowcount(handle.rowcount)

    def delete_table_rows(self, handle: Any, table: str, thread_id: str) -> None:
        from psycopg import sql  # type: ignore[import-untyped]

        handle.execute(
            sql.SQL("DELETE FROM {table} WHERE thread_id = %s").format(table=sql.Identifier(table)),
            (thread_id,),
        )


def _checkpoint_id_for_message_index(
    agent: Any,
    source_thread_id: str,
    from_message_index: int | None,
) -> str | None:
    if from_message_index is None:
        return None
    graph = getattr(agent, "_default_graph", None)
    if graph is None:
        raise ThreadBranchError("Message-index branching requires graph state history")

    config = {"configurable": {"thread_id": source_thread_id}}
    latest_count = _message_count(getattr(graph.get_state(config), "values", {}))
    if from_message_index > latest_count:
        raise ThreadBranchError(
            f"from_message_index {from_message_index} exceeds current message count {latest_count}"
        )

    selected_checkpoint_id = None
    selected_count = -1
    for state in graph.get_state_history(config):
        count = _message_count(getattr(state, "values", {}))
        checkpoint_id = _state_checkpoint_id(state)
        if checkpoint_id is None:
            continue
        if count <= from_message_index and count > selected_count:
            selected_checkpoint_id = checkpoint_id
            selected_count = count
            if count == from_message_index:
                break

    if selected_checkpoint_id is None:
        raise ThreadBranchError(
            f"No checkpoint found at or before message index {from_message_index}"
        )
    return selected_checkpoint_id


def _message_count(values: Any) -> int:
    if not isinstance(values, dict):
        return 0
    messages = values.get("messages", [])
    if not isinstance(messages, list):
        return 0
    return len(messages)


def _state_checkpoint_id(state: Any) -> str | None:
    config = getattr(state, "config", None)
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    checkpoint_id = configurable.get("checkpoint_id")
    return str(checkpoint_id) if checkpoint_id else None


def _clone_thread_config(
    agent: Any,
    *,
    user_id: str,
    source_thread_id: str,
    target_thread_id: str,
    title: str,
) -> tuple[bool, str | None]:
    manager = getattr(agent, "thread_config_manager", None)
    if manager is None:
        return False, None
    source_config = manager.get_config(source_thread_id)
    if source_config is None:
        return False, None

    data = source_config.model_dump(mode="python")
    data["thread_id"] = target_thread_id
    data["created_at"] = utc_now()
    data["updated_at"] = utc_now()

    callable_name = None
    if source_config.callable:
        callable_name = _unique_callable_name(
            agent,
            user_id,
            title or source_config.callable_name or "Branch",
        )
        data["callable_name"] = callable_name

    clone = ThreadConfig.model_validate(data)
    if not manager.save_config(clone):
        raise ThreadBranchError("Failed to save branched thread config")

    _invalidate_thread_config(agent, target_thread_id)
    if clone.callable:
        sync_tools = getattr(agent, "sync_agent_tools", None)
        if callable(sync_tools):
            sync_tools()
    return True, callable_name


def _unique_callable_name(agent: Any, user_id: str, desired: str) -> str:
    base = _safe_callable_name(desired)
    if not base.endswith("_branch"):
        base = f"{base[:57]}_branch"

    unavailable = _unavailable_callable_names(agent, user_id)
    candidate = dedupe_callable_name(base, unavailable)
    if candidate is None:
        raise ThreadBranchError("Could not create a unique callable name for branch")
    return candidate


def _safe_callable_name(value: str) -> str:
    return safe_callable_base(value, fallback="Branch", digit_prefix="Branch", max_len=64)


def _unavailable_callable_names(agent: Any, user_id: str) -> set[str]:
    names: set[str] = set()
    try:
        from ..tools import SEED_TOOLS

        names.update(tool.name for tool in SEED_TOOLS)
    except Exception:
        logger.warning("Failed to inspect core tool names while branching", exc_info=True)

    manager = getattr(agent, "thread_config_manager", None)
    if manager is None:
        return names
    try:
        owned = set(agent.accounts_repo.list_threads_for_user(user_id))
        names.update(
            config.callable_name
            for config in manager.list_callable_threads(owned_thread_ids=owned)
            if config.callable_name
        )
    except Exception:
        logger.warning("Failed to inspect callable names while branching", exc_info=True)
    return names


def _source_thread_title(agent: Any, user_id: str, source_thread_id: str) -> str:
    meta = agent.thread_metadata_manager.get_thread(user_id, source_thread_id)
    if meta and meta.title:
        return meta.title
    return "New Chat"


def _branch_title(title: str | None, source_title: str) -> str:
    cleaned = " ".join(str(title or "").split())
    if cleaned:
        return cleaned[:200]
    return f"Branch of {source_title or 'New Chat'}"[:200]


def _thread_owner(agent: Any, thread_id: str) -> str | None:
    getter = getattr(agent.accounts_repo, "get_thread_owner", None)
    if not callable(getter):
        return None
    result = getter(thread_id)
    return str(result) if result is not None else None


def _cleanup_failed_branch(
    agent: Any,
    settings: Any,
    user_id: str,
    thread_id: str,
) -> None:
    try:
        _delete_branch_checkpoint_rows(settings, thread_id)
    except Exception:
        logger.warning("Failed to clean branch checkpoints for %s", thread_id, exc_info=True)
        try:
            delete_thread_checkpoints(settings, thread_id)
        except Exception:
            logger.warning(
                "Fallback checkpoint cleanup also failed for %s",
                thread_id,
                exc_info=True,
            )
    try:
        manager = getattr(agent, "thread_config_manager", None)
        if manager is not None:
            manager.delete_config(thread_id)
    except Exception:
        logger.warning("Failed to clean branch config for %s", thread_id, exc_info=True)
    try:
        agent.thread_metadata_manager.delete_thread(user_id, thread_id)
    except Exception:
        logger.warning("Failed to clean branch metadata for %s", thread_id, exc_info=True)
    try:
        delete_owner = getattr(agent.accounts_repo, "delete_thread_owner", None)
        if callable(delete_owner):
            delete_owner(thread_id)
    except Exception:
        logger.warning("Failed to clean branch owner for %s", thread_id, exc_info=True)


def _invalidate_thread_config(agent: Any, thread_id: str) -> None:
    invalidate = getattr(agent, "invalidate_thread_config_cache", None)
    if callable(invalidate):
        invalidate(thread_id)


def _delete_branch_checkpoint_rows(settings: Any, thread_id: str) -> None:
    dialect = _resolve_branch_dialect(settings)
    if dialect is None:
        return
    with dialect.transaction() as handle:
        for table in CHECKPOINT_BRANCH_TABLES:
            columns = dialect.table_columns(handle, table)
            if "thread_id" not in columns:
                continue
            dialect.delete_table_rows(handle, table, thread_id)


def _quote_sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _rowcount(value: int | None) -> int:
    if value is None or value < 0:
        return 0
    return value


__all__ = [
    "ThreadBranchError",
    "branch_thread",
    "clone_thread_checkpoints",
]
