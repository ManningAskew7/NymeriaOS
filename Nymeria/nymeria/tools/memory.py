"""Memory tools for Nymeria — unified CRUD over global profile + per-thread notepad.

The agent sees three primitives that dispatch on ``scope``:

- ``memory_add(scope, key?, content)`` — create or upsert.
- ``memory_edit(scope, key?, find, replace)`` — surgical find/replace within an entry.
- ``memory_read(scope, key?, query?)`` — get one, list all, or substring-filter.

``scope="global"`` operates on key/value entries in the user profile (auto-injected
into every future conversation). ``scope="thread"`` operates on the active thread's
notepad (markdown file that survives context compaction).

``memory_add`` is purely additive: it appends to the thread notepad and
creates/sets a single global key, but never deletes. Removing and clearing
live in ``memory_edit`` -- an empty ``replace`` drops the matched text, and an
empty ``find`` operates on the whole target (rewrite or clear the thread
notepad, or delete just the named global key). Deletes pop the profile entry
(no zombie blanks in ``memory_read`` output) and unlink the notepad file at the
storage layer.

Bulk wipe (``memory_clear_all``) and personality preferences
(``personality_set``) stay separate as lexically distinct tools. Semantic search
(``rag_search`` / ``rag_settings``) is a different mental model again and lives
in ``rag_search_tool.py``; it is re-exported here so the memory module's public
surface is unchanged.
"""

import logging
import threading
from typing import Annotated, Dict, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.user_profile import UserProfileManager
from ..core.memory_index import MemoryIndex
from ..core.memory_limits import (
    get_global_memory_char_limit,
    get_memory_max_entries,
    get_memory_value_max_chars,
    memory_entries_full_error,
    validate_profile_memory_write,
)
from . import thread_notes
from .utils import get_effective_thread_id, get_user_id

# The RAG-search subsystem (semantic search + rerank diagnostics) lives in
# rag_search_tool.py; it is a different mental model from profile/notepad memory
# CRUD (see the module docstring). It is re-exported here so the memory module's
# attribute surface (nymeria.tools.memory.rag_search / rag_settings /
# _do_rerank_with_status / ...) stays unchanged for __init__.py and the existing
# tests that import or patch these via nymeria.tools.memory. rag_search_tool
# imports memory's two shared accessors function-locally, so this top-level
# import is acyclic.
from .rag_search_tool import (  # noqa: F401
    _MANAGED_RERANKERS,
    _do_rerank_with_status,
    _format_retrieval_footer,
    _humanize_age,
    _thread_title_resolver,
    rag_search,
    rag_settings,
)

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


# Per-user MemoryIndex cache. Constructing a MemoryIndex opens the DB, loads the
# sqlite-vec extension, and runs the schema init; the memory tools (rag_search,
# memory_add/edit/read, ...) are invoked repeatedly, so one instance per user is
# reused instead of rebuilt on every call (mirroring how the agent caches its own
# per-user indexes). Keyed by the sanitized user id; the db_path guard rebuilds if
# the resolved path changes (e.g. data_dir is repointed under tests). MemoryIndex
# serializes its own DB access with an internal lock.
_memory_index_cache: Dict[str, MemoryIndex] = {}
_memory_index_cache_lock = threading.Lock()


def _reset_memory_index_cache() -> None:
    """Drop and close all cached MemoryIndex instances. For explicit teardown and
    tests that repoint the data directory."""
    with _memory_index_cache_lock:
        for index in _memory_index_cache.values():
            try:
                index.close()
            except Exception:
                logger.debug("Error closing cached memory index", exc_info=True)
        _memory_index_cache.clear()


def _get_memory_index(user_id: str) -> Optional[MemoryIndex]:
    """Get the cached memory index for a user if RAG is enabled."""
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
        with _memory_index_cache_lock:
            index = _memory_index_cache.get(safe_user_id)
            if index is None or index.db_path != db_path:
                if index is not None:
                    index.close()  # release the evicted instance's connection
                index = MemoryIndex(db_path)
                _memory_index_cache[safe_user_id] = index
            return index
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
    Add to memory: append to the thread notepad, or create/set one global key.
    Never deletes: a thread add appends (it never overwrites existing notes); a
    global add sets only its named key (replacing that key's value, others
    untouched). Use memory_edit to revise, clear, or remove memory.

    scope="global": persistent fact about the user. Requires `key` (e.g.
        "occupation", "favorite_language"). Auto-injected into every future
        conversation. Setting an existing key replaces only that key's value.
        Examples:
          memory_add(scope="global", key="prefers_typescript", content="Yes")
          memory_add(scope="global", key="timezone", content="Australia/Sydney")

    scope="thread": append to the per-thread notepad (markdown that survives
        compaction). No `key` — there is one notepad per thread. Does NOT
        overwrite existing notes; use memory_edit to revise or clear them.
        Example:
          memory_add(scope="thread", content="Working on auth refactor; deadline Friday.")

    Empty `content` is a no-op (nothing is deleted); use memory_edit to remove
    a global key or clear the thread notepad.

    Args:
        scope: "global" (user profile) or "thread" (per-thread notepad).
        content: The memory text. Empty string is a no-op.
        key: Required when scope="global". Ignored when scope="thread".

    Returns:
        Global: "[Saved]: I'll remember '<key>'...". Thread: "[Saved]: Notepad
        updated (N chars)...". Empty content: "[Info]: ..." pointing at
        memory_edit. Errors: "[Error]: <reason>".
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
            return (
                f"[Info]: No content provided; '{key}' is unchanged. memory_add only "
                f"adds. To delete it, use memory_edit(scope='global', key='{key}', "
                f"find='', replace='')."
            )

        with manager.atomic_update(user_id) as profile:
            max_entries = get_memory_max_entries()
            value_cap = get_memory_value_max_chars()
            stored_content = content[:value_cap]
            limit_error = validate_profile_memory_write(
                profile,
                key=key,
                value=stored_content,
                limit=get_global_memory_char_limit(),
                max_entries=max_entries,
                max_value_chars=value_cap,
            )
            if limit_error:
                return limit_error
            ok = profile.add_memory(
                key, content, max_entries=max_entries, max_value_chars=value_cap
            )
            if not ok:
                return memory_entries_full_error(max_entries)
            logger.info(f"Memory saved for user {user_id}: {key}={stored_content[:50]}")
            _rag_index_global(user_id, key, stored_content)
            return f"[Saved]: I'll remember '{key}'. This will be available in all future conversations."

    # scope == "thread" — additive only; never clobbers existing notes.
    thread_id = get_effective_thread_id(config)
    if not content:
        return (
            "[Info]: No content to add; the notepad is unchanged. To revise or "
            "clear it, use memory_edit(scope='thread', ...)."
        )
    return thread_notes.write_notepad(thread_id, content, mode="append")


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
    Edit existing memory: find/replace a substring (first exact match only);
    owns clearing and removing.

    scope="global": find/replace within a single profile memory's value.
        Requires `key`. Empty `find` operates on the whole value: a non-empty
        `replace` sets it, an empty `replace` deletes just that key. If a
        find/replace empties the value, the key is removed.
        Examples:
          memory_edit(scope="global", key="job_title", find="Engineer", replace="Senior Engineer")
          memory_edit(scope="global", key="old_fact", find="", replace="")   # delete this key

    scope="thread": find/replace within the thread notepad's markdown. No
        `key`. Empty `find` operates on the whole notepad: a non-empty `replace`
        rewrites it end-to-end, an empty `replace` clears it. Empty `replace`
        with a non-empty `find` deletes just the matched text; if the notepad
        becomes empty, the file is deleted.
        Examples:
          memory_edit(scope="thread", find="deadline Friday", replace="deadline Monday")
          memory_edit(scope="thread", find="", replace="<consolidated notepad>")  # full rewrite
          memory_edit(scope="thread", find="", replace="")                        # clear notepad

    Args:
        scope: "global" or "thread".
        find: Exact substring to locate (first occurrence). Empty = whole target.
        replace: Replacement text. Empty string deletes the matched substring
            (or the whole target when `find` is empty).
        key: Required when scope="global". Ignored when scope="thread".

    Returns:
        Global: "[Saved]: Updated '<key>'" or "[Deleted]: Removed '<key>'".
        Thread: "[Saved]: Text replaced...", "[Saved]: Notepad rewritten (N
        chars).", or "[Saved]: Notepad cleared...". Errors: "[Error]: <reason>"
        (key not found, substring not matched).
    """
    err = _validate_scope(scope)
    if err:
        return err

    if scope == "global":
        if not key:
            return "[Error]: scope='global' requires a key."

        user_id = get_user_id(config)
        manager = _get_profile_manager()

        with manager.atomic_update(user_id) as profile:
            mem = profile.get_memory(key)
            if not mem:
                return f"[Error]: No memory with key '{key}'."

            if not find:
                # Empty find = operate on the whole value of this key: a blank
                # replace deletes just this key, otherwise set its value.
                updated_value = replace
            else:
                if find not in mem.value:
                    return f"[Error]: Could not find '{find}' in memory '{key}'."
                updated_value = mem.value.replace(find, replace, 1)

            if updated_value == "":
                profile.remove_memory(key)
                logger.info(f"Memory '{key}' edited to empty -> removed for user {user_id}")
                _rag_remove_global(user_id, key)
                return f"[Deleted]: Removed '{key}'."

            value_cap = get_memory_value_max_chars()
            stored_value = updated_value[:value_cap]
            limit_error = validate_profile_memory_write(
                profile,
                key=key,
                value=stored_value,
                limit=get_global_memory_char_limit(),
                max_value_chars=value_cap,
            )
            if limit_error:
                return limit_error
            profile.add_memory(key, updated_value, max_value_chars=value_cap)
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


# Export memory tools
MEMORY_TOOLS = [
    memory_add,
    memory_edit,
    memory_read,
    personality_set,
    rag_search,
]
