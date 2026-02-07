"""Memory tools for Nymeria - save and manage user memories.

Memories are automatically injected into the system prompt, so Nymeria
always knows them without needing to call a recall tool.
"""

import logging
from pathlib import Path
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.user_profile import UserProfileManager
from ..core.memory_index import MemoryIndex
from .utils import get_user_id

logger = logging.getLogger(__name__)

# Global profile manager instance (initialized lazily)
_profile_manager: Optional[UserProfileManager] = None


def _get_profile_manager() -> UserProfileManager:
    """Get or create the global profile manager."""
    global _profile_manager
    if _profile_manager is None:
        # Default to standard data directory
        from ..config import get_settings
        settings = get_settings()
        _profile_manager = UserProfileManager(settings.data_dir)
    return _profile_manager


def _get_memory_index(user_id: str) -> Optional[MemoryIndex]:
    """Get memory index for a user if RAG is enabled."""
    manager = _get_profile_manager()
    profile = manager.get_profile(user_id)

    if not profile.opt_in.rag_enabled:
        return None

    try:
        from ..config import get_settings
        settings = get_settings()

        # Sanitize user_id for path safety
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_")
        if not safe_user_id:
            safe_user_id = "default"

        db_path = settings.data_dir / "users" / safe_user_id / "memory.db"
        return MemoryIndex(db_path)
    except Exception as e:
        logger.warning(f"Failed to get memory index for user {user_id}: {e}")
        return None


@tool
def memory_save(
    key: str,
    value: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Save a memory about the user. Auto-injected into future conversations.

    Args:
        key: Category identifier (e.g., "user_name", "occupation")
        value: Information to remember (max 1000 chars)
    """
    logger.info(f"memory_save called: key={key}")

    user_id = get_user_id(config)
    manager = _get_profile_manager()

    # Use atomic update to prevent race conditions
    with manager.atomic_update(user_id) as profile:
        success = profile.add_memory(key, value)
        if success:
            logger.info(f"Memory saved for user {user_id}: {key}={value[:50]}")

            # Also index in RAG for semantic search
            memory_index = _get_memory_index(user_id)
            if memory_index:
                try:
                    memory_index.add_chunk(
                        content=f"{key}: {value}",
                        metadata={"key": key},
                        chunk_type="memory",
                        user_id=user_id,
                    )
                except Exception as e:
                    logger.warning(f"Failed to index memory in RAG: {e}")

            return f"[Saved]: I'll remember '{key}'. This will be available in all future conversations."
        else:
            return f"[Error]: Memory limit reached ({profile.MAX_MEMORIES} memories). Use memory_forget to remove old ones first."


@tool
def memory_forget(
    key: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Remove a memory by key.

    Args:
        key: The memory key to delete
    """
    logger.info(f"memory_forget called: key={key}")

    user_id = get_user_id(config)
    manager = _get_profile_manager()

    # Use atomic update to prevent race conditions
    with manager.atomic_update(user_id) as profile:
        success = profile.remove_memory(key)
        if success:
            logger.info(f"Memory deleted for user {user_id}: {key}")
            return f"[Deleted]: Forgot '{key}'. This will no longer appear in future conversations."
        else:
            # List available keys to help
            keys = profile.list_memory_keys()
            if keys:
                return f"[Error]: No memory found with key '{key}'. Available keys: {', '.join(keys)}"
            return f"[Error]: No memories stored yet."


@tool
def memory_list(
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    List all memories currently stored for this user.

    Use this to show the user what you remember about them.

    Returns:
        List of all stored memories with their values
    """
    logger.info("memory_list called")

    user_id = get_user_id(config)
    manager = _get_profile_manager()
    profile = manager.get_profile(user_id)

    if not profile.memories:
        return "[Info]: No memories stored yet. Use memory_save to remember things about the user."

    lines = [f"Stored memories ({len(profile.memories)} total):"]
    for mem in sorted(profile.memories, key=lambda m: m.key):
        lines.append(f"- {mem.key}: {mem.value}")

    if profile.personality_overrides:
        lines.append("\nPersonality preferences:")
        for trait, value in profile.personality_overrides.items():
            lines.append(f"- {trait}: {value}")

    return "\n".join(lines)


@tool
def memory_clear_all(
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Clear ALL memories for this user.

    Use this when the user explicitly asks you to forget everything about them.
    This is irreversible - all memories and personality preferences will be deleted.

    Returns:
        Confirmation message
    """
    logger.info("memory_clear_all called")

    user_id = get_user_id(config)
    manager = _get_profile_manager()

    # Use atomic update to prevent race conditions
    with manager.atomic_update(user_id) as profile:
        count = len(profile.memories)
        personality_count = len(profile.personality_overrides)

        profile.memories = []
        profile.personality_overrides = {}
        profile.name = None

        logger.info(f"All memories cleared for user {user_id}")
        return f"[Cleared]: Deleted {count} memories and {personality_count} personality preferences. Starting fresh."


@tool
def personality_set(
    trait: str,
    value: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Set a communication preference. Auto-applied in future conversations.

    Args:
        trait: Preference category (e.g., "tone", "verbosity")
        value: Desired behavior
    """
    logger.info(f"personality_set called: trait={trait}, value={value}")

    user_id = get_user_id(config)
    manager = _get_profile_manager()

    # Use atomic update to prevent race conditions
    with manager.atomic_update(user_id) as profile:
        profile.set_personality(trait, value)
        logger.info(f"Personality set for user {user_id}: {trait}={value}")
        return f"[Set]: I'll remember to '{value}' in future conversations."


@tool
def rag_search(
    query: str,
    max_results: int = 5,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Search past conversations and memories for relevant context.

    Args:
        query: What to search for
        max_results: Max results (1-10, default 5)
    """
    logger.info(f"rag_search called: query={query[:50]}...")

    user_id = get_user_id(config)
    manager = _get_profile_manager()
    profile = manager.get_profile(user_id)

    # Check if RAG is enabled
    if not profile.opt_in.rag_enabled:
        return (
            "[RAG Disabled]: RAG is not enabled for this user. "
            "Use rag_settings(enabled=True) to enable it first."
        )

    # Get memory index
    memory_index = _get_memory_index(user_id)
    if not memory_index:
        return "[Error]: Could not access memory index."

    try:
        # Get RAG preferences
        rag_prefs = profile.get_rag_preferences()

        # Build chunk types filter based on preferences
        chunk_types = []
        if rag_prefs.get("include_conversations", True):
            chunk_types.append("conversation")
        if rag_prefs.get("include_memories", True):
            chunk_types.append("memory")
        if rag_prefs.get("include_todos", True):
            chunk_types.append("todo")

        if not chunk_types:
            return "[Info]: All content types are disabled in RAG settings."

        # Clamp max_results
        max_results = max(1, min(10, max_results))

        # Search
        results = memory_index.search(
            query=query,
            user_id=user_id,
            limit=max_results,
            chunk_types=chunk_types,
        )

        if not results:
            return f"[No Results]: No relevant context found for '{query}'."

        # Format results
        lines = [f"Found {len(results)} relevant result(s) for '{query}':\n"]

        for i, result in enumerate(results, 1):
            type_emoji = {
                'conversation': '💬',
                'memory': '🧠',
                'todo': '✅',
            }.get(result.chunk_type, '📝')

            # Truncate long content
            content = result.content
            if len(content) > 400:
                content = content[:397] + "..."

            lines.append(f"{i}. {type_emoji} [{result.chunk_type}] (relevance: {result.score:.2f})")
            lines.append(f"   {content}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"RAG search failed: {e}")
        return f"[Error]: Search failed - {str(e)}"


@tool
def rag_settings(
    enabled: Optional[bool] = None,
    max_chunks: Optional[int] = None,
    include_conversations: Optional[bool] = None,
    include_memories: Optional[bool] = None,
    include_todos: Optional[bool] = None,
    auto_flush: Optional[bool] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Configure RAG settings. Returns current settings after changes.

    Args:
        enabled: Turn RAG on/off
        max_chunks: Context chunks per message (1-10)
        include_conversations: Include past conversations
        include_memories: Include saved memories
        include_todos: Include completed TODOs
        auto_flush: Preserve context before window trims
    """
    logger.info(f"rag_settings called")

    user_id = get_user_id(config)
    manager = _get_profile_manager()

    with manager.atomic_update(user_id) as profile:
        # Apply changes
        if enabled is not None:
            profile.opt_in.rag_enabled = enabled
            logger.info(f"RAG {'enabled' if enabled else 'disabled'} for user {user_id}")

        if max_chunks is not None:
            # Clamp to valid range
            max_chunks = max(1, min(10, max_chunks))
            profile.set_rag_preference("max_chunks", max_chunks)

        if include_conversations is not None:
            profile.set_rag_preference("include_conversations", include_conversations)

        if include_memories is not None:
            profile.set_rag_preference("include_memories", include_memories)

        if include_todos is not None:
            profile.set_rag_preference("include_todos", include_todos)

        if auto_flush is not None:
            profile.set_rag_preference("auto_flush", auto_flush)

        # Build status response
        rag_prefs = profile.get_rag_preferences()
        status = "enabled" if profile.opt_in.rag_enabled else "disabled"

        lines = [
            f"RAG Settings (currently {status}):",
            f"- enabled: {profile.opt_in.rag_enabled}",
            f"- max_chunks: {rag_prefs.get('max_chunks', 5)}",
            f"- include_conversations: {rag_prefs.get('include_conversations', True)}",
            f"- include_memories: {rag_prefs.get('include_memories', True)}",
            f"- include_todos: {rag_prefs.get('include_todos', True)}",
            f"- auto_flush: {rag_prefs.get('auto_flush', True)}",
        ]

        if profile.opt_in.rag_enabled:
            # Get stats if RAG is enabled
            memory_index = _get_memory_index(user_id)
            if memory_index:
                stats = memory_index.get_stats(user_id)
                lines.append(f"\nIndex stats:")
                lines.append(f"- Total chunks: {stats.get('total_chunks', 0)}")
                for chunk_type, count in stats.get('by_type', {}).items():
                    lines.append(f"  - {chunk_type}: {count}")

        return "\n".join(lines)


# Export memory tools
# memory_list removed - memories are auto-injected into system prompt
# rag_settings removed - should be configured via UI settings
MEMORY_TOOLS = [
    memory_save,
    memory_forget,
    memory_clear_all,
    personality_set,
    rag_search,
]
