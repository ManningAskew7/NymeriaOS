"""
The ReAct Agent Graph

Modular graph construction that accepts configuration for framework integration.
Supports multiple checkpointer backends and custom tools.
"""

import atexit
import logging
import sqlite3
from typing import List, Optional, Any, Tuple
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from .state import AgentState
from .config import AgentConfig, CheckpointerConfig, default_config
from .nodes import NodeFactory, simple_should_continue
from .tools import TOOLS as DEFAULT_TOOLS

# Shared savers for consistent state + persistence
_shared_sqlite_conn: Optional[sqlite3.Connection] = None
_shared_sqlite_saver: Optional[SqliteSaver] = None
_shared_db_path: Optional[str] = None

logger = logging.getLogger(__name__)


class AsyncSqliteSaverWrapper(BaseCheckpointSaver):
    """Wrapper that makes SqliteSaver async-compatible by running sync methods in executor.

    This ensures both sync and async paths use the SAME SqliteSaver instance,
    avoiding serialization format incompatibilities between SqliteSaver and AsyncSqliteSaver.
    """

    def __init__(self, sync_saver: SqliteSaver):
        super().__init__()
        self._saver = sync_saver

    # Async methods - run sync methods via thread executor
    async def aget_tuple(self, config):
        import asyncio
        logger.info(f"[CHECKPOINT] AsyncWrapper.aget_tuple config={config}")
        try:
            result = await asyncio.to_thread(self._saver.get_tuple, config)
            logger.info(f"[CHECKPOINT] aget_tuple result: {type(result).__name__}, has_checkpoint={result is not None}")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] aget_tuple ERROR: {e}", exc_info=True)
            raise

    async def alist(self, config, *, filter=None, before=None, limit=None):
        import asyncio
        logger.info(f"[CHECKPOINT] AsyncWrapper.alist called")
        return await asyncio.to_thread(
            self._saver.list, config, filter=filter, before=before, limit=limit
        )

    async def aput(self, config, checkpoint, metadata, new_versions):
        import asyncio
        logger.info(f"[CHECKPOINT] AsyncWrapper.aput new_versions={new_versions}")
        try:
            result = await asyncio.to_thread(
                self._saver.put, config, checkpoint, metadata, new_versions
            )
            logger.info(f"[CHECKPOINT] aput SUCCESS")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] aput ERROR: {e}", exc_info=True)
            raise

    async def aput_writes(self, config, writes, task_id):
        import asyncio
        logger.info(f"[CHECKPOINT] AsyncWrapper.aput_writes task_id={task_id}")
        return await asyncio.to_thread(
            self._saver.put_writes, config, writes, task_id
        )

    # Sync methods - forward directly to underlying saver
    def get_tuple(self, config):
        logger.info(f"[CHECKPOINT] SyncWrapper.get_tuple config={config}")
        try:
            result = self._saver.get_tuple(config)
            logger.info(f"[CHECKPOINT] get_tuple result: {type(result).__name__}, has_checkpoint={result is not None}")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] get_tuple ERROR: {e}", exc_info=True)
            raise

    def list(self, config, *, filter=None, before=None, limit=None):
        return self._saver.list(config, filter=filter, before=before, limit=limit)

    def put(self, config, checkpoint, metadata, new_versions):
        logger.info(f"[CHECKPOINT] SyncWrapper.put new_versions={new_versions}")
        try:
            result = self._saver.put(config, checkpoint, metadata, new_versions)
            logger.info(f"[CHECKPOINT] put SUCCESS")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] put ERROR: {e}", exc_info=True)
            raise

    def put_writes(self, config, writes, task_id):
        return self._saver.put_writes(config, writes, task_id)


class AsyncPostgresSaverWrapper(BaseCheckpointSaver):
    """Wrapper that makes PostgresSaver async-compatible by running sync methods in executor.

    This avoids needing an event loop at initialization time while still supporting
    async streaming operations.
    """

    def __init__(self, sync_saver):
        super().__init__()
        self._saver = sync_saver

    # Async methods - run sync methods via thread executor
    async def aget_tuple(self, config):
        import asyncio
        logger.info(f"[CHECKPOINT] PostgresWrapper.aget_tuple config={config}")
        try:
            result = await asyncio.to_thread(self._saver.get_tuple, config)
            logger.info(f"[CHECKPOINT] aget_tuple result: {type(result).__name__}, has_checkpoint={result is not None}")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] aget_tuple ERROR: {e}", exc_info=True)
            raise

    async def alist(self, config, *, filter=None, before=None, limit=None):
        import asyncio
        return await asyncio.to_thread(
            self._saver.list, config, filter=filter, before=before, limit=limit
        )

    async def aput(self, config, checkpoint, metadata, new_versions):
        import asyncio
        logger.info(f"[CHECKPOINT] PostgresWrapper.aput new_versions={new_versions}")
        try:
            result = await asyncio.to_thread(
                self._saver.put, config, checkpoint, metadata, new_versions
            )
            logger.info(f"[CHECKPOINT] aput SUCCESS")
            return result
        except Exception as e:
            logger.error(f"[CHECKPOINT] aput ERROR: {e}", exc_info=True)
            raise

    async def aput_writes(self, config, writes, task_id):
        import asyncio
        return await asyncio.to_thread(
            self._saver.put_writes, config, writes, task_id
        )

    # Sync methods - forward directly to underlying saver
    def get_tuple(self, config):
        return self._saver.get_tuple(config)

    def list(self, config, *, filter=None, before=None, limit=None):
        return self._saver.list(config, filter=filter, before=before, limit=limit)

    def put(self, config, checkpoint, metadata, new_versions):
        return self._saver.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id):
        return self._saver.put_writes(config, writes, task_id)


# Global async wrapper instance (wraps the shared SqliteSaver)
_async_sqlite_wrapper: Optional[AsyncSqliteSaverWrapper] = None


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
        logger.info(f"[CHECKPOINT] Reusing SqliteSaver id={id(_shared_sqlite_saver)}")

    return _shared_sqlite_saver


def _get_async_sqlite_wrapper(db_path: str) -> AsyncSqliteSaverWrapper:
    """Get or create async wrapper around the shared SqliteSaver.

    This ensures both sync and async paths use the SAME SqliteSaver,
    avoiding serialization format incompatibilities.
    """
    global _async_sqlite_wrapper

    # Get the shared sync saver (creates it if needed)
    sync_saver = _get_shared_sqlite_saver(db_path)

    if _async_sqlite_wrapper is None:
        _async_sqlite_wrapper = AsyncSqliteSaverWrapper(sync_saver)
        logger.info(f"[CHECKPOINT] Created AsyncWrapper id={id(_async_sqlite_wrapper)} wrapping SqliteSaver id={id(sync_saver)}")
    else:
        logger.info(f"[CHECKPOINT] Reusing AsyncWrapper id={id(_async_sqlite_wrapper)}")

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
        # Use AsyncSqliteSaverWrapper for BOTH sync and async paths
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
            wrapper = AsyncPostgresSaverWrapper(sync_saver)
            logger.info(f"[CHECKPOINT] Created AsyncPostgresSaverWrapper for {config.postgres_uri.split('@')[-1] if '@' in config.postgres_uri else 'postgres'}")
            return wrapper
        except ImportError as e:
            raise ImportError(
                f"Postgres checkpointer requires additional packages. "
                f"Install with: pip install langgraph-checkpoint-postgres psycopg[binary]. "
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
) -> Any:
    """
    Build and compile the ReAct agent graph.

    This is the main entry point for creating an agent graph.
    Frameworks should use this function with custom config.

    Args:
        config: AgentConfig with all settings. Defaults to default_config.
        tools: List of tools. Defaults to DEFAULT_TOOLS.
        checkpointer: Override checkpointer (ignores config.checkpointer)

    Returns:
        A compiled LangGraph ready to be invoked.

    Usage:
        # Default graph
        graph = create_graph()

        # Custom configuration
        config = AgentConfig(
            llm=LLMConfig(provider="anthropic", model="claude-3-5-sonnet"),
            system_prompt="You are a helpful coding assistant.",
        )
        my_tools = [search_tool, calculate_tool]
        graph = create_graph(config=config, tools=my_tools)

        # Invoke the graph
        result = graph.invoke(
            {"messages": [HumanMessage(content="Hello")]},
            config={"configurable": {"thread_id": "user-123"}}
        )
    """
    config = config or default_config
    tools = tools if tools is not None else DEFAULT_TOOLS

    # Create nodes using the factory
    factory = NodeFactory(config, tools)

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

    logger.info(f"[CHECKPOINT] Compiling graph with {type(checkpointer).__name__} id={id(checkpointer)}")

    # Compile with recursion limit
    compiled = graph.compile(
        checkpointer=checkpointer,
    )
    logger.info(f"[CHECKPOINT] Graph compiled, checkpointer={type(compiled.checkpointer).__name__ if compiled.checkpointer else 'None'}")
    return compiled


def get_graph_with_memory(
    config: Optional[AgentConfig] = None,
    tools: Optional[List[BaseTool]] = None,
) -> Any:
    """
    Create a graph with in-memory persistence.

    Convenience function for development and testing.
    For production, use create_graph with sqlite or postgres checkpointer.

    Args:
        config: Optional AgentConfig
        tools: Optional tool list

    Returns:
        Compiled graph with MemorySaver
    """
    return create_graph(
        config=config,
        tools=tools,
        checkpointer=MemorySaver()
    )


class ReactAgent:
    """
    High-level wrapper around the ReAct graph.

    Provides a cleaner interface for framework integration.

    Usage:
        agent = ReactAgent(config=my_config, tools=my_tools)
        response = agent.chat("Hello!", thread_id="user-123")
        response = agent.chat("What's 2+2?", thread_id="user-123")
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        tools: Optional[List[BaseTool]] = None,
        checkpointer: Optional[BaseCheckpointSaver] = None,
    ):
        self.config = config or default_config
        self.tools = tools if tools is not None else DEFAULT_TOOLS
        self._graph = create_graph(
            config=self.config,
            tools=self.tools,
            checkpointer=checkpointer
        )

    @property
    def graph(self):
        """Access the underlying LangGraph."""
        return self._graph

    def chat(
        self,
        message: str,
        thread_id: str = "default",
        **kwargs
    ) -> str:
        """
        Send a message and get a response.

        Args:
            message: User message
            thread_id: Conversation thread ID for persistence
            **kwargs: Additional config passed to graph.invoke

        Returns:
            Agent's response text

        Raises:
            ValueError: If message is empty or whitespace only
        """
        from langchain_core.messages import HumanMessage

        # Input validation
        if not message or not message.strip():
            raise ValueError("Message cannot be empty")

        result = self._graph.invoke(
            {"messages": [HumanMessage(content=message)]},
            config={"configurable": {"thread_id": thread_id}, **kwargs}
        )

        # Extract the last AI message
        messages = result.get("messages", [])
        for msg in reversed(messages):
            if hasattr(msg, "content") and msg.content:
                return msg.content

        return ""

    def stream(
        self,
        message: str,
        thread_id: str = "default",
        **kwargs
    ):
        """
        Stream a response.

        Args:
            message: User message
            thread_id: Conversation thread ID
            **kwargs: Additional config

        Yields:
            State updates as they occur
        """
        from langchain_core.messages import HumanMessage

        for chunk in self._graph.stream(
            {"messages": [HumanMessage(content=message)]},
            config={"configurable": {"thread_id": thread_id}, **kwargs}
        ):
            yield chunk


# === DEFAULT GRAPH INSTANCE ===
# Lazy-initialized to avoid crashing on import when env vars aren't set.
graph = None


def _get_default_graph():
    """Lazily initialize the default graph on first use."""
    global graph
    if graph is None:
        graph = get_graph_with_memory()
    return graph
