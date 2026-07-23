"""Spawn and optionally delete conversation threads with per-thread config.

Lets the agent create a new thread that appears in the desktop sidebar with
its own appended instructions, tool selection, and optional LLM overrides.
Spawned threads are callable (invocable as tools) by default, so the parent
can re-invoke them later. Supports two actions:

  action="create" (default): Create a new thread. Optionally dispatches an
      prompt and blocks until the child responds.
  action="delete": Remove a previously-spawned thread (metadata, config,
      checkpoints, notepad, and callable-tool registration). Only the thread
      that originally spawned it can delete it.

The tool exposes three ergonomic knobs over the raw thread config:
  * tool_queries: free-text intents resolved via the semantic tool search
    index (e.g. ["research"] selects web/RAG/wiki tools).
  * ttl_hours: single optional TTL (None = permanent, int = temporary with
    that many idle hours before the ticker sweeps it).
  * include_core_tools: when False, the child skips SEED_TOOLS and gets only
    the explicitly resolved/selected set.

It only exposes the *append* path for system prompts
(ThreadConfig.instructions); it cannot replace soul.md.
"""
from .registry import ToolGroup, register_tool_group

import logging
import os
import threading
import time
import uuid
from typing import Annotated, Any, Dict, List, Optional, Set, Tuple

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_thread_id_or_none, get_user_id

logger = logging.getLogger(__name__)

DEFAULT_MAX_SPAWN_DEPTH = 3
DEFAULT_MAX_SPAWNS_PER_HOUR = 10
RATE_WINDOW_SECONDS = 3600

VALID_MODES = ("fresh", "branched")
VALID_LIFETIMES = ("permanent", "temporary")
DEFAULT_IDLE_TIMEOUT_HOURS = 24
PLATFORM_META_LIFETIME = "lifetime"
PLATFORM_META_IDLE_TIMEOUT = "idle_timeout_hours"
PLATFORM_META_LAST_ACTIVE = "last_active_at"

# Process-local rate limiter: parent_thread_id -> list of spawn timestamps.
# Intentionally not persisted — process restart breaks any active spawn loop,
# and the depth limit (DEFAULT_MAX_SPAWN_DEPTH) is the hard guard against
# recursive chains. For single-process deployments this is sufficient; if
# horizontal scaling is ever added, move counters to Redis or the accounts DB.
_spawn_rate_lock = threading.Lock()
_spawn_counts: Dict[str, List[float]] = {}


def _slug_from_title(title: str, max_len: int = 24) -> str:
    """Safe slug (hyphenated) for thread IDs."""
    safe = "".join(c if c.isalnum() else "-" for c in title.lower())
    safe = "-".join(filter(None, safe.split("-")))
    safe = safe[:max_len].rstrip("-")
    return safe or "thread"


def _callable_name_from_title(title: str) -> str:
    """Unique callable_name (snake_case + random suffix).

    Prefixed with 'spawned_' so auto-generated names don't collide with
    user-created callable threads or core tool names.
    """
    safe = "".join(c.lower() if c.isalnum() else "_" for c in title)
    safe = "_".join(filter(None, safe.split("_")))
    safe = safe[:24].strip("_") or "thread"
    return f"spawned_{safe}_{uuid.uuid4().hex[:8]}"


def _check_rate_limit(parent_thread_id: str) -> Optional[str]:
    """Return an error message if rate-limited, else None."""
    max_per_hour = int(
        os.environ.get("NYMERIA_MAX_SPAWNS_PER_HOUR", DEFAULT_MAX_SPAWNS_PER_HOUR)
    )
    now = time.time()
    with _spawn_rate_lock:
        timestamps = _spawn_counts.get(parent_thread_id, [])
        timestamps = [t for t in timestamps if now - t < RATE_WINDOW_SECONDS]
        if len(timestamps) >= max_per_hour:
            oldest = min(timestamps)
            wait = int(RATE_WINDOW_SECONDS - (now - oldest))
            return (
                f"[Error]: Spawn rate limit reached ({max_per_hour} per hour). "
                f"Wait ~{wait}s before spawning again."
            )
        timestamps.append(now)
        _spawn_counts[parent_thread_id] = timestamps
    return None


def _get_parent_spawn_depth(
    parent_thread_id: Optional[str], agent, user_id: str = "default"
) -> int:
    """Read parent's spawn_depth from thread metadata. Returns 0 if not set.

    Reads under the caller's ``user_id`` partition because spawn_thread saves
    metadata under the actual user_id (not always "default") since the
    multi-user refactor. A hardcoded "default" lookup here weakened the
    max-depth guard for non-default users.
    """
    if not parent_thread_id or not agent:
        return 0
    try:
        meta = agent.thread_metadata_manager.get_thread(user_id, parent_thread_id)
        if meta and meta.platform_meta:
            return int(meta.platform_meta.get("spawn_depth", "0"))
    except Exception:
        logger.warning("Failed to read parent spawn depth", exc_info=True)
    return 0


def _delete_checkpoints(thread_id: str) -> None:
    """Delete all checkpoint rows for a thread (SQLite or Postgres)."""
    from ..config.settings import get_settings
    from ..core.checkpoint_cleanup import delete_thread_checkpoints

    settings = get_settings()
    try:
        delete_thread_checkpoints(settings, thread_id)
    except Exception as e:
        logger.warning(f"spawn_thread: checkpoint delete failed for {thread_id}: {e}")


def _delete_spawned(
    agent,
    target_thread_id: str,
    user_id: str,
    caller_thread_id: Optional[str],
) -> str:
    """Delete a spawned thread (config, metadata, checkpoints, notepad)."""
    from ..core.event_bus import publish_sync_event

    if not target_thread_id or not target_thread_id.startswith("spawned-"):
        return (
            f"[Error]: Can only delete spawned threads (id must start with "
            f"'spawned-'). Got: {target_thread_id!r}"
        )

    target_meta = agent.thread_metadata_manager.get_thread(user_id, target_thread_id)
    target_config = agent.thread_config_manager.get_config(target_thread_id)
    if target_meta is None and target_config is None:
        return f"[Error]: Spawned thread not found: {target_thread_id}"

    spawn_parent = None
    if target_meta and target_meta.platform_meta:
        spawn_parent = target_meta.platform_meta.get("spawn_parent")

    # Only the spawning parent may delete. If metadata has no recorded parent
    # (old spawn), allow deletion from any thread as a fallback.
    if caller_thread_id and spawn_parent and spawn_parent != caller_thread_id:
        return (
            f"[Error]: Only the parent that spawned this thread can delete it. "
            f"Caller is {caller_thread_id!r}; spawn_parent is {spawn_parent!r}."
        )

    was_callable = bool(target_config and target_config.callable)

    try:
        agent.thread_metadata_manager.delete_thread(user_id, target_thread_id)
    except Exception as e:
        logger.warning(
            f"spawn_thread: metadata delete failed for {target_thread_id}: {e}"
        )

    _delete_checkpoints(target_thread_id)

    try:
        agent.thread_config_manager.delete_config(target_thread_id)
    except Exception as e:
        logger.warning(
            f"spawn_thread: config delete failed for {target_thread_id}: {e}"
        )

    try:
        agent.invalidate_thread_config_cache(target_thread_id)
    except Exception as e:
        logger.warning(
            f"spawn_thread: cache invalidate failed for {target_thread_id}: {e}"
        )

    if was_callable:
        try:
            agent.sync_agent_tools()
        except Exception as e:
            logger.warning(f"spawn_thread: sync_agent_tools failed after delete: {e}")

    try:
        from .thread_notes import delete_notepad

        delete_notepad(target_thread_id)
    except Exception as e:
        logger.warning(
            f"spawn_thread: notepad delete failed for {target_thread_id}: {e}"
        )

    try:
        publish_sync_event(
            event_type="thread_deleted",
            thread_id=target_thread_id,
            user_id=user_id,
            data={},
        )
    except Exception as e:
        logger.warning(f"spawn_thread: thread_deleted publish failed: {e}")

    logger.info(
        f"Deleted spawned thread {target_thread_id} (was_callable={was_callable})"
    )
    return f"[Deleted]: thread_id={target_thread_id}"


def refresh_thread_activity(agent, user_id: str, thread_id: str) -> None:
    """Reset the idle-timeout clock for a temporary-lifetime spawned thread.

    Called when the thread is invoked or runs a turn. No-op if the thread
    is not flagged as temporary, so callers don't need to check first.
    """
    try:
        meta = agent.thread_metadata_manager.get_thread(user_id, thread_id)
    except Exception:
        return
    if meta is None or not meta.platform_meta:
        return
    if meta.platform_meta.get(PLATFORM_META_LIFETIME) != "temporary":
        return

    from ..core.time_utils import utc_now as _utc_now

    updated = dict(meta.platform_meta)
    updated[PLATFORM_META_LAST_ACTIVE] = _utc_now().isoformat()
    try:
        agent.thread_metadata_manager.upsert_thread(
            user_id, thread_id, platform_meta=updated
        )
    except Exception:
        logger.debug(
            f"refresh_thread_activity: upsert failed for {thread_id}",
            exc_info=True,
        )


def _delete_temporary_thread(agent, target_thread_id: str, user_id: str) -> bool:
    """Delete any temporary-lifetime thread, including spawned and dream threads."""
    from ..config.settings import get_settings
    from ..core.event_bus import publish_sync_event
    from ..core.thread_deletion import ThreadDeletionBusy, cascade_delete_thread

    try:
        cascade_delete_thread(
            agent,
            get_settings(),
            user_id,
            target_thread_id,
            lock_timeout_seconds=1.0,
        )
    except ThreadDeletionBusy:
        logger.info(
            "Temporary thread %s is busy; idle cleanup will retry later",
            target_thread_id,
        )
        return False
    except Exception:
        logger.warning(
            "Temporary thread cleanup failed for %s",
            target_thread_id,
            exc_info=True,
        )
        return False

    try:
        publish_sync_event(
            event_type="thread_deleted",
            thread_id=target_thread_id,
            user_id=user_id,
            data={},
        )
    except Exception as e:
        logger.warning("temporary thread_deleted publish failed: %s", e)

    logger.info("Deleted temporary thread %s", target_thread_id)
    return True


def sweep_idle_spawned_threads(agent) -> int:
    """Delete temporary-lifetime threads whose idle window has elapsed.

    Iterates every user's thread metadata store, identifies threads flagged
    ``lifetime=temporary`` whose ``last_active_at`` is older than their
    ``idle_timeout_hours``, and deletes them via the standard cascade cleanup
    path (config, metadata, checkpoints, notepad, callable registration).

    Returns the number of threads deleted. Intended to be called periodically
    from the Ticker's housekeeping executor.
    """
    from datetime import datetime, timezone

    from ..core.time_utils import utc_now as _utc_now

    deleted = 0
    try:
        metadata_dir = agent.thread_metadata_manager.metadata_dir
    except AttributeError:
        return 0
    if not metadata_dir.exists():
        return 0

    now = _utc_now()
    for path in sorted(metadata_dir.glob("*.json")):
        user_id = path.stem
        if not user_id:
            continue
        try:
            store = agent.thread_metadata_manager.get_store(user_id)
        except Exception:
            logger.debug(
                f"sweep_idle_spawned_threads: failed to load store for {user_id}",
                exc_info=True,
            )
            continue

        for thread_id, meta in list(store.threads.items()):
            if not meta.platform_meta:
                continue
            if meta.platform_meta.get(PLATFORM_META_LIFETIME) != "temporary":
                continue
            try:
                idle_hours = int(
                    meta.platform_meta.get(PLATFORM_META_IDLE_TIMEOUT, "0")
                )
            except (TypeError, ValueError):
                continue
            if idle_hours < 1:
                continue

            last_active_str = meta.platform_meta.get(PLATFORM_META_LAST_ACTIVE)
            if last_active_str:
                try:
                    last_active = datetime.fromisoformat(last_active_str)
                except (TypeError, ValueError):
                    continue
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=timezone.utc)
            else:
                last_active = meta.created_at
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=timezone.utc)

            elapsed_hours = (now - last_active).total_seconds() / 3600.0
            if elapsed_hours < idle_hours:
                continue

            if _delete_temporary_thread(agent, thread_id, user_id):
                deleted += 1

    return deleted


def _resolve_semantic_tools(
    queries: List[str],
    top_k: int,
    *,
    agent,
    user_id: str,
    user_role: str,
    parent_thread_id: Optional[str],
) -> Tuple[Set[str], List[dict]]:
    """Resolve free-text intents to optional tool names via the search index.

    Returns (set_of_optional_tool_names, sorted_resolution_records). Each
    record is {"name": str, "score": float, "query": str}, deduped by name
    keeping the highest score across queries. Core tools (in SEED_TOOLS) are
    excluded since they're inherited by default; unknown names and names not
    accepted as optional/registry tools are skipped. Admin/dev-only tools are
    filtered by the search index when user_role is passed.
    """
    from . import SEED_TOOLS, CATALOG_TOOLS
    from ..core.tool_search_index import search_tools

    core_names = {t.name for t in SEED_TOOLS}
    best: Dict[str, dict] = {}
    for raw_query in queries:
        query = (raw_query or "").strip()
        if not query:
            continue
        try:
            response = search_tools(
                query,
                user_id=user_id or "default",
                user_role=user_role or "user",
                agent=agent,
                thread_id=parent_thread_id,
                top_k=max(1, int(top_k)),
                include_status=False,
            )
        except Exception:
            logger.warning(
                "spawn_thread: semantic search failed for query %r",
                query,
                exc_info=True,
            )
            continue

        for result in getattr(response, "results", []) or []:
            name = getattr(result, "name", None)
            if not name or name in core_names:
                continue
            is_optional = name in CATALOG_TOOLS
            is_registry = bool(
                getattr(agent, "tool_registry", None)
                and agent.tool_registry.get_tool(name)
            )
            if not (is_optional or is_registry):
                continue
            score = float(getattr(result, "score", 0.0) or 0.0)
            current = best.get(name)
            if current is None or score > current["score"]:
                best[name] = {"name": name, "score": score, "query": query}

    records = sorted(best.values(), key=lambda r: r["score"], reverse=True)
    return set(best.keys()), records


def _reconcile_lifetime(
    ttl_hours: Optional[int],
    lifetime: Optional[str],
    idle_timeout_hours: Optional[int],
) -> Tuple[Optional[int], List[str], Optional[str]]:
    """Resolve the ttl_hours / legacy lifetime+idle_timeout_hours knobs.

    Returns ``(ttl_hours_resolved, warnings, error)``. ``ttl_hours_resolved`` is
    None for a permanent thread. On a validation failure ``error`` is the
    agent-facing ``"[Error]: ..."`` string and the caller returns it immediately
    (the other fields are then unused).
    """
    pre_warnings: List[str] = []
    if lifetime is not None:
        lifetime_norm = (lifetime or "permanent").strip().lower()
        if lifetime_norm not in VALID_LIFETIMES:
            return None, pre_warnings, (
                f"[Error]: Unknown lifetime '{lifetime}'. Use "
                f"{' or '.join(repr(value) for value in VALID_LIFETIMES)}."
            )
        if ttl_hours is not None:
            pre_warnings.append(
                "legacy lifetime/idle_timeout_hours ignored because ttl_hours is set"
            )
        elif lifetime_norm == "temporary":
            ttl_hours = (
                DEFAULT_IDLE_TIMEOUT_HOURS
                if idle_timeout_hours is None
                else idle_timeout_hours
            )
        elif idle_timeout_hours is not None:
            pre_warnings.append(
                "idle_timeout_hours ignored when lifetime='permanent'; use ttl_hours for temporary cleanup"
            )
    elif idle_timeout_hours is not None and ttl_hours is None:
        pre_warnings.append(
            "idle_timeout_hours ignored without lifetime='temporary'; use ttl_hours for temporary cleanup"
        )

    if ttl_hours is None:
        return None, pre_warnings, None
    try:
        ttl_hours_int = int(ttl_hours)
    except (TypeError, ValueError):
        return None, pre_warnings, (
            f"[Error]: ttl_hours must be an integer; got {ttl_hours!r}."
        )
    if ttl_hours_int < 1:
        return None, pre_warnings, (
            "[Error]: ttl_hours must be >= 1 (or None for a permanent thread)."
        )
    return ttl_hours_int, pre_warnings, None


def _resolve_spawn_tools(
    agent,
    *,
    user_id: str,
    parent_thread_id: Optional[str],
    tool_queries: Optional[List[str]],
    tool_query_top_k: int,
    tool_categories: Optional[List[str]],
    optional_tools: Optional[List[str]],
    disabled_tools: Optional[List[str]],
    include_core_tools: bool,
    pre_warnings: List[str],
) -> Tuple[Set[str], List[str], List[dict], List[str], Optional[str]]:
    """Resolve the child's enabled/disabled tool sets from the spawn knobs.

    Runs the three resolution passes (tool_queries -> tool_categories ->
    optional_tools), the admin/developer-only gate, the disabled_tools pass, and
    the ``include_core_tools=False`` funnel, threading ``warnings`` from
    ``pre_warnings``. Returns ``(enabled_set, disabled_list,
    tool_resolution_records, warnings, error)``. On the admin/developer gate
    ``error`` is the agent-facing ``"[Error]: ..."`` string and the caller
    returns it immediately (no thread is created; the other fields are unused).
    """
    from . import (
        CATALOG_TOOLS,
        SEED_TOOLS,
        filter_admin_only_tools,
        filter_developer_only_tools,
    )
    from .metadata import get_all_categories, get_category_tools_summary

    warnings: List[str] = list(pre_warnings)
    enabled_set: Set[str] = set()
    tool_resolution_records: List[dict] = []

    user = agent.accounts_repo.get_user_by_id(user_id) if user_id else None
    user_role = user.role if user else "user"

    if tool_queries:
        resolved_names, tool_resolution_records = _resolve_semantic_tools(
            tool_queries,
            tool_query_top_k,
            agent=agent,
            user_id=user_id,
            user_role=user_role,
            parent_thread_id=parent_thread_id,
        )
        enabled_set |= resolved_names
        unmatched_queries = [
            q.strip()
            for q in tool_queries
            if q
            and q.strip()
            and not any(r["query"] == q.strip() for r in tool_resolution_records)
        ]
        if unmatched_queries:
            warnings.append(
                f"tool_queries with no matches: {', '.join(unmatched_queries)}"
            )

    if tool_categories:
        cat_summary = get_category_tools_summary()
        valid_cats = set(get_all_categories())
        unknown_cats: List[str] = []
        for cat in tool_categories:
            cat_norm = cat.lower().strip().replace("-", "_")
            if cat_norm not in valid_cats:
                unknown_cats.append(cat)
                continue
            for name in cat_summary.get(cat_norm, []):
                if name in CATALOG_TOOLS:
                    enabled_set.add(name)
        if unknown_cats:
            warnings.append(f"unknown categor(ies): {', '.join(unknown_cats)}")

    if optional_tools:
        all_known = {t.name for t in SEED_TOOLS} | set(CATALOG_TOOLS.keys())
        unknown_tools: List[str] = []
        for name in optional_tools:
            if name in CATALOG_TOOLS:
                enabled_set.add(name)
            elif name in all_known or (
                agent.tool_registry and agent.tool_registry.get_tool(name)
            ):
                enabled_set.add(name)
            else:
                unknown_tools.append(name)
        if unknown_tools:
            warnings.append(f"unknown tool(s): {', '.join(unknown_tools)}")

    # Admin-only gate. Mirror the REST gate at PATCH /threads/{id}/config and
    # the tool_search gate; without this, a non-admin could spawn a child
    # thread seeded with reload_all/claude_code/self_modify and escalate via
    # the child. Block category-expansion AND named optional_tools.
    _, blocked = filter_admin_only_tools(enabled_set, user_role)
    if blocked:
        return enabled_set, [], tool_resolution_records, warnings, (
            f"[Error]: Admin-only tools cannot be enabled on a spawned thread "
            f"by this user: {sorted(blocked)}. Drop them from optional_tools / "
            f"tool_categories or ask an administrator to spawn the thread."
        )
    _, blocked = filter_developer_only_tools(enabled_set, user_role)
    if blocked:
        return enabled_set, [], tool_resolution_records, warnings, (
            f"[Error]: Developer-only diagnostic tools cannot be enabled on a "
            f"spawned thread by this user: {sorted(blocked)}. Drop them from "
            f"optional_tools / tool_categories or ask an administrator to "
            f"spawn the thread."
        )

    disabled_list: List[str] = []
    if disabled_tools:
        # disabled_tools is authoritative subtraction at graph-build and can
        # target ANY bound tool, seed or catalog (optional) or dynamic, not just
        # seed tools. Mirror the optional_tools validation above: accept any
        # known tool name and warn only for genuinely unknown ones (the old code
        # checked SEED_TOOLS alone, mislabeling valid optional names as
        # "unknown core tool(s)").
        all_known = {t.name for t in SEED_TOOLS} | set(CATALOG_TOOLS.keys())
        unknown_disabled: List[str] = []
        for name in disabled_tools:
            if name in all_known or (
                agent.tool_registry and agent.tool_registry.get_tool(name)
            ):
                disabled_list.append(name)
            else:
                unknown_disabled.append(name)
        if unknown_disabled:
            warnings.append(
                f"unknown tool(s) to disable: {', '.join(unknown_disabled)}"
            )

    if not include_core_tools:
        # Funnel every core tool name into disabled_list so the graph builder
        # filters them out. Dedupe in case the caller also named some explicitly.
        disabled_list = sorted({*(disabled_list), *(t.name for t in SEED_TOOLS)})

    return enabled_set, disabled_list, tool_resolution_records, warnings, None


def _build_spawn_preamble(
    *,
    new_thread_id: str,
    mode_norm: str,
    parent_thread_id: Optional[str],
    ttl_hours_resolved: Optional[int],
    make_callable: bool,
    callable_name: Optional[str],
    tool_resolution_records: List[dict],
    include_core_tools: bool,
    enabled_tools: List[str],
    disabled_tools: List[str],
    warnings: List[str],
    kit_line: Optional[str] = None,
    team_line: Optional[str] = None,
) -> str:
    """Render the ``[Spawned]`` preamble (everything before the optional child
    response)."""
    preamble_lines = [f"[Spawned]: thread_id={new_thread_id}"]
    if mode_norm == "branched":
        preamble_lines.append(
            f"Mode: branched from {parent_thread_id} (inherits checkpoint history)."
        )
    if team_line:
        preamble_lines.append(team_line)
    if ttl_hours_resolved is not None:
        preamble_lines.append(
            f"Lifetime: temporary (auto-deletes after "
            f"{ttl_hours_resolved}h of inactivity)."
        )
    if make_callable and callable_name:
        invoke_scope = (
            "Same-team threads can invoke this."
            if team_line
            else "Any unteamed thread can invoke this."
        )
        preamble_lines.append(
            f'Callable as: {callable_name}(task="..."). {invoke_scope}'
        )
    if kit_line:
        preamble_lines.append(kit_line)
    if tool_resolution_records:
        head = tool_resolution_records[:6]
        rendered = ", ".join(
            f'{r["name"]} ({r["score"]:.2f} <- "{r["query"]}")' for r in head
        )
        if len(tool_resolution_records) > len(head):
            rendered += f", +{len(tool_resolution_records) - len(head)} more"
        preamble_lines.append(f"[Resolved tools]: {rendered}")
    if not include_core_tools:
        preamble_lines.append(
            "Core tools: disabled (include_core_tools=False; child gets only "
            "the explicitly resolved/selected tools)."
        )
    if enabled_tools:
        preamble_lines.append(
            f"Enabled optional tools: {', '.join(enabled_tools)}"
        )
    if disabled_tools and include_core_tools:
        preamble_lines.append(
            f"Disabled core tools: {', '.join(disabled_tools)}"
        )
    if warnings:
        preamble_lines.append(f"[Warning]: {'; '.join(warnings)}")
    preamble_lines.append(
        f'To delete later: spawn_thread(action="delete", '
        f'delete_thread_id="{new_thread_id}")'
    )
    return "\n".join(preamble_lines)


@tool
def spawn_thread(
    title: Optional[str] = None,
    instructions: Optional[str] = None,
    tool_queries: Optional[List[str]] = None,
    tool_query_top_k: int = 8,
    optional_tools: Optional[List[str]] = None,
    tool_categories: Optional[List[str]] = None,
    disabled_tools: Optional[List[str]] = None,
    include_core_tools: bool = True,
    make_callable: bool = True,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_temperature: Optional[float] = None,
    llm_max_tokens: Optional[int] = None,
    llm_extended_thinking: Optional[bool] = None,
    llm_reasoning_effort: Optional[str] = None,
    kit: Optional[str] = None,
    prompt: Optional[str] = None,
    action: str = "create",
    delete_thread_id: Optional[str] = None,
    mode: str = "fresh",
    ttl_hours: Optional[int] = None,
    lifetime: Optional[str] = None,
    idle_timeout_hours: Optional[int] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Create or delete a conversation thread with scoped configuration.

    Two actions via the `action` parameter:

      action="create" (default): Create a new thread. It appears in the
          desktop sidebar inside a "Spawned by Nymeria" folder. By default
          the thread is CALLABLE; it's registered as a global tool so any
          thread (including its parent) can invoke it by calling the
          auto-generated tool name. Set make_callable=False to opt out.
          If the spawning thread belongs to a callable team, the child
          inherits that team (teams are isolated bubbles in both
          directions).

      action="delete": Remove a previously-spawned thread. Only the thread
          that originally spawned it can delete it. Cleans up metadata,
          config, checkpoints, notepad, and (if callable) unregisters
          the tool globally.

    Args (create mode):
        title: Required for create. User-visible thread title shown in the
            sidebar. Truncated to 80 chars.
        instructions: Extra system-prompt instructions APPENDED to the
            default personality (soul.md). Max 5000 chars. Cannot replace
            the base personality. Also used as the callable tool's
            description if provided.
        tool_queries: Free-text intents (e.g. ["research", "browser
            automation"]). Each query is resolved via the semantic tool
            search index and the top matches are merged into enabled_tools.
            Faster than enumerating exact tool names. Mix freely with
            optional_tools/tool_categories. Returns 0 results silently if
            the search index is unavailable; check the [Resolved tools]
            line in the response to see what landed.
        tool_query_top_k: Max matches kept per query before deduping across
            queries. Default 8. Increase for broader nets, decrease for
            tighter focus.
        optional_tools: Lower-level escape hatch when a specific tool name
            is known (e.g. ["memory_clear_all", "browser_navigate"]). Use
            tool_queries first; this only when semantic search misses or
            you want surgical control. Names can be discovered with
            Skill(name="tool-management") + tool_search(query="...").
        tool_categories: Category names (e.g. ["email", "browser"]) to
            bulk-enable every optional tool in that category. Merged with
            optional_tools and tool_queries results.
        disabled_tools: CORE tool names to EXCLUDE for this child (e.g.
            ["bash_execute"] for a sandboxed child).
        include_core_tools: If True (default), the child inherits all core
            tools (SEED_TOOLS). Set False to give the child only the tools
            resolved by tool_queries/optional_tools/tool_categories (plus
            its callable invoker if make_callable). Useful for tight,
            focused sub-agents that should not have memory writes,
            sub-spawning, file IO, etc.
        make_callable: If True (default), the new thread becomes a globally
            callable tool. The tool's name is auto-derived from the title
            with a random suffix (e.g. 'spawned_research_a3f21c9d') and
            printed in the response so you can invoke it later. Set False
            if this thread should be single-use.
        llm_provider, llm_model, llm_temperature, llm_max_tokens,
        llm_extended_thinking, llm_reasoning_effort: Optional LLM overrides
            for this thread. Omit to inherit global settings. llm_model also
            accepts the tier aliases "fast", "smart", or "default": these
            resolve to the configured fast/smart/primary model (and may route to
            a different provider), so you can pick a cheap model for simple
            sub-tasks or a stronger model for hard ones without naming an exact
            model ID.
        kit: Optional skill or Skill Kit name to activate on the new
            thread (e.g. "web-research"). The skill is added to the
            child's enabled_skills; if it is a Skill Kit, its
            required_tools are also bound with the kit's declared TTL.
            An unknown kit name or a failed tool binding aborts the
            spawn (no half-configured thread is left behind).
        prompt: If provided, dispatches this message to the new
            thread and BLOCKS until the child returns its response. The
            child's response becomes part of this tool's output.
        mode: 'fresh' (default) creates an empty thread. 'branched' forks
            the calling thread's checkpoint history and configuration via
            branch_thread(); the new thread starts with the parent's full
            conversation context, then your overrides are layered on top.
        ttl_hours: Optional thread lifetime. None (default) = permanent.
            Any positive integer marks the thread temporary; the worker
            ticker deletes it after that many hours without activity
            (no callable invocations and no own turns). Activity is
            refreshed automatically on each turn and each callable invoke.
        lifetime: Deprecated compatibility alias. Use ttl_hours instead.
            If set to "temporary" and ttl_hours is omitted, uses
            idle_timeout_hours or the default 24-hour idle TTL.
        idle_timeout_hours: Deprecated compatibility alias for ttl_hours
            when lifetime="temporary".

    Args (delete mode):
        delete_thread_id: Required for delete. The spawned thread's ID
            (must start with "spawned-"). Only valid if the calling thread
            is the one that originally spawned it.

    Returns (create):
        Preamble with the new thread_id, the callable tool name (if
        make_callable=True), the [Resolved tools] line when tool_queries
        was used, and, if prompt was provided, the child thread's
        response text.

    Returns (delete):
        "[Deleted]: thread_id=spawned-..." on success.

    Limits (create):
        - Spawn depth capped at 3 by default (NYMERIA_MAX_SPAWN_DEPTH env).
        - 10 spawns per parent per hour (NYMERIA_MAX_SPAWNS_PER_HOUR).
        - instructions max 5000 chars; title truncated to 80 chars.
        - ttl_hours must be >= 1 when set.
    """
    from ..config.model_tiers import is_thread_tier_alias, resolve_tier
    from ..core.agent import get_current_agent
    from ..core.event_bus import publish_sync_event
    from ..core.thread_config import ThreadConfig, ThreadLLMConfig

    agent = get_current_agent()
    if agent is None:
        return "[Error]: No active agent; cannot spawn thread."

    user_id = get_user_id(config)
    parent_thread_id = get_thread_id_or_none(config)

    action_norm = (action or "create").strip().lower()

    # === Delete action ===
    if action_norm == "delete":
        if not delete_thread_id or not delete_thread_id.strip():
            return "[Error]: delete_thread_id is required when action='delete'."
        return _delete_spawned(
            agent=agent,
            target_thread_id=delete_thread_id.strip(),
            user_id=user_id,
            caller_thread_id=parent_thread_id,
        )

    if action_norm != "create":
        return f"[Error]: Unknown action '{action}'. Use 'create' or 'delete'."

    # === Create action ===
    if not title or not title.strip():
        return "[Error]: title is required when action='create' and cannot be empty."
    title = title.strip()[:80]

    # Resolve the kit BEFORE creating anything, so a typo'd name fails fast
    # without burning spawn rate limit or leaving a thread to roll back.
    kit_skill = None
    if kit and kit.strip():
        skill_manager = getattr(agent, "skill_manager", None)
        if skill_manager is None:
            return "[Error]: Skill manager unavailable; cannot bind a kit."
        try:
            kit_skill = skill_manager.get(kit.strip(), user_id=user_id)
        except Exception as e:
            return f"[Error]: Skill lookup failed for kit '{kit.strip()}': {e}"
        if kit_skill is None:
            return (
                f"[Error]: Kit '{kit.strip()}' not found. Use "
                "skill_search to discover available skills and kits."
            )

    mode_norm = (mode or "fresh").strip().lower()
    if mode_norm not in VALID_MODES:
        return f"[Error]: Unknown mode '{mode}'. Use {' or '.join(repr(m) for m in VALID_MODES)}."

    ttl_hours_resolved, pre_warnings, ttl_error = _reconcile_lifetime(
        ttl_hours, lifetime, idle_timeout_hours
    )
    if ttl_error:
        return ttl_error

    if mode_norm == "branched" and not parent_thread_id:
        return "[Error]: mode='branched' requires a parent thread; call this from inside a thread."

    max_depth = int(
        os.environ.get("NYMERIA_MAX_SPAWN_DEPTH", DEFAULT_MAX_SPAWN_DEPTH)
    )
    parent_depth = _get_parent_spawn_depth(parent_thread_id, agent, user_id=user_id)
    new_depth = parent_depth + 1
    if new_depth > max_depth:
        return (
            f"[Error]: Spawn depth limit reached ({max_depth}). "
            f"Parent thread is already at depth {parent_depth}. "
            f"Override via NYMERIA_MAX_SPAWN_DEPTH if intentional."
        )

    if parent_thread_id:
        err = _check_rate_limit(parent_thread_id)
        if err:
            return err

    enabled_set, disabled_list, tool_resolution_records, warnings, tool_error = (
        _resolve_spawn_tools(
            agent,
            user_id=user_id,
            parent_thread_id=parent_thread_id,
            tool_queries=tool_queries,
            tool_query_top_k=tool_query_top_k,
            tool_categories=tool_categories,
            optional_tools=optional_tools,
            disabled_tools=disabled_tools,
            include_core_tools=include_core_tools,
            pre_warnings=pre_warnings,
        )
    )
    if tool_error:
        return tool_error

    # Expand a thread tier alias ("fast"/"smart"/"default") into a concrete
    # provider+model so the child thread carries real IDs. A tier may target a
    # different provider (provider:model), which the runtime resolves credentials
    # for via the existing cross-provider fallback machinery. The global-only
    # "background" utility tier is excluded: it never becomes a thread model.
    if is_thread_tier_alias(llm_model):
        resolved_tier = resolve_tier(
            llm_model,
            agent.settings,
            provider=llm_provider or agent.settings.llm_provider,
        )
        if resolved_tier is not None:
            llm_provider, llm_model = resolved_tier

    llm_config = None
    if any(
        v is not None
        for v in [
            llm_provider,
            llm_model,
            llm_temperature,
            llm_max_tokens,
            llm_extended_thinking,
            llm_reasoning_effort,
        ]
    ):
        llm_config = ThreadLLMConfig(
            provider=llm_provider,
            model=llm_model,
            temperature=llm_temperature,
            max_tokens=llm_max_tokens,
            extended_thinking=llm_extended_thinking,
            reasoning_effort=llm_reasoning_effort,
        )

    slug = _slug_from_title(title)
    rand_suffix = uuid.uuid4().hex[:8]
    new_thread_id = f"spawned-{slug}-{rand_suffix}"

    callable_name: Optional[str] = None
    callable_description: Optional[str] = None
    if make_callable:
        callable_name = _callable_name_from_title(title)
        if instructions and instructions.strip():
            callable_description = instructions.strip()[:500]
        else:
            callable_description = f"Invoke the '{title}' spawned thread"

    # Backlog #97: children join the spawning thread's callable-team bubble
    # so a teamed parent can invoke what it spawns under full team isolation.
    # Branched mode inherits via the full config clone in thread_branch; fresh
    # mode copies the id explicitly below. Only the id is written (the name is
    # deprecated on configs, backlog #100); the receipt's display name resolves
    # from the team store, then a surviving legacy config name, then the id.
    parent_team_id: Optional[str] = None
    parent_team_display: Optional[str] = None
    if parent_thread_id:
        parent_tc = agent.thread_config_manager.get_config(parent_thread_id)
        if parent_tc is not None:
            parent_team_id = getattr(parent_tc, "callable_team_id", None) or None
            if parent_team_id:
                team_manager = getattr(agent, "team_manager", None)
                store_name = None
                if team_manager is not None:
                    try:
                        store_name = team_manager.resolve_team_name(
                            user_id, parent_team_id
                        )
                    except Exception:  # noqa: BLE001 - display-only, never
                        # fail the spawn over a team-name lookup
                        logger.debug(
                            "spawn_thread team-name resolution failed",
                            exc_info=True,
                        )
                parent_team_display = (
                    store_name
                    or getattr(parent_tc, "callable_team_name", None)
                    or parent_team_id
                )

    if mode_norm == "branched":
        from ..core.thread_branch import ThreadBranchError, branch_thread
        from ..config.settings import get_settings

        assert parent_thread_id is not None  # guarded by mode_norm == "branched" check above
        try:
            branch_thread(
                agent=agent,
                settings=get_settings(),
                user_id=user_id,
                source_thread_id=parent_thread_id,
                title=title,
                new_thread_id=new_thread_id,
            )
        except ThreadBranchError as e:
            return f"[Error]: Failed to branch parent thread: {e}"
        except Exception as e:
            logger.exception("spawn_thread: branched-mode branch failed")
            return f"[Error]: Failed to branch parent thread: {e}"

        cloned = agent.thread_config_manager.get_config(new_thread_id)
        if cloned is None:
            return "[Error]: Branched thread config missing after branch."

        merged_enabled = sorted(set(cloned.enabled_tools or []) | enabled_set)
        merged_disabled = sorted(
            set(cloned.disabled_tools or []) | set(disabled_list)
        )
        try:
            tc = cloned.model_copy(
                update={
                    "thread_id": new_thread_id,
                    "instructions": (
                        instructions.strip()
                        if instructions
                        else cloned.instructions
                    ),
                    "enabled_tools": merged_enabled,
                    "disabled_tools": merged_disabled,
                    "llm_config": llm_config if llm_config else cloned.llm_config,
                    "callable": bool(make_callable),
                    "callable_name": callable_name if make_callable else None,
                    "callable_description": (
                        callable_description if make_callable else None
                    ),
                }
            )
        except Exception as e:
            return f"[Error]: Invalid branched configuration overrides: {str(e)}"

        if not agent.thread_config_manager.save_config(tc):
            return "[Error]: Failed to save branched thread config."
    else:
        try:
            tc = ThreadConfig(
                thread_id=new_thread_id,
                instructions=instructions.strip() if instructions else None,
                enabled_tools=sorted(enabled_set),
                disabled_tools=sorted(disabled_list),
                llm_config=llm_config,
                callable=bool(make_callable),
                callable_name=callable_name,
                callable_description=callable_description,
                callable_team_id=parent_team_id,
            )
        except Exception as e:
            return f"[Error]: Invalid configuration: {str(e)}"

        if not agent.thread_config_manager.save_config(tc):
            return "[Error]: Failed to save thread config."

    # Kit activation runs after the config exists (bind_tools_for_thread
    # reads it by thread_id) and before metadata/ownership, so a strict-bind
    # failure (e.g. an admin-blocked required tool for this user) rolls back
    # with a single config delete: no half-configured thread leaks.
    kit_line: Optional[str] = None
    if kit_skill is not None:
        from ..core.command_service import activate_skill_kit

        kit_ok, kit_message = activate_skill_kit(
            agent=agent,
            thread_id=new_thread_id,
            user_id=user_id,
            skill_name=kit_skill.name,
            reason=f"spawn_thread kit binding for {new_thread_id}",
        )
        if not kit_ok:
            try:
                agent.thread_config_manager.delete_config(new_thread_id)
            except Exception:
                logger.warning(
                    "Failed to rollback thread config after kit-bind failure",
                    exc_info=True,
                )
            if mode_norm == "branched":
                # branch_thread already copied checkpoint history; a config
                # delete alone would orphan those rows.
                _delete_checkpoints(new_thread_id)
            return (
                f"[Error]: Failed to activate kit '{kit_skill.name}' on the "
                f"spawned thread: {kit_message}"
            )
        if getattr(kit_skill, "is_skill_kit", False):
            kit_parts = []
            if kit_skill.required_tools:
                kit_parts.append(
                    f"tools: {', '.join(kit_skill.required_tools)}, "
                    f"TTL {kit_skill.tool_ttl}"
                )
            required_skills = getattr(kit_skill, "required_skills", [])
            if required_skills:
                kit_parts.append(f"skills: {', '.join(required_skills)}")
            detail = f" ({'; '.join(kit_parts)})" if kit_parts else ""
            kit_line = f"Kit: {kit_skill.name}{detail}"
        else:
            kit_line = f"Skill: {kit_skill.name} enabled"

    platform_meta: Dict[str, str] = {"spawn_depth": str(new_depth)}
    if parent_thread_id:
        platform_meta["spawn_parent"] = parent_thread_id
    if ttl_hours_resolved is not None:
        from ..core.time_utils import utc_now as _utc_now

        platform_meta[PLATFORM_META_LIFETIME] = "temporary"
        platform_meta[PLATFORM_META_IDLE_TIMEOUT] = str(ttl_hours_resolved)
        platform_meta[PLATFORM_META_LAST_ACTIVE] = _utc_now().isoformat()

    try:
        agent.thread_metadata_manager.upsert_thread(
            user_id,
            new_thread_id,
            title=title,
            title_source="callable",
            platform="callable",
            platform_meta=platform_meta,
        )
    except Exception as e:
        try:
            agent.thread_config_manager.delete_config(new_thread_id)
        except Exception:
            logger.warning("Failed to rollback thread config after error", exc_info=True)
        return f"[Error]: Failed to save thread metadata: {str(e)}"

    # Claim ownership for the spawning user. Without this, callable spawns
    # land in the "legacy unowned" bucket and the per-user graph filter drops
    # them, so the parent thread's LLM gets told the new tool exists but can't
    # actually invoke it. The runtime gate in tool_factory.py checks this row.
    try:
        agent.accounts_repo.claim_thread(new_thread_id, user_id)
    except Exception as e:
        logger.warning(f"spawn_thread: claim_thread failed for {new_thread_id}: {e}")

    agent.invalidate_thread_config_cache(new_thread_id)

    if make_callable:
        try:
            agent.sync_agent_tools()
        except Exception as e:
            logger.warning(
                f"spawn_thread: sync_agent_tools failed after create: {e}"
            )

    try:
        publish_sync_event(
            event_type="thread_created",
            thread_id=new_thread_id,
            user_id=user_id,
            data={
                "title": title,
                "title_source": "callable",
                "platform": "callable",
                "platform_meta": platform_meta,
            },
        )
    except Exception as e:
        logger.warning(f"spawn_thread: thread_created publish failed: {e}")

    preamble = _build_spawn_preamble(
        new_thread_id=new_thread_id,
        mode_norm=mode_norm,
        parent_thread_id=parent_thread_id,
        ttl_hours_resolved=ttl_hours_resolved,
        make_callable=make_callable,
        callable_name=callable_name,
        tool_resolution_records=tool_resolution_records,
        include_core_tools=include_core_tools,
        enabled_tools=tc.enabled_tools,
        disabled_tools=tc.disabled_tools,
        warnings=warnings,
        kit_line=kit_line,
        team_line=(
            f"Team: {parent_team_display or parent_team_id} "
            "(inherited from the spawning thread)."
            if parent_team_id
            else None
        ),
    )

    if not prompt or not prompt.strip():
        return preamble

    response = _invoke_spawned(
        agent=agent,
        child_thread_id=new_thread_id,
        parent_thread_id=parent_thread_id,
        title=title,
        task=prompt.strip(),
        user_id=user_id,
    )
    return f"{preamble}\n\n{response}"


def _invoke_spawned(
    agent,
    child_thread_id: str,
    parent_thread_id: Optional[str],
    title: str,
    task: str,
    user_id: str,
) -> str:
    """Dispatch the initial message to the spawned thread and collect its response.

    Mirrors thread_agent_executor.invoke() but works for either callable or
    non-callable spawned threads. Publishes autonomous events so the frontend
    can stream the child's activity live.
    """
    from langchain_core.runnables.config import var_child_runnable_config
    from langchain_core.tracers.context import (
        run_collector_var,
        tracing_v2_callback_var,
    )

    from ..core.autonomous_turn import AutonomousTurnEmitter
    from ..core.stream_bridge import stream_and_collect

    task_id = f"spawned-{uuid.uuid4().hex[:8]}"

    if parent_thread_id:
        agent.register_callable_invocation(parent_thread_id, child_thread_id)

    config_token = var_child_runnable_config.set(None)
    callback_token = tracing_v2_callback_var.set(None)
    collector_token = run_collector_var.set(None)

    # Bound before the parent-title resolution (which is internally guarded) so
    # the error handler below can always publish the task_completed event.
    # Note: unlike background_bash, spawn intentionally does NOT force
    # task_started before task_completed; it only fires task_started from a
    # streamed chunk (via emitter.handle_chunk). Do not add an
    # emitter.publish_started() call on the success/error paths here without
    # intending to change spawn's SSE contract.
    emitter = AutonomousTurnEmitter(
        thread_id=child_thread_id,
        user_id=user_id,
        task_id=task_id,
        started_data={
            "prompt": task,
            "callable_name": title,
            "trigger": "spawn_thread",
        },
        meta_event_types=("queued", "prompt_queued"),
    )

    try:
        parent_name = parent_thread_id or "unknown"
        if parent_thread_id:
            try:
                parent_meta = agent.thread_metadata_manager.get_thread(
                    user_id, parent_thread_id
                )
                if parent_meta and parent_meta.title:
                    parent_name = parent_meta.title
            except Exception:
                logger.debug("Failed to resolve parent thread title")
        trigger_override = f'SpawnedBy("{parent_thread_id}", "{parent_name}")'

        def stream_error_message(chunk: Dict[str, Any]) -> str:
            content = chunk.get("content")
            return str(content) if content else "spawned thread stream error"

        result = stream_and_collect(
            agent,
            astream_kwargs={
                "message": task,
                "thread_id": child_thread_id,
                "user_id": user_id,
                "_is_self_invoke": True,
                "_trigger_override": trigger_override,
            },
            on_chunk=emitter.handle_chunk,
            error_message_factory=stream_error_message,
        )

        response_text = result.response_text()

        iteration_limit_hit = result.iteration_limit_hit
        if iteration_limit_hit and response_text:
            response_text += (
                "\n\n[Note: Spawned thread was stopped at iteration limit; "
                "result may be incomplete.]"
            )
        elif iteration_limit_hit and not response_text:
            response_text = (
                "[Spawned thread hit iteration limit without producing a response.]"
            )

        emitter.publish_completed(
            {"content": response_text, "callable_name": title}
        )

        return response_text or "[Spawned thread returned no content.]"

    except Exception as e:
        logger.error(
            f"spawn_thread dispatch failed for {child_thread_id}: {e}", exc_info=True
        )
        try:
            emitter.publish_completed(
                {
                    "error": True,
                    "error_message": str(e)[:200],
                    "content": f"Task failed: {str(e)[:200]}",
                    "callable_name": title,
                }
            )
        except Exception:
            logger.warning("Failed to publish task-completed error event", exc_info=True)
        return f"[Error]: Initial message failed: {str(e)}"

    finally:
        run_collector_var.reset(collector_token)
        tracing_v2_callback_var.reset(callback_token)
        var_child_runnable_config.reset(config_token)
        if parent_thread_id:
            agent.unregister_callable_invocation(parent_thread_id, child_thread_id)


SPAWN_THREAD_TOOLS = [spawn_thread]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="spawn_thread", tools=tuple(SPAWN_THREAD_TOOLS)))
