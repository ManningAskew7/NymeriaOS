"""
The ReAct Agent Graph

Modular graph construction that accepts configuration for framework integration.
Supports multiple checkpointer backends and custom tools.
"""

import asyncio
import atexit
import logging
import sqlite3
from typing import Callable, List, Optional, Any
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from .state import AgentState
from .config import AgentConfig, CheckpointerConfig, default_config
from .nodes import NodeFactory

# Shared savers for consistent state + persistence
_shared_sqlite_conn: Optional[sqlite3.Connection] = None
_shared_sqlite_saver: Optional[SqliteSaver] = None
_shared_db_path: Optional[str] = None

logger = logging.getLogger(__name__)


class AsyncCheckpointSaverWrapper(BaseCheckpointSaver):
    """Expose sync checkpoint savers through both sync and async interfaces.

    This keeps sync and async agent paths on the same underlying saver instance,
    avoiding serialization differences between separate sync/async savers.
    """

    def __init__(self, sync_saver: Any, backend_label: str):
        super().__init__()
        self._saver = sync_saver
        self._backend_label = backend_label

    async def aget_tuple(self, config):
        logger.debug(f"[CHECKPOINT] {self._backend_label}Wrapper.aget_tuple config={config}")
        try:
            result = await asyncio.to_thread(self._saver.get_tuple, config)
            logger.debug(f"[CHECKPOINT] aget_tuple result: {type(result).__name__}, has_checkpoint={result is not None}")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] aget_tuple ERROR: {e}", exc_info=True)
            raise

    async def alist(self, config, *, filter=None, before=None, limit=None):
        logger.debug(f"[CHECKPOINT] {self._backend_label}Wrapper.alist called")
        return await asyncio.to_thread(
            self._saver.list, config, filter=filter, before=before, limit=limit
        )

    async def aput(self, config, checkpoint, metadata, new_versions):
        logger.debug(f"[CHECKPOINT] {self._backend_label}Wrapper.aput new_versions={new_versions}")
        try:
            result = await asyncio.to_thread(
                self._saver.put, config, checkpoint, metadata, new_versions
            )
            logger.debug("[CHECKPOINT] aput SUCCESS")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] aput ERROR: {e}", exc_info=True)
            raise

    async def aput_writes(self, config, writes, task_id):
        logger.debug(f"[CHECKPOINT] {self._backend_label}Wrapper.aput_writes task_id={task_id}")
        return await asyncio.to_thread(
            self._saver.put_writes, config, writes, task_id
        )

    def get_tuple(self, config):
        logger.debug(f"[CHECKPOINT] {self._backend_label}Wrapper.get_tuple config={config}")
        try:
            result = self._saver.get_tuple(config)
            logger.debug(f"[CHECKPOINT] get_tuple result: {type(result).__name__}, has_checkpoint={result is not None}")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] get_tuple ERROR: {e}", exc_info=True)
            raise

    def list(self, config, *, filter=None, before=None, limit=None):
        return self._saver.list(config, filter=filter, before=before, limit=limit)

    def put(self, config, checkpoint, metadata, new_versions):
        logger.debug(f"[CHECKPOINT] {self._backend_label}Wrapper.put new_versions={new_versions}")
        try:
            result = self._saver.put(config, checkpoint, metadata, new_versions)
            logger.debug("[CHECKPOINT] put SUCCESS")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] put ERROR: {e}", exc_info=True)
            raise

    def put_writes(self, config, writes, task_id):
        return self._saver.put_writes(config, writes, task_id)


# Global async wrapper instance (wraps the shared SqliteSaver)
_async_sqlite_wrapper: Optional[AsyncCheckpointSaverWrapper] = None


def _init_sqlite_db(db_path: str) -> None:
    """Initialize SQLite database with WAL mode (one-time setup)."""
    global _shared_db_path
    if _shared_db_path == db_path:
        return  # Already initialized

    # Set up WAL mode using a temporary connection
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.close()
    _shared_db_path = db_path
    logger.info(f"Initialized SQLite database with WAL mode: {db_path}")


def _get_shared_sqlite_saver(db_path: str) -> SqliteSaver:
    """Get or create shared SqliteSaver for sync operations."""
    global _shared_sqlite_conn, _shared_sqlite_saver

    # Ensure DB is initialized with WAL mode
    _init_sqlite_db(db_path)

    if _shared_sqlite_saver is None:
        # Connect with multi-thread support
        _shared_sqlite_conn = sqlite3.connect(
            db_path,
            check_same_thread=False,  # Required for multi-threaded access
        )
        _shared_sqlite_saver = SqliteSaver(_shared_sqlite_conn)
        # Setup creates the required tables
        _shared_sqlite_saver.setup()
        logger.info(f"[CHECKPOINT] Created SqliteSaver id={id(_shared_sqlite_saver)} for {db_path}")
    else:
        logger.debug(f"[CHECKPOINT] Reusing SqliteSaver id={id(_shared_sqlite_saver)}")

    return _shared_sqlite_saver


def _get_async_sqlite_wrapper(db_path: str) -> AsyncCheckpointSaverWrapper:
    """Get or create async wrapper around the shared SqliteSaver.

    This ensures both sync and async paths use the SAME SqliteSaver,
    avoiding serialization format incompatibilities.
    """
    global _async_sqlite_wrapper

    # Get the shared sync saver (creates it if needed)
    sync_saver = _get_shared_sqlite_saver(db_path)

    if _async_sqlite_wrapper is None:
        _async_sqlite_wrapper = AsyncCheckpointSaverWrapper(sync_saver, "SQLite")
        logger.info(f"[CHECKPOINT] Created AsyncWrapper id={id(_async_sqlite_wrapper)} wrapping SqliteSaver id={id(sync_saver)}")
    else:
        logger.debug(f"[CHECKPOINT] Reusing AsyncWrapper id={id(_async_sqlite_wrapper)}")

    return _async_sqlite_wrapper


def close_checkpointer_connections():
    """Close database connections on shutdown."""
    global _shared_sqlite_conn, _shared_sqlite_saver, _async_sqlite_wrapper

    if _shared_sqlite_conn:
        try:
            _shared_sqlite_conn.close()
            logger.info("Closed shared SqliteSaver connection")
        except Exception as e:
            logger.warning(f"Error closing SqliteSaver connection: {e}")
        _shared_sqlite_conn = None
        _shared_sqlite_saver = None

    # Async wrapper just references the sync saver, no separate cleanup needed
    _async_sqlite_wrapper = None


# Register cleanup for graceful shutdown
atexit.register(close_checkpointer_connections)


def create_checkpointer(config: CheckpointerConfig) -> Optional[BaseCheckpointSaver]:
    """
    Create a checkpointer based on configuration.

    Args:
        config: CheckpointerConfig with backend settings

    Returns:
        Checkpointer instance or None
    """
    logger.info(f"[CHECKPOINT] create_checkpointer backend={config.backend}")

    if config.custom_checkpointer is not None:
        logger.info(f"[CHECKPOINT] Using custom: {type(config.custom_checkpointer).__name__}")
        return config.custom_checkpointer

    if config.backend == "memory":
        logger.info("[CHECKPOINT] Creating MemorySaver")
        return MemorySaver()

    elif config.backend in ("sqlite", "sqlite_async"):
        # Use AsyncCheckpointSaverWrapper for BOTH sync and async paths
        # This ensures consistent serialization/deserialization of checkpoint versions
        # The wrapper supports both sync methods (get_tuple, put) and async methods (aget_tuple, aput)
        if not config.sqlite_path:
            raise ValueError("SQLite backend requires sqlite_path")
        wrapper = _get_async_sqlite_wrapper(config.sqlite_path)
        logger.info(f"[CHECKPOINT] Returning AsyncWrapper id={id(wrapper)} for {config.backend} (wraps SqliteSaver id={id(wrapper._saver)})")
        return wrapper

    elif config.backend == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
            import psycopg
            if not config.postgres_uri:
                raise ValueError("Postgres backend requires postgres_uri")

            # Create connection with autocommit=True for all operations
            # PostgresSaver doesn't manage commits internally, so autocommit ensures
            # each operation is immediately persisted
            conn = psycopg.connect(config.postgres_uri, autocommit=True)
            sync_saver = PostgresSaver(conn)
            # Setup tables (requires autocommit for CREATE INDEX CONCURRENTLY)
            sync_saver.setup()
            logger.info("[CHECKPOINT] PostgreSQL tables setup complete")

            # Keep autocommit=True so all writes are immediately committed
            # (PostgresSaver doesn't handle transaction commits internally)

            # Wrap in async-compatible wrapper
            wrapper = AsyncCheckpointSaverWrapper(sync_saver, "Postgres")
            logger.info(f"[CHECKPOINT] Created AsyncCheckpointSaverWrapper for {config.postgres_uri.split('@')[-1] if '@' in config.postgres_uri else 'postgres'}")
            return wrapper
        except ImportError as e:
            raise ImportError(
                f"Postgres checkpointer requires additional packages. "
                f"From the Nymeria directory, install with: "
                f"pip install -r requirements-postgres.txt. "
                f"Error: {e}"
            )

    elif config.backend == "custom":
        if config.custom_checkpointer is None:
            raise ValueError("Custom backend requires custom_checkpointer to be set")
        return config.custom_checkpointer

    else:
        raise ValueError(f"Unknown checkpointer backend: {config.backend}")


def create_graph(
    config: Optional[AgentConfig] = None,
    tools: Optional[List[BaseTool]] = None,
    checkpointer: Optional[BaseCheckpointSaver] = None,
    *,
    dynamic_tool_resolver: Optional[Callable[[], tuple]] = None,
    superset_tools: Optional[List[BaseTool]] = None,
) -> Any:
    """
    Build and compile the ReAct agent graph.

    This is the main entry point for creating an agent graph.
    Frameworks should use this function with custom config.

    Args:
        config: AgentConfig with all settings. Defaults to default_config.
        tools: List of tools. Defaults to no tools. In dynamic mode this is
            ignored by the agent node (the resolver supplies tools per-step)
            but still serves as the static fallback when ``superset_tools``
            is not given.
        checkpointer: Override checkpointer (ignores config.checkpointer)
        dynamic_tool_resolver: Optional ``() -> (tools, hash)`` callable.
            When provided, the agent node uses ``create_dynamic_agent_node``
            and rebinds the LLM per-step on hash change.
        superset_tools: Optional superset for ``ToolNode`` execution dispatch
            when in dynamic mode. Must include every tool the resolver may
            ever return; otherwise mid-turn enables of newly-registered tools
            must fall back to the rebuild path.

    Returns:
        A compiled LangGraph ready to be invoked.

    Usage:
        # Graph with no tools
        graph = create_graph()

        # Custom configuration
        config = AgentConfig(
            llm=LLMConfig(provider="anthropic", model="claude-sonnet-4-6"),
            system_prompt="You are a helpful coding assistant.",
        )
        my_tools = [search_tool, calculate_tool]
        graph = create_graph(config=config, tools=my_tools)

        # Dynamic binding
        graph = create_graph(
            config=config,
            tools=superset,
            dynamic_tool_resolver=resolver,
            superset_tools=superset,
        )

        # Invoke the graph
        result = graph.invoke(
            {"messages": [HumanMessage(content="Hello")]},
            config={"configurable": {"thread_id": "user-123"}}
        )
    """
    config = config or default_config
    tools = tools if tools is not None else []

    # Create nodes using the factory
    factory = NodeFactory(
        config,
        tools,
        dynamic_tool_resolver=dynamic_tool_resolver,
        superset_tools=superset_tools,
    )

    # Build the graph
    graph = StateGraph(AgentState)

    # Add nodes
    graph.add_node("agent", factory.create_agent_node())
    graph.add_node("tools", factory.create_tools_node())

    # Set entry point
    graph.set_entry_point("agent")

    # Add edges
    graph.add_conditional_edges(
        "agent",
        factory.create_router(),
        {"tools": "tools", "end": END}
    )
    graph.add_conditional_edges(
        "tools",
        factory.create_tools_router(),
        {"agent": "agent", "end": END}
    )

    # Create or use provided checkpointer
    if checkpointer is None:
        checkpointer = create_checkpointer(config.checkpointer)

    logger.debug(f"[CHECKPOINT] Compiling graph with {type(checkpointer).__name__} id={id(checkpointer)}")

    # Compile with recursion limit
    compiled = graph.compile(
        checkpointer=checkpointer,
    )
    logger.debug(f"[CHECKPOINT] Graph compiled, checkpointer={type(compiled.checkpointer).__name__ if compiled.checkpointer else 'None'}")
    return compiled
