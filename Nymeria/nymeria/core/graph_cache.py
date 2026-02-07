"""Graph caching for Nymeria agent.

Provides caching for user-specific LangGraph execution graphs based on
memory/TODO content hashes.
"""

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

if TYPE_CHECKING:
    from .user_profile import UserProfileManager
    from .todo_manager import TodoManager

logger = logging.getLogger(__name__)


class GraphCache:
    """
    Cache for user-specific LangGraph execution graphs.

    Maintains separate caches for sync and async graphs, rebuilding when
    user memories or TODOs change.
    """

    def __init__(
        self,
        profile_manager: "UserProfileManager",
        todo_manager: "TodoManager",
        build_graph_fn: Callable[[str], Any],
        build_async_graph_fn: Callable[[str], Any],
        default_graph: Any,
        default_async_graph: Any,
    ):
        """
        Initialize the graph cache.

        Args:
            profile_manager: User profile manager for memory access
            todo_manager: TODO manager for active TODOs
            build_graph_fn: Function to build a sync graph with a system prompt
            build_async_graph_fn: Function to build an async graph with a system prompt
            default_graph: Default sync graph (no user memories/TODOs)
            default_async_graph: Default async graph (no user memories/TODOs)
        """
        self._profile_manager = profile_manager
        self._todo_manager = todo_manager
        self._build_graph = build_graph_fn
        self._build_async_graph = build_async_graph_fn
        self._default_graph = default_graph
        self._default_async_graph = default_async_graph

        # Cache: user_id -> (memory_hash, graph)
        self._sync_cache: Dict[str, Tuple[str, Any]] = {}
        self._async_cache: Dict[str, Tuple[str, Any]] = {}

    def _get_memory_hash(self, user_id: str) -> str:
        """Get a hash of the user's memories and TODOs to detect changes."""
        profile = self._profile_manager.get_profile(user_id)
        # Simple hash based on memory keys and values
        memory_str = "|".join(f"{m.key}:{m.value}" for m in profile.memories)
        personality_str = "|".join(f"{k}:{v}" for k, v in profile.personality_overrides.items())

        # Include TODOs in the hash
        todo_list = self._todo_manager.get_todos(user_id)
        active_todos = todo_list.get_active_todos()
        todo_str = "|".join(f"{t.id}:{t.status.value}:{t.task[:50]}" for t in active_todos)

        return f"{hash(memory_str + personality_str + todo_str)}"

    def _has_user_context(self, user_id: str) -> bool:
        """Check if user has any memories or active TODOs."""
        profile = self._profile_manager.get_profile(user_id)
        todo_list = self._todo_manager.get_todos(user_id)
        has_memories = profile.memories or profile.personality_overrides
        has_todos = bool(todo_list.get_active_todos())
        return has_memories or has_todos

    def get_sync_graph(
        self,
        user_id: str,
        build_prompt_fn: Callable[[str, bool], str],
        is_autonomous: bool = False,
    ) -> Any:
        """
        Get the appropriate sync graph for a user, rebuilding if context changed.

        Args:
            user_id: User identifier
            build_prompt_fn: Function to build full system prompt (user_id, is_autonomous) -> str
            is_autonomous: If True, include autonomous execution instructions

        Returns:
            LangGraph compiled graph
        """
        memory_hash = self._get_memory_hash(user_id)

        # For autonomous mode, always build fresh to include the autonomous prompt
        if is_autonomous:
            logger.debug(f"Building autonomous graph for user {user_id}")
            full_prompt = build_prompt_fn(user_id, True)
            return self._build_graph(full_prompt)

        # Check if we have a cached graph with current memories
        if user_id in self._sync_cache:
            cached_hash, cached_graph = self._sync_cache[user_id]
            if cached_hash == memory_hash:
                return cached_graph

        # Check if user has any memories or active TODOs
        if not self._has_user_context(user_id):
            return self._default_graph

        # Build new graph with user's memories and TODOs in system prompt
        logger.debug(f"Building new graph for user {user_id} (context changed)")
        full_prompt = build_prompt_fn(user_id, False)
        graph = self._build_graph(full_prompt)

        # Cache it
        self._sync_cache[user_id] = (memory_hash, graph)
        return graph

    def get_async_graph(
        self,
        user_id: str,
        build_prompt_fn: Callable[[str, bool], str],
    ) -> Any:
        """
        Get the appropriate async graph for a user, rebuilding if context changed.

        Args:
            user_id: User identifier
            build_prompt_fn: Function to build full system prompt (user_id, is_autonomous) -> str

        Returns:
            LangGraph compiled graph for async operations
        """
        memory_hash = self._get_memory_hash(user_id)

        # Check if we have a cached async graph with current memories
        if user_id in self._async_cache:
            cached_hash, cached_graph = self._async_cache[user_id]
            if cached_hash == memory_hash:
                return cached_graph

        # Check if user has any memories or active TODOs
        if not self._has_user_context(user_id):
            return self._default_async_graph

        # Build new async graph with user's memories and TODOs in system prompt
        logger.debug(f"Building new async graph for user {user_id} (context changed)")
        full_prompt = build_prompt_fn(user_id, False)
        graph = self._build_async_graph(full_prompt)

        # Cache it
        self._async_cache[user_id] = (memory_hash, graph)
        return graph

    def clear_cache(self) -> None:
        """Clear all cached graphs (use when tools are reloaded)."""
        self._sync_cache.clear()
        self._async_cache.clear()

    def update_default_graphs(self, default_graph: Any, default_async_graph: Any) -> None:
        """Update the default graphs (used after tool reload)."""
        self._default_graph = default_graph
        self._default_async_graph = default_async_graph
