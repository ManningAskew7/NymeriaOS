#!/usr/bin/env python
"""Inspect persisted conversation history for a given thread.

Usage:
    python tools/inspect_thread.py <thread_id>
    python tools/inspect_thread.py <thread_id> --last 5     # only last N messages
    python tools/inspect_thread.py <thread_id> --full       # show full content
    python tools/inspect_thread.py --list                   # list threads with message counts
"""

import argparse
import io
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

BackendName = Literal["sqlite", "postgres"]


class InspectError(RuntimeError):
    """User-facing inspection failure."""


@dataclass(frozen=True)
class StoreConfig:
    backend: BackendName
    sqlite_path: Path | None = None
    postgres_uri: str | None = None


class CheckpointStore:
    def __init__(self, config: StoreConfig, saver: Any, conn: Any):
        self.config = config
        self.saver = saver
        self.conn = conn

    @classmethod
    def open(cls, config: StoreConfig) -> "CheckpointStore":
        if config.backend == "sqlite":
            return cls._open_sqlite(config)
        if config.backend == "postgres":
            return cls._open_postgres(config)
        raise InspectError(f"Unsupported checkpoint backend: {config.backend}")

    @classmethod
    def _open_sqlite(cls, config: StoreConfig) -> "CheckpointStore":
        if config.sqlite_path is None:
            raise InspectError("SQLite backend selected but no SQLite path is configured")
        if not config.sqlite_path.exists():
            raise InspectError(f"SQLite checkpoint database not found: {config.sqlite_path}")

        from langgraph.checkpoint.sqlite import SqliteSaver

        conn = sqlite3.connect(str(config.sqlite_path), check_same_thread=False)
        return cls(config, SqliteSaver(conn), conn)

    @classmethod
    def _open_postgres(cls, config: StoreConfig) -> "CheckpointStore":
        if not config.postgres_uri:
            raise InspectError("PostgreSQL backend selected but POSTGRES_URI is not configured")
        try:
            import psycopg  # type: ignore[import-untyped]
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:
            raise InspectError(
                "PostgreSQL inspection requires the Postgres checkpoint extras. "
                "Install them with: pip install -r requirements-postgres.txt"
            ) from exc

        conn = psycopg.connect(config.postgres_uri, autocommit=True)
        return cls(config, PostgresSaver(conn), conn)

    def close(self) -> None:
        close = getattr(self.conn, "close", None)
        if callable(close):
            close()

    def thread_ids(self) -> list[str]:
        try:
            if self.config.backend == "sqlite":
                if not _sqlite_table_exists(self.conn, "checkpoints"):
                    return []
                cursor = self.conn.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
                )
                return [row[0] for row in cursor.fetchall()]

            with self.conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", ("checkpoints",))
                row = cur.fetchone()
                if not row or row[0] is None:
                    return []
                cur.execute("SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id")
                return [row[0] for row in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise InspectError(
                f"Failed to query thread IDs from {self.config.backend} checkpoints: {exc}"
            ) from exc

    def latest_messages(self, thread_id: str) -> list[Any] | None:
        if self.config.backend == "sqlite" and not _sqlite_table_exists(self.conn, "checkpoints"):
            return None
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        try:
            checkpoint_tuple = self.saver.get_tuple(config)
        except Exception as exc:  # noqa: BLE001
            raise InspectError(
                f"Failed to read latest checkpoint for {thread_id}: {exc}"
            ) from exc
        if not checkpoint_tuple:
            return None
        return checkpoint_tuple.checkpoint.get("channel_values", {}).get("messages", [])


def _ensure_utf8_stdout() -> None:
    """Force UTF-8 output on Windows terminals without replacing stdout in tests."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    elif hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def resolve_store_config(args: argparse.Namespace) -> StoreConfig:
    from nymeria.config.settings import get_settings

    settings = get_settings()
    backend = args.backend or settings.database_backend
    if backend == "memory":
        raise InspectError("The memory checkpoint backend has no persisted history to inspect")
    if backend == "sqlite":
        sqlite_path = Path(args.sqlite_path).expanduser() if args.sqlite_path else settings.db_path
        return StoreConfig(backend="sqlite", sqlite_path=sqlite_path)
    if backend == "postgres":
        postgres_uri = args.postgres_uri or settings.postgres_uri
        return StoreConfig(backend="postgres", postgres_uri=postgres_uri)
    raise InspectError(f"Unsupported checkpoint backend: {backend}")


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return repr(content)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "..."


def list_threads(config: StoreConfig) -> None:
    store = CheckpointStore.open(config)
    try:
        rows: list[tuple[str, str]] = []
        for thread_id in store.thread_ids():
            try:
                messages = store.latest_messages(thread_id) or []
                count = str(len(messages))
            except Exception as exc:  # noqa: BLE001
                count = f"error: {exc}"
            rows.append((thread_id, count))

        print(f"Checkpoint backend: {config.backend}")
        print(f"{'Thread ID':<45} {'Messages':>8}")
        print("-" * 55)
        for thread_id, count in rows:
            print(f"{thread_id:<45} {count:>8}")
        print(f"\n{len(rows)} threads total")
    finally:
        store.close()


def inspect_thread(
    config: StoreConfig,
    thread_id: str,
    last_n: int | None = None,
    full: bool = False,
) -> None:
    store = CheckpointStore.open(config)
    try:
        messages = store.latest_messages(thread_id)
        if messages is None:
            print(f"No checkpoint found for thread: {thread_id}")
            return

        total = len(messages)

        if last_n and last_n < total:
            messages = messages[-last_n:]
            print(f"Showing last {last_n} of {total} messages")
        else:
            print(f"Thread: {thread_id}")
            print(f"Messages: {total}")

        print("=" * 80)

        max_content = 500 if not full else 999999

        for i, msg in enumerate(messages):
            idx = (total - len(messages)) + i
            msg_type = type(msg).__name__
            content = _content_to_text(msg.content if hasattr(msg, "content") else msg)
            tool_calls = getattr(msg, "tool_calls", [])
            tool_name = getattr(msg, "name", None)

            label = msg_type
            if tool_name:
                label += f" (tool={tool_name})"

            content_display = _truncate(content, max_content)

            print(f"\n[{idx}] {label}")
            if content_display.strip():
                for line in content_display.split("\n"):
                    print(f"    {line}")
            else:
                print("    (empty)")

            if tool_calls:
                for tool_call in tool_calls:
                    if isinstance(tool_call, dict):
                        tool_call_name = tool_call.get("name", "<unknown>")
                        args = tool_call.get("args", {})
                    else:
                        tool_call_name = getattr(tool_call, "name", "<unknown>")
                        args = getattr(tool_call, "args", {})
                    args_str = _truncate(str(args), max_content)
                    print(f"    -> tool_call: {tool_call_name}({args_str})")

        print("\n" + "=" * 80)
    finally:
        store.close()


def main() -> None:
    _ensure_utf8_stdout()
    parser = argparse.ArgumentParser(description="Inspect persisted thread conversation history")
    parser.add_argument("thread_id", nargs="?", help="Thread ID to inspect")
    parser.add_argument("--last", type=int, help="Show only last N messages")
    parser.add_argument("--full", action="store_true", help="Show full content without truncation")
    parser.add_argument("--list", action="store_true", help="List all threads with message counts")
    parser.add_argument(
        "--backend",
        choices=["sqlite", "postgres"],
        help="Override DATABASE_BACKEND for this inspection",
    )
    parser.add_argument("--sqlite-path", help="Override SQLITE_PATH/data/nymeria.db")
    parser.add_argument("--postgres-uri", help="Override POSTGRES_URI")

    args = parser.parse_args()

    try:
        config = resolve_store_config(args)
        if args.list:
            list_threads(config)
        elif args.thread_id:
            inspect_thread(config, args.thread_id, last_n=args.last, full=args.full)
        else:
            parser.print_help()
    except InspectError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
