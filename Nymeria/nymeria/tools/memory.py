"""Memory tools for Nymeria — unified CRUD over global profile + per-thread notepad.

The agent sees three primitives that dispatch on ``scope``:

- ``memory_add(scope, key?, content)`` — create or upsert.
- ``memory_edit(scope, key?, find, replace)`` — surgical find/replace within an entry.
- ``memory_read(scope, key?, query?)`` — get one, list all, or substring-filter.

``scope="global"`` operates on key/value entries in the user profile (auto-injected
into every future conversation). ``scope="thread"`` operates on the active thread's
notepad (markdown file that survives context compaction).

Empty content / replace-to-empty triggers deletion at the storage layer:
the profile entry is popped (no zombie blanks in ``memory_read`` output),
and the notepad file is unlinked.

Bulk wipe (``memory_clear_all``), personality preferences (``personality_set``),
and semantic search (``rag_search`` / ``rag_settings``) stay separate — they're
different mental models and warrant lexically distinct tools.
"""

import logging
from datetime import datetime
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.time_utils import ensure_aware_utc, utc_now
from ..core.user_profile import UserProfileManager
from ..core.memory_index import MemoryIndex
from ..core.memory_limits import (
    get_global_memory_char_limit,
    validate_profile_memory_write,
)
from . import thread_notes
from .utils import get_effective_thread_id, get_user_id

logger = logging.getLogger(__name__)

VALID_SCOPES = ("global", "thread")

# Global profile manager instance (initialized lazily)
_profile_manager: Optional[UserProfileManager] = None


def _get_profile_manager() -> UserProfileManager:
    """Get or create the global profile manager."""
    global _profile_manager
    if _profile_manager is None:
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

        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_")
        if not safe_user_id:
            safe_user_id = "default"

        db_path = settings.data_dir / "users" / safe_user_id / "memory.db"
        return MemoryIndex(db_path)
    except Exception as e:
        logger.warning(f"Failed to get memory index for user {user_id}: {e}")
        return None


def _validate_scope(scope: str) -> Optional[str]:
    """Return an error string if scope is invalid, else None."""
    if scope not in VALID_SCOPES:
        return f"[Error]: scope must be one of {VALID_SCOPES}, got '{scope}'."
    return None


def _rag_index_global(user_id: str, key: str, value: str) -> None:
    """Replace a key's chunk in the RAG index. Silently swallows failures."""
    memory_index = _get_memory_index(user_id)
    if not memory_index:
        return
    try:
        memory_index.delete_memory_key(user_id, key)
        memory_index.add_chunk(
            content=f"{key}: {value}",
            metadata={"key": key},
            chunk_type="memory",
            user_id=user_id,
        )
    except Exception as e:
        logger.warning(f"Failed to index memory in RAG: {e}")


def _rag_remove_global(user_id: str, key: str) -> None:
    """Remove a key from the RAG index. Silently swallows failures."""
    memory_index = _get_memory_index(user_id)
    if not memory_index:
        return
    try:
        memory_index.delete_memory_key(user_id, key)
    except Exception as e:
        logger.warning(f"Failed to remove memory from RAG index: {e}")


@tool
def memory_add(
    scope: str,
    content: str,
    key: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Save a memory. Creates a new entry or overwrites an existing one.

    scope="global": persistent fact about the user. Requires `key` (e.g.
        "occupation", "favorite_language"). Auto-injected into every future
        conversation. Examples:
          memory_add(scope="global", key="prefers_typescript", content="Yes")
          memory_add(scope="global", key="timezone", content="Australia/Sydney")

    scope="thread": per-thread notepad text that survives compaction. No `key`
        — there is one notepad per thread. Overwrites the entire notepad.
        Example:
          memory_add(scope="thread", content="Working on auth refactor; deadline Friday.")

    Empty `content` deletes the entry (global) or the notepad (thread).

    Args:
        scope: "global" (user profile) or "thread" (per-thread notepad).
        content: The memory text. Empty string deletes.
        key: Required when scope="global". Ignored when scope="thread".

    Returns:
        Global: "[Saved]: I'll remember '<key>'..." or "[Deleted]: Forgot
        '<key>'..." (when content is empty). Thread: "[Saved]: Notepad
        updated (N chars)...". Errors: "[Error]: <reason>".
    """
    err = _validate_scope(scope)
    if err:
        return err

    if scope == "global":
        if not key:
            return "[Error]: scope='global' requires a key (e.g., 'occupation')."

        user_id = get_user_id(config)
        manager = _get_profile_manager()

        if content == "":
            with manager.atomic_update(user_id) as profile:
                if profile.remove_memory(key):
                    logger.info(f"Memory deleted via memory_add(empty content) for user {user_id}: {key}")
                    _rag_remove_global(user_id, key)
                    return f"[Deleted]: Forgot '{key}'. This will no longer appear in future conversations."
                return f"[Info]: No memory with key '{key}' to delete."

        with manager.atomic_update(user_id) as profile:
            stored_content = content[: profile.MAX_VALUE_LENGTH]
            limit_error = validate_profile_memory_write(
                profile,
                key=key,
                value=stored_content,
                limit=get_global_memory_char_limit(),
            )
            if limit_error:
                return limit_error
            ok = profile.add_memory(key, content)
            if not ok:
                return f"[Error]: Memory limit reached ({profile.MAX_MEMORIES} memories). Delete some first."
            logger.info(f"Memory saved for user {user_id}: {key}={stored_content[:50]}")
            _rag_index_global(user_id, key, stored_content)
            return f"[Saved]: I'll remember '{key}'. This will be available in all future conversations."

    # scope == "thread"
    thread_id = get_effective_thread_id(config)
    return thread_notes.write_notepad(thread_id, content, mode="replace")


@tool
def memory_edit(
    scope: str,
    find: str,
    replace: str = "",
    key: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Surgical edit of an existing memory. Find a substring and replace it.

    scope="global": find/replace within a single profile memory's value.
        Requires `key`. If the resulting value is empty, the entry is removed.
        Example:
          memory_edit(scope="global", key="job_title", find="Engineer", replace="Senior Engineer")

    scope="thread": find/replace within the thread notepad's markdown.
        No `key`. Empty `replace` deletes the matched text. If the notepad
        becomes empty, the file is deleted.
        Example:
          memory_edit(scope="thread", find="deadline Friday", replace="deadline Monday")

    Args:
        scope: "global" or "thread".
        find: Exact substring to locate (first occurrence).
        replace: Replacement text. Empty string deletes the matched substring.
        key: Required when scope="global". Ignored when scope="thread".

    Returns:
        Global: "[Saved]: Updated '<key>'" or "[Deleted]: Edit emptied
        '<key>'". Thread: "[Saved]: Text replaced...". Errors: "[Error]:
        <reason>" (key not found, substring not matched).
    """
    err = _validate_scope(scope)
    if err:
        return err

    if scope == "global":
        if not key:
            return "[Error]: scope='global' requires a key."
        if not find:
            return "[Error]: 'find' must be non-empty."

        user_id = get_user_id(config)
        manager = _get_profile_manager()

        with manager.atomic_update(user_id) as profile:
            mem = profile.get_memory(key)
            if not mem:
                return f"[Error]: No memory with key '{key}'."

            if find not in mem.value:
                return f"[Error]: Could not find '{find}' in memory '{key}'."

            updated_value = mem.value.replace(find, replace, 1)

            if updated_value == "":
                profile.remove_memory(key)
                logger.info(f"Memory '{key}' edited to empty -> removed for user {user_id}")
                _rag_remove_global(user_id, key)
                return f"[Deleted]: Edit emptied '{key}'; entry removed."

            stored_value = updated_value[: profile.MAX_VALUE_LENGTH]
            limit_error = validate_profile_memory_write(
                profile,
                key=key,
                value=stored_value,
                limit=get_global_memory_char_limit(),
            )
            if limit_error:
                return limit_error
            profile.add_memory(key, updated_value)
            logger.info(f"Memory '{key}' edited for user {user_id}")
            _rag_index_global(user_id, key, stored_value)
            return f"[Saved]: Updated '{key}'."

    # scope == "thread"
    thread_id = get_effective_thread_id(config)
    return thread_notes.edit_notepad(thread_id, find, replace)


@tool
def memory_read(
    scope: str,
    key: Optional[str] = None,
    query: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Read memory. Get a specific entry, list everything, or substring-filter.

    scope="global": user profile memories.
        - No `key`, no `query`: list all memories + personality preferences.
        - `key` provided: return that one memory's value.
        - `query` provided: list memories whose key or value contains the query
          (substring, case-insensitive). For semantic search use `rag_search`.

    scope="thread": per-thread notepad.
        - No `query`: return full notepad contents (or "[empty]").
        - `query` provided: return only the notepad lines containing the query.

    Args:
        scope: "global" or "thread".
        key: (global only) fetch a single memory by key.
        query: substring filter (both scopes).

    Returns:
        Global: "key: value" for single key; "Stored memories (N shown):"
        + "- key: value" lines for list mode; personality prefs appended
        when no query filter. Thread: raw notepad markdown or matching
        lines. Empty: "[Info]: ..." or "[empty]".
    """
    err = _validate_scope(scope)
    if err:
        return err

    if scope == "global":
        user_id = get_user_id(config)
        manager = _get_profile_manager()
        profile = manager.get_profile(user_id)

        if key:
            mem = profile.get_memory(key)
            if not mem:
                return f"[Info]: No memory with key '{key}'."
            return f"{mem.key}: {mem.value}"

        memories = profile.memories
        if query:
            memories = profile.search_memories(query)

        if not memories and not profile.personality_overrides:
            return "[Info]: No memories stored yet. Use memory_add(scope='global', key=..., content=...) to remember things about the user."

        lines = [f"Stored memories ({len(memories)} shown):"] if memories else []
        for mem in sorted(memories, key=lambda m: m.key):
            lines.append(f"- {mem.key}: {mem.value}")

        if profile.personality_overrides and not query:
            lines.append("\nPersonality preferences:")
            for trait, value in profile.personality_overrides.items():
                lines.append(f"- {trait}: {value}")

        if not lines:
            return f"[Info]: No memories match '{query}'."
        return "\n".join(lines)

    # scope == "thread"
    thread_id = get_effective_thread_id(config)
    content = thread_notes.read_notepad(thread_id)
    if not content:
        return "[empty]"

    if query:
        q_lower = query.lower()
        matched = [line for line in content.splitlines() if q_lower in line.lower()]
        if not matched:
            return f"[Info]: No notepad lines match '{query}'."
        return "\n".join(matched)

    return content


@tool
def memory_clear_all(
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Clear ALL global memories for this user.

    Use this when the user explicitly asks you to forget everything about them.
    This is irreversible — all memories and personality preferences will be deleted.
    Does NOT touch per-thread notepads.

    Returns:
        Confirmation message
    """
    logger.info("memory_clear_all called")

    user_id = get_user_id(config)
    manager = _get_profile_manager()

    with manager.atomic_update(user_id) as profile:
        count = len(profile.memories)
        personality_count = len(profile.personality_overrides)

        profile.memories = []
        profile.personality_overrides = {}
        profile.name = None

        memory_index = _get_memory_index(user_id)
        if memory_index:
            try:
                memory_index.delete_by_type(user_id, "memory")
            except Exception as e:
                logger.warning(f"Failed to clear memories from RAG index: {e}")

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
        trait: Preference category (e.g., "tone", "verbosity", "formality", "humor", "detail_level")
        value: Desired behavior (e.g., "casual and friendly", "concise", "always include code examples")

    Returns:
        "[Set]: I'll remember to '<value>' in future conversations."
    """
    logger.info(f"personality_set called: trait={trait}, value={value}")

    user_id = get_user_id(config)
    manager = _get_profile_manager()

    with manager.atomic_update(user_id) as profile:
        profile.set_personality(trait, value)
        logger.info(f"Personality set for user {user_id}: {trait}={value}")
        return f"[Set]: I'll remember to '{value}' in future conversations."


def _parse_when(value: Optional[str]):
    """Parse an ISO date/datetime string to aware UTC, or None if unparseable."""
    if not value or not value.strip():
        return None
    try:
        return ensure_aware_utc(datetime.fromisoformat(value.strip()))
    except ValueError:
        return None


def _humanize_age(delta_seconds: float) -> str:
    """Render an age (seconds) as a short relative phrase the LLM reads easily."""
    s = int(max(0, delta_seconds))
    if s < 60:
        return "just now"
    m = s // 60
    if m < 60:
        return f"{m}m ago"
    h = m // 60
    if h < 24:
        return f"{h}h ago"
    d = h // 24
    if d < 7:
        return f"{d}d ago"
    if d < 30:
        return f"{d // 7}w ago"
    if d < 365:
        return f"{d // 30}mo ago"
    return f"{d // 365}y ago"


def _thread_title_resolver(user_id: str):
    """Return a cached thread_id -> title lookup using the current agent."""
    cache: dict = {}
    try:
        from ..core.agent import get_current_agent
        agent = get_current_agent()
    except Exception:
        agent = None

    def resolve(thread_id: Optional[str]) -> Optional[str]:
        if not thread_id:
            return None
        if thread_id in cache:
            return cache[thread_id]
        title = None
        try:
            if agent is not None:
                meta = agent.thread_metadata_manager.get_thread(user_id, thread_id)
                if meta and meta.title:
                    title = meta.title
        except Exception:
            title = None
        cache[thread_id] = title
        return title

    return resolve


@tool
def rag_search(
    query: str,
    max_results: int = 5,
    thread_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Search your own memory: past conversations, saved memories, and completed TODOs.

    Results are ranked by hybrid relevance with a gentle recency bias. Each result
    shows when it is from and which thread it came from, so you can reason about
    freshness and, when useful, follow up on the source thread (call it if it is a
    bound callable thread, or open it by id).

    Args:
        query: What to search for.
        max_results: Max results (1-10, default 5).
        thread_id: Restrict the search to a single source thread.
        since: Only chunks at or after this ISO time (e.g. "2026-05-01" or
            "2026-05-01T09:00:00").
        until: Only chunks at or before this ISO time.

    Returns:
        A "now:" anchor header plus numbered entries. Each entry shows
        [chunk_type], relative age + event time, a 0-1 relevance, the source
        thread title + id (when applicable), and a content snippet (truncated to
        the configured budget, default 1000 chars). Near-duplicate results are
        collapsed. "[No Results]: ..." when empty. "[RAG Disabled]: ..." if RAG
        is off. Errors: "[Error]: <reason>".
    """
    logger.info(f"rag_search called: query={query[:50]}...")

    user_id = get_user_id(config)
    manager = _get_profile_manager()
    profile = manager.get_profile(user_id)

    if not profile.opt_in.rag_enabled:
        return (
            "[RAG Disabled]: RAG is not enabled for this user. "
            "Use rag_settings(enabled=True) to enable it first."
        )

    memory_index = _get_memory_index(user_id)
    if not memory_index:
        return "[Error]: Could not access memory index."

    try:
        rag_prefs = profile.get_rag_preferences()

        chunk_types = []
        if rag_prefs.get("include_conversations", True):
            chunk_types.append("conversation")
        if rag_prefs.get("include_memories", False):
            chunk_types.append("memory")
        if rag_prefs.get("include_todos", True):
            chunk_types.append("todo")

        if not chunk_types:
            return "[Info]: All content types are disabled in RAG settings."

        max_results = max(1, min(10, max_results))

        try:
            from ..config import get_settings
            settings = get_settings()
            fusion = settings.rag_fusion_method
            apply_recency = settings.rag_recency_enabled
            rerank_enabled = settings.rag_rerank_enabled
            rerank_top_n = settings.rag_rerank_top_n
            prose_priority = settings.rag_prose_priority_enabled
            prose_priority_weight = settings.rag_prose_priority_weight
            dedup_enabled = settings.rag_dedup_enabled
            dedup_threshold = settings.rag_dedup_threshold
            result_max_chars = settings.rag_result_max_chars
        except Exception:
            fusion, apply_recency, rerank_enabled, rerank_top_n = "rrf", False, False, 20
            prose_priority, prose_priority_weight = True, 0.4
            dedup_enabled, dedup_threshold, result_max_chars = True, 0.9, 1000

        now = utc_now()
        search_limit = max(max_results, rerank_top_n) if rerank_enabled else max_results
        results = memory_index.search(
            query=query,
            user_id=user_id,
            limit=search_limit,
            chunk_types=chunk_types,
            thread_id=thread_id,
            since=_parse_when(since),
            until=_parse_when(until),
            fusion=fusion,
            apply_recency=apply_recency,
            now=now,
            apply_prose_priority=prose_priority,
            prose_priority_weight=prose_priority_weight,
            dedup=dedup_enabled,
            dedup_threshold=dedup_threshold,
        )

        # Optional LLM listwise rerank (off by default; adds latency + tokens).
        if rerank_enabled and len(results) > 1:
            try:
                from ..core.agent import get_current_agent
                from ..core.rag_quality import llm_rerank
                agent = get_current_agent()
                if agent is not None:
                    results = llm_rerank(
                        agent,
                        get_effective_thread_id(config),
                        query,
                        results,
                        top_n=rerank_top_n,
                    )
            except Exception as e:
                logger.warning(f"rag_search rerank skipped: {e}")

        results = results[:max_results]

        if not results:
            return f"[No Results]: No relevant context found for '{query}'."

        resolve_title = _thread_title_resolver(user_id)
        top_score = max((r.score for r in results), default=0.0) or 1.0

        lines = [
            f"Found {len(results)} relevant result(s) for '{query}' "
            f"(now: {now.isoformat()}):\n"
        ]

        for i, result in enumerate(results, 1):
            type_emoji = {
                'conversation': '💬',
                'memory': '🧠',
                'todo': '✅',
            }.get(result.chunk_type, '📝')

            event_time = result.event_time or result.created_at
            age = _humanize_age((now - event_time).total_seconds())
            relevance = result.score / top_score

            # Provenance: thread title + id for thread-scoped chunks; saved
            # memories are global (no thread).
            if result.thread_id:
                title = resolve_title(result.thread_id)
                if title:
                    source = f'thread "{title}" ({result.thread_id})'
                else:
                    source = f"thread {result.thread_id}"
            else:
                source = "saved memory (global)"

            content = result.content
            if len(content) > result_max_chars:
                content = content[:max(0, result_max_chars - 3)] + "..."

            lines.append(
                f"{i}. {type_emoji} [{result.chunk_type}] "
                f"({age}, {event_time.isoformat()}, relevance {relevance:.2f})"
            )
            lines.append(f"   from {source}")
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
    logger.info("rag_settings called")

    user_id = get_user_id(config)
    manager = _get_profile_manager()

    with manager.atomic_update(user_id) as profile:
        if enabled is not None:
            profile.opt_in.rag_enabled = enabled
            logger.info(f"RAG {'enabled' if enabled else 'disabled'} for user {user_id}")

        if max_chunks is not None:
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

        rag_prefs = profile.get_rag_preferences()
        status = "enabled" if profile.opt_in.rag_enabled else "disabled"

        lines = [
            f"RAG Settings (currently {status}):",
            f"- enabled: {profile.opt_in.rag_enabled}",
            f"- max_chunks: {rag_prefs.get('max_chunks', 5)}",
            f"- include_conversations: {rag_prefs.get('include_conversations', True)}",
            f"- include_memories: {rag_prefs.get('include_memories', False)}",
            f"- include_todos: {rag_prefs.get('include_todos', True)}",
            f"- auto_flush: {rag_prefs.get('auto_flush', True)}",
        ]

        if profile.opt_in.rag_enabled:
            memory_index = _get_memory_index(user_id)
            if memory_index:
                stats = memory_index.get_stats(user_id)
                lines.append("\nIndex stats:")
                lines.append(f"- Total chunks: {stats.get('total_chunks', 0)}")
                for chunk_type, count in stats.get('by_type', {}).items():
                    lines.append(f"  - {chunk_type}: {count}")

        return "\n".join(lines)


# Export memory tools
MEMORY_TOOLS = [
    memory_add,
    memory_edit,
    memory_read,
    personality_set,
    rag_search,
]
