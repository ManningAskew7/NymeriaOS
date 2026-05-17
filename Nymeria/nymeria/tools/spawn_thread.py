"""Spawn and optionally delete conversation threads with per-thread config.

Lets the agent create a new thread that appears in the desktop sidebar with
its own appended instructions, tool selection, and optional LLM overrides.
Spawned threads are callable (invocable as tools) by default, so the parent
can re-invoke them later. Supports two actions:

  action="create" (default): Create a new thread. Optionally dispatches an
      initial_message and blocks until the child responds.
  action="delete": Remove a previously-spawned thread (metadata, config,
      checkpoints, notepad, and callable-tool registration). Only the thread
      that originally spawned it can delete it.

The tool only exposes the *append* path for system prompts
(ThreadConfig.instructions); it cannot replace soul.md.
"""

import logging
import os
import threading
import time
import uuid
from typing import Annotated, Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_thread_id_or_none, get_user_id

logger = logging.getLogger(__name__)

DEFAULT_MAX_SPAWN_DEPTH = 3
DEFAULT_MAX_SPAWNS_PER_HOUR = 10
RATE_WINDOW_SECONDS = 3600

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


@tool
def spawn_thread(
    title: Optional[str] = None,
    instructions: Optional[str] = None,
    optional_tools: Optional[List[str]] = None,
    tool_categories: Optional[List[str]] = None,
    disabled_tools: Optional[List[str]] = None,
    make_callable: bool = True,
    llm_provider: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_temperature: Optional[float] = None,
    llm_max_tokens: Optional[int] = None,
    llm_extended_thinking: Optional[bool] = None,
    llm_reasoning_effort: Optional[str] = None,
    initial_message: Optional[str] = None,
    action: str = "create",
    delete_thread_id: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Create or delete a conversation thread with scoped configuration.

    Two modes via the `action` parameter:

      action="create" (default): Create a new thread. It appears in the
          desktop sidebar inside a "Spawned by Nymeria" folder. By default
          the thread is CALLABLE; it's registered as a global tool so any
          thread (including its parent) can invoke it by calling the
          auto-generated tool name. Set make_callable=False to opt out.

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
        optional_tools: List of OPTIONAL tool names to enable on the new
            thread (e.g. ["memory_clear_all", "browser_navigate"]). Core tools
            are inherited automatically; only list EXTRAS. Use
            Skill(name="self-improve") then tool_search(query="...") to
            discover names.
        tool_categories: List of category names (e.g. ["email", "browser"])
            to bulk-enable every optional tool in that category. Merged
            with optional_tools.
        disabled_tools: List of CORE tool names to EXCLUDE (e.g.
            ["bash_execute"] for a sandboxed child).
        make_callable: If True (default), the new thread becomes a globally
            callable tool. The tool's name is auto-derived from the title
            with a random suffix (e.g. 'spawned_research_a3f21c9d') and
            printed in the response so you can invoke it later. Set False
            if this thread should be single-use.
        llm_provider, llm_model, llm_temperature, llm_max_tokens,
        llm_extended_thinking, llm_reasoning_effort: Optional LLM overrides
            for this thread. Omit to inherit global settings.
        initial_message: If provided, dispatches this message to the new
            thread and BLOCKS until the child returns its response. The
            child's response becomes part of this tool's output.

    Args (delete mode):
        delete_thread_id: Required for delete. The spawned thread's ID
            (must start with "spawned-"). Only valid if the calling thread
            is the one that originally spawned it.

    Returns (create):
        Preamble with the new thread_id, the callable tool name (if
        make_callable=True), and, if initial_message was provided,
        the child thread's response text.

    Returns (delete):
        "[Deleted]: thread_id=spawned-..." on success.

    Limits (create):
        - Spawn depth capped at 3 by default (NYMERIA_MAX_SPAWN_DEPTH env).
        - 10 spawns per parent per hour (NYMERIA_MAX_SPAWNS_PER_HOUR).
        - instructions max 5000 chars; title truncated to 80 chars.
    """
    from . import ALL_TOOLS, OPTIONAL_TOOLS
    from ..core.agent import get_current_agent
    from ..core.event_bus import publish_sync_event
    from ..core.thread_config import ThreadConfig, ThreadLLMConfig
    from .metadata import get_all_categories, get_category_tools_summary

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

    warnings: List[str] = []
    enabled_set: set = set()

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
                if name in OPTIONAL_TOOLS:
                    enabled_set.add(name)
        if unknown_cats:
            warnings.append(f"unknown categor(ies): {', '.join(unknown_cats)}")

    if optional_tools:
        all_known = {t.name for t in ALL_TOOLS} | set(OPTIONAL_TOOLS.keys())
        unknown_tools: List[str] = []
        for name in optional_tools:
            if name in OPTIONAL_TOOLS:
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
    # the tool_search gate — without this, a non-admin could spawn a child
    # thread seeded with reload_all/claude_code/self_modify and escalate via
    # the child. Block category-expansion AND named optional_tools.
    from . import filter_admin_only_tools, filter_developer_only_tools
    user = agent.accounts_repo.get_user_by_id(user_id) if user_id else None
    user_role = user.role if user else "user"
    _, blocked = filter_admin_only_tools(enabled_set, user_role)
    if blocked:
        return (
            f"[Error]: Admin-only tools cannot be enabled on a spawned thread "
            f"by this user: {sorted(blocked)}. Drop them from optional_tools / "
            f"tool_categories or ask an administrator to spawn the thread."
        )
    _, blocked = filter_developer_only_tools(enabled_set, user_role)
    if blocked:
        return (
            f"[Error]: Developer-only diagnostic tools cannot be enabled on a "
            f"spawned thread by this user: {sorted(blocked)}. Drop them from "
            f"optional_tools / tool_categories or ask an administrator to "
            f"spawn the thread."
        )

    disabled_list: List[str] = []
    if disabled_tools:
        core_tool_names = {t.name for t in ALL_TOOLS}
        unknown_disabled: List[str] = []
        for name in disabled_tools:
            if name in core_tool_names:
                disabled_list.append(name)
            else:
                unknown_disabled.append(name)
        if unknown_disabled:
            warnings.append(
                f"unknown core tool(s) to disable: {', '.join(unknown_disabled)}"
            )

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
        )
    except Exception as e:
        return f"[Error]: Invalid configuration: {str(e)}"

    if not agent.thread_config_manager.save_config(tc):
        return "[Error]: Failed to save thread config."

    platform_meta: Dict[str, str] = {"spawn_depth": str(new_depth)}
    if parent_thread_id:
        platform_meta["spawn_parent"] = parent_thread_id

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
    # them — the parent thread's LLM gets told the new tool exists but can't
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

    preamble_lines = [f"[Spawned]: thread_id={new_thread_id}"]
    if make_callable and callable_name:
        preamble_lines.append(
            f'Callable as: {callable_name}(task="..."). Any thread can invoke this.'
        )
    if tc.enabled_tools:
        preamble_lines.append(
            f"Enabled optional tools: {', '.join(tc.enabled_tools)}"
        )
    if tc.disabled_tools:
        preamble_lines.append(
            f"Disabled core tools: {', '.join(tc.disabled_tools)}"
        )
    if warnings:
        preamble_lines.append(f"[Warning]: {'; '.join(warnings)}")
    preamble_lines.append(
        f'To delete later: spawn_thread(action="delete", '
        f'delete_thread_id="{new_thread_id}")'
    )
    preamble = "\n".join(preamble_lines)

    if not initial_message or not initial_message.strip():
        return preamble

    response = _invoke_spawned(
        agent=agent,
        child_thread_id=new_thread_id,
        parent_thread_id=parent_thread_id,
        title=title,
        task=initial_message.strip(),
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

    from ..core.event_bus import publish_agent_stream_chunk, publish_autonomous_event
    from ..core.stream_bridge import stream_and_collect

    task_id = f"spawned-{uuid.uuid4().hex[:8]}"

    if parent_thread_id:
        agent.register_callable_invocation(parent_thread_id, child_thread_id)

    config_token = var_child_runnable_config.set(None)
    callback_token = tracing_v2_callback_var.set(None)
    collector_token = run_collector_var.set(None)

    try:
        started_published = False

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

        def handle_chunk(chunk: Dict[str, Any], _collection) -> None:
            nonlocal started_published
            if not started_published and chunk.get("type") != "queued":
                publish_autonomous_event(
                    event_type="task_started",
                    thread_id=child_thread_id,
                    user_id=user_id,
                    task_id=task_id,
                    data={"prompt": task, "callable_name": title, "trigger": "spawn_thread"},
                )
                started_published = True

            publish_agent_stream_chunk(
                chunk,
                thread_id=child_thread_id,
                user_id=user_id,
                task_id=task_id,
            )

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
            on_chunk=handle_chunk,
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

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=child_thread_id,
            user_id=user_id,
            task_id=task_id,
            data={"content": response_text, "callable_name": title},
        )

        return response_text or "[Spawned thread returned no content.]"

    except Exception as e:
        logger.error(
            f"spawn_thread dispatch failed for {child_thread_id}: {e}", exc_info=True
        )
        try:
            publish_autonomous_event(
                event_type="task_completed",
                thread_id=child_thread_id,
                user_id=user_id,
                task_id=task_id,
                data={
                    "error": True,
                    "error_message": str(e)[:200],
                    "content": f"Task failed: {str(e)[:200]}",
                    "callable_name": title,
                },
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
