"""Tool reload + hot-loading for ``NymeriaAgent``.

This module owns the "register, sync, reload, prune" half of the tool lifecycle
— the cluster that used to live as ~440 lines inside ``NymeriaAgent``
(formerly ``agent.py`` lines 2240–2715). It covers:

- callable-thread tool sync (``sync_agent_tools``)
- graph-cache invalidation for thread-config changes
- TTL eviction of temporary thread tools
- custom-tool loader bookkeeping
- MCP server tool registration + binding pruning
- shared default-graph rebuilds after any registry mutation
- hot-reload of the ``nymeria.tools`` package (``reload_tools``)
- per-user ``default_thread_tools`` sync on core-tool diff

All functions take ``agent`` as the first argument and read/write state on
``agent`` directly. ``NymeriaAgent`` keeps thin facade methods (e.g.
``agent.sync_agent_tools()``) that delegate here, which preserves every
external caller — ``run.py``, the API routers, ``command_service``,
``thread_deletion``, ``triggers/cli`` commands, and ``agent_graph``'s
``_resolve_temporary_tools`` callout.

Cross-module imports (``get_callable_thread_tools``, ``SEED_TOOLS``,
``get_custom_tool_loader``, ``get_mcp_server_registry``,
``reload_mcp_server_registry``, MCP metadata helpers,
``mark_tool_search_dirty``) stay inside function bodies — they are lazy in
the originals to avoid circular imports and we preserve that here.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List

from ..vendor.react_agent import ToolRegistry
from .time_utils import ensure_aware_utc, utc_now

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)


def sync_agent_tools(agent: "NymeriaAgent") -> List[str]:
    """Sync callable thread tools into the tool registry.

    Rebuilds the registry with SEED_TOOLS + callable thread tools + custom tools.
    Call this after creating/deleting callable threads.

    Note: per-user graph builds source callable thread tools directly from
    the per-user-filtered ``thread_config_manager`` (see
    ``_build_graph_with_prompt``). ``_callable_tool_thread_map`` remains a
    legacy fallback for timeout hooks that run without runnable config;
    normal timeout handling resolves against the current user/thread scope.

    Returns:
        List of callable thread tool names now in the registry
    """
    from ..agents.tool_factory import get_callable_thread_tools
    from ..tools import SEED_TOOLS

    # Get callable thread tools (from threads with callable=True)
    thread_tools = get_callable_thread_tools(agent.thread_config_manager)
    thread_tool_names = {t.name for t in thread_tools}

    # Rebuild callable tool -> thread_id map (for auto-abort on timeout)
    # Build locally then assign atomically so readers never see a partial map
    new_map: Dict[str, str] = {}
    for tc in agent.thread_config_manager.list_callable_threads():
        if tc.callable_name:
            new_map[tc.callable_name] = tc.thread_id
    agent._callable_tool_thread_map = new_map

    # Rebuild the tool registry: core + callable thread tools
    combined = list(SEED_TOOLS) + thread_tools
    agent.tool_registry = ToolRegistry()
    agent.tool_registry.register_all(combined)

    # Re-register custom tools
    agent._load_custom_tools()

    # Re-register MCP server tools
    agent._load_mcp_server_tools()

    agent._rebuild_default_graphs()
    try:
        from .tool_search_index import mark_tool_search_dirty

        mark_tool_search_dirty()
    except Exception:
        logger.debug("Failed to mark tool search index dirty", exc_info=True)

    all_names = list(thread_tool_names)
    logger.info(f"Synced agent tools: {all_names} ({len(thread_tools)} callable threads, {len(combined)} total tools)")
    return all_names


def invalidate_thread_config_cache(agent: "NymeriaAgent", thread_id: str) -> None:
    """Remove cached graphs for a specific thread after its config changes."""
    with agent._graph_cache_lock:
        keys_to_remove = [k for k in agent._user_graphs if k[1] == thread_id]
        for k in keys_to_remove:
            del agent._user_graphs[k]
        keys_to_remove = [
            k
            for k in agent._async_user_graphs
            if (k[2] if len(k) > 2 else k[1]) == thread_id
        ]
        for k in keys_to_remove:
            del agent._async_user_graphs[k]
    logger.debug(f"Invalidated graph cache for thread {thread_id}")


def resolve_temporary_tools(agent: "NymeriaAgent", tc) -> set:
    """Evict expired TTL'd tool entries, persist, and return the live set.

    Runs at graph-build time only. Tools that were live when the current
    graph was built stay callable for the whole invocation — no surprise
    mid-turn eviction.
    """
    if tc is None or not getattr(tc, "temporary_tools", None):
        return set()
    now = utc_now()
    live = {
        name: entry
        for name, entry in tc.temporary_tools.items()
        if ensure_aware_utc(entry.expires_at) > now
    }
    if len(live) != len(tc.temporary_tools):
        evicted = set(tc.temporary_tools) - set(live)
        logger.info(
            f"Thread {tc.thread_id}: TTL evicting {len(evicted)} tool(s): "
            f"{', '.join(sorted(evicted))}"
        )
        tc.temporary_tools = live
        agent.thread_config_manager.save_config(tc)
    return set(live.keys())


def load_custom_tools(agent: "NymeriaAgent") -> int:
    """Load custom tools from the custom_tools directory.

    Returns:
        Number of custom tools loaded.
    """
    try:
        from .custom_tools import get_custom_tool_loader

        agent._custom_tool_loader = get_custom_tool_loader()
        old_custom_names = set(getattr(agent._custom_tool_loader, "_tools", {}).keys())
        for name in old_custom_names:
            agent.tool_registry.unregister(name)
        custom_tools = agent._custom_tool_loader.load_all()

        if custom_tools:
            agent.tool_registry.register_all(custom_tools)
            logger.info(f"Loaded {len(custom_tools)} custom tool(s)")

        return len(custom_tools)
    except Exception as e:
        logger.error(f"Failed to load custom tools: {e}", exc_info=True)
        return 0


def unregister_existing_mcp_tools(agent: "NymeriaAgent") -> set[str]:
    """Remove previously registered dynamic MCP wrappers from the live registry."""
    existing = {
        tool.name
        for tool in agent.tool_registry.get_all_tools()
        if getattr(tool, "name", "").startswith("mcp__")
    }
    for name in existing:
        agent.tool_registry.unregister(name)
    return existing


def prune_mcp_tool_bindings(agent: "NymeriaAgent", live_tool_names: set[str]) -> int:
    """Remove unavailable MCP tool names from defaults and thread configs."""
    removed = 0

    try:
        for user_id in agent.profile_manager.list_users():
            with agent.profile_manager.atomic_update(user_id) as profile:
                defaults = profile.tool_preferences.default_thread_tools
                if defaults is None:
                    continue
                filtered = [
                    name
                    for name in defaults
                    if not name.startswith("mcp__") or name in live_tool_names
                ]
                removed += len(defaults) - len(filtered)
                profile.tool_preferences.default_thread_tools = filtered
    except Exception:
        logger.debug("Failed to prune MCP defaults", exc_info=True)

    try:
        for thread_id in agent.thread_config_manager.list_configured_threads():
            tc = agent.thread_config_manager.get_config(thread_id)
            if tc is None:
                continue
            original_enabled = list(tc.enabled_tools)
            original_disabled = list(tc.disabled_tools)
            original_temporary = dict(tc.temporary_tools)
            tc.enabled_tools = [
                name
                for name in tc.enabled_tools
                if not name.startswith("mcp__") or name in live_tool_names
            ]
            tc.disabled_tools = [
                name
                for name in tc.disabled_tools
                if not name.startswith("mcp__") or name in live_tool_names
            ]
            tc.temporary_tools = {
                name: entry
                for name, entry in tc.temporary_tools.items()
                if not name.startswith("mcp__") or name in live_tool_names
            }
            removed += len(original_enabled) - len(tc.enabled_tools)
            removed += len(original_disabled) - len(tc.disabled_tools)
            removed += len(original_temporary) - len(tc.temporary_tools)
            if (
                tc.enabled_tools != original_enabled
                or tc.disabled_tools != original_disabled
                or tc.temporary_tools != original_temporary
            ):
                agent.thread_config_manager.save_config(tc)
                if hasattr(agent, "_graph_cache_lock"):
                    agent.invalidate_thread_config_cache(tc.thread_id)
    except Exception:
        logger.debug("Failed to prune MCP thread bindings", exc_info=True)

    if removed:
        logger.info("Pruned %d stale MCP tool binding(s)", removed)
    return removed


def load_mcp_server_tools(agent: "NymeriaAgent") -> int:
    """Load MCP server tools from the mcp_servers directory.

    Returns:
        Number of MCP server tools loaded.
    """
    try:
        from .mcp_servers import get_mcp_server_registry
        from .mcp_tool_names import format_mcp_tool_name
        from ..tools.metadata import (
            clear_mcp_server_tool_metadata,
            register_mcp_server_tool_metadata,
        )

        registry = get_mcp_server_registry()
        agent._unregister_existing_mcp_tools()
        mcp_tools = registry.get_all_tools()
        live_names = {tool.name for tool in mcp_tools}

        # Metadata covers every discovered tool across every installed
        # server — even ones whose defn.enabled is False — so that the
        # frontend's per-tool toggle list validates against the full set.
        # defn.enabled still gates whether the tool is actually callable
        # (via get_all_tools()'s filter), but a name not yet "live" should
        # still be a known name the defaults endpoint will accept.
        clear_mcp_server_tool_metadata()
        for defn in registry.get_all_servers():
            for dt in defn.discovered_tools:
                tool_name = format_mcp_tool_name(defn.id, dt.name)
                register_mcp_server_tool_metadata(
                    tool_name,
                    dt.description,
                    live=tool_name in live_names,
                    enabled=bool(defn.enabled),
                    server_id=defn.id,
                    install_status=defn.install_status,
                )
        agent._prune_mcp_tool_bindings(live_names)

        if mcp_tools:
            agent.tool_registry.register_all(mcp_tools)
            logger.info(f"Loaded {len(mcp_tools)} MCP server tool(s)")

        return len(mcp_tools)
    except Exception as e:
        logger.error(f"Failed to load MCP server tools: {e}", exc_info=True)
        return 0


def reload_mcp_server_tools(agent: "NymeriaAgent") -> List[str]:
    """Reload MCP server tools and rebuild graphs.

    Returns:
        List of MCP server tool names loaded.
    """
    try:
        from .mcp_servers import reload_mcp_server_registry
        from .mcp_tool_names import format_mcp_tool_name
        from ..tools.metadata import (
            clear_mcp_server_tool_metadata,
            register_mcp_server_tool_metadata,
        )

        registry = reload_mcp_server_registry()
        agent._unregister_existing_mcp_tools()
        mcp_tools = registry.get_all_tools()
        live_names = {tool.name for tool in mcp_tools}

        # See _load_mcp_server_tools: register metadata for every
        # discovered tool regardless of defn.enabled, so the UI's defaults
        # validation accepts names for installed-but-not-yet-enabled
        # servers.
        clear_mcp_server_tool_metadata()
        for defn in registry.get_all_servers():
            for dt in defn.discovered_tools:
                tool_name = format_mcp_tool_name(defn.id, dt.name)
                register_mcp_server_tool_metadata(
                    tool_name,
                    dt.description,
                    live=tool_name in live_names,
                    enabled=bool(defn.enabled),
                    server_id=defn.id,
                    install_status=defn.install_status,
                )
        agent._prune_mcp_tool_bindings(live_names)

        # Re-register tools
        if mcp_tools:
            agent.tool_registry.register_all(mcp_tools)

        agent._rebuild_default_graphs()
        try:
            from .tool_search_index import mark_tool_search_dirty

            mark_tool_search_dirty()
        except Exception:
            logger.debug("Failed to mark tool search index dirty", exc_info=True)

        tool_names = [t.name for t in mcp_tools]
        logger.info(f"MCP server tools reloaded: {tool_names}")
        return tool_names
    except Exception as e:
        logger.error(f"Failed to reload MCP server tools: {e}", exc_info=True)
        return []


def rebuild_default_graphs(agent: "NymeriaAgent") -> None:
    """Drop cached per-thread graphs and rebuild the shared defaults.

    Call after any mutation that could change the tool set visible to a
    graph build: default_thread_tools saved, MCP server enable/install/
    delete, custom-tool reload. Subsequent thread messages rebuild their
    graph lazily from the up-to-date registry + profile.
    """
    agent._user_graphs.clear()
    agent._async_user_graphs.clear()
    agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
    agent._default_async_graph = agent._build_async_graph_with_prompt(agent._base_system_prompt)


def reload_custom_tools(agent: "NymeriaAgent") -> List[str]:
    """Reload only custom tools.

    This is lighter weight than reload_tools() and only affects
    custom tool definitions, not built-in tools.

    Returns:
        List of custom tool names loaded.
    """
    try:
        from .custom_tools import get_custom_tool_loader

        loader = get_custom_tool_loader()
        old_custom_names = set(getattr(loader, "_tools", {}).keys())
        for name in old_custom_names:
            agent.tool_registry.unregister(name)
        custom_tools = loader.load_all()

        # Re-register custom tools (they replace existing ones with same name)
        if custom_tools:
            agent.tool_registry.register_all(custom_tools)

        agent._rebuild_default_graphs()
        try:
            from .tool_search_index import mark_tool_search_dirty

            mark_tool_search_dirty()
        except Exception:
            logger.debug("Failed to mark tool search index dirty", exc_info=True)

        tool_names = [t.name for t in custom_tools]
        logger.info(f"Custom tools reloaded: {tool_names}")
        return tool_names

    except Exception as e:
        logger.error(f"Failed to reload custom tools: {e}", exc_info=True)
        return []


def reload_tools(agent: "NymeriaAgent") -> List[str]:
    """Hot-reload all tools from the tools module.

    This re-imports all tools (picking up any new files) and rebuilds the agent's
    graphs so new tools become available on the NEXT message turn.

    New core tools (added to SEED_TOOLS) are automatically registered in each
    user's default_thread_tools so they appear as enabled by default.  Removed
    core tools are cleaned out of the list as well.

    NOTE: Due to how LangGraph works, newly created tools are NOT available
    in the same conversation turn. The current turn's graph was captured at
    the start of the turn. New tools will work on the next user message.

    Returns:
        List of tool names now available
    """
    from .. import tools as tools_module

    logger.info("Reloading tools module...")

    # Snapshot current SEED_TOOLS before reload (for diff)
    old_core_names = {
        t.name for t in getattr(tools_module, 'SEED_TOOLS', [])
    }

    # Get all submodule names (including newly created files)
    tools_path = Path(tools_module.__file__).parent
    submodules = [name for _, name, _ in pkgutil.iter_modules([str(tools_path)])]
    logger.info(f"Found tool submodules on disk: {submodules}")

    # Process each submodule - reload existing, import new
    for submod_name in submodules:
        full_name = f"nymeria.tools.{submod_name}"
        if full_name in sys.modules:
            # Existing module - reload it
            try:
                importlib.reload(sys.modules[full_name])
                logger.debug(f"Reloaded existing: {full_name}")
            except Exception as e:
                logger.warning(f"Failed to reload {full_name}: {e}")
        else:
            # New module - import it
            try:
                importlib.import_module(full_name)
                logger.info(f"Imported new module: {full_name}")
            except Exception as e:
                logger.warning(f"Failed to import new module {full_name}: {e}")

    # Reload the main tools module (__init__.py) to pick up new exports
    importlib.reload(tools_module)
    from ..tools.metadata import refresh_builtin_tool_metadata

    # Get SEED_TOOLS directly from the reloaded module object
    # (using 'from ..tools import SEED_TOOLS' could get cached references)
    SEED_TOOLS = getattr(tools_module, 'SEED_TOOLS', [])
    refresh_builtin_tool_metadata()
    new_core_names = {t.name for t in SEED_TOOLS}
    logger.info(f"SEED_TOOLS after reload: {list(new_core_names)}")

    # Auto-sync default_thread_tools for all users
    agent._sync_default_thread_tools(old_core_names, new_core_names)

    # Get callable thread tools
    from ..agents.tool_factory import get_callable_thread_tools
    thread_tools = get_callable_thread_tools(agent.thread_config_manager)

    # Clear and re-register all tools (core + callable thread tools)
    combined_tools = list(SEED_TOOLS) + thread_tools
    agent.tool_registry = ToolRegistry()
    agent.tool_registry.register_all(combined_tools)

    # Reload custom tools as well
    custom_count = agent._load_custom_tools()
    logger.info(f"Reloaded {custom_count} custom tool(s)")

    agent._rebuild_default_graphs()
    try:
        from .tool_search_index import mark_tool_search_dirty

        mark_tool_search_dirty()
    except Exception:
        logger.debug("Failed to mark tool search index dirty", exc_info=True)

    tool_list = agent.tool_registry.list_tools()
    tool_names = [t["name"] for t in tool_list]
    logger.info(f"Tools reloaded successfully. Available ({len(tool_names)}): {tool_names}")
    return tool_names


def sync_default_thread_tools(
    agent: "NymeriaAgent", old_core: set, new_core: set
) -> None:
    """Sync each user's default_thread_tools after a core tool change.

    - Newly added core tools are appended so they're enabled by default.
    - Removed core tools are cleaned out to avoid stale entries.
    - Users whose default_thread_tools is None (legacy mode) are skipped.
    """
    from ..tools import CAPABILITY_EXPANSION_TOOL_NAMES

    added = new_core - old_core
    removed = (old_core - new_core) | set(CAPABILITY_EXPANSION_TOOL_NAMES)
    if not added and not removed:
        return

    if added:
        logger.info(f"New core tools detected: {added}")
    if removed:
        logger.info(f"Removed core tools detected: {removed}")

    for user_id in agent.profile_manager.list_users():
        try:
            profile = agent.profile_manager.get_profile(user_id)
            dt = profile.tool_preferences.default_thread_tools
            if dt is None:
                continue  # legacy mode — no explicit list to update

            current = set(dt)
            updated = (current | added) - removed
            if updated != current:
                with agent.profile_manager.atomic_update(user_id) as p:
                    p.tool_preferences.default_thread_tools = sorted(updated)
                logger.info(
                    f"Updated default_thread_tools for user {user_id}: "
                    f"+{added & updated} -{removed & current}"
                )
        except Exception as e:
            logger.warning(
                f"Failed to sync default_thread_tools for {user_id}: {e}"
            )
