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


def rebuild_tool_registry(agent: "NymeriaAgent") -> List:
    """THE registry assembly: swap in a fresh ToolRegistry from every source.

    Order: seed + callable-thread tools land on a FRESH registry, then the
    custom-tool and MCP loaders re-register their slices into it. Shared by
    ``sync_agent_tools`` and ``reload_tools`` so the two can never drift
    again: #277 happened because ``reload_tools`` grew its own assembly that
    dropped every MCP wrapper tool. ``NymeriaAgent.__init__`` mirrors this
    sequence inline (constructor ordering: the registry must exist before
    other init steps run); keep the two in step.

    Also rebuilds ``_callable_tool_thread_map`` so the timeout-hook fallback
    map stays consistent with the callable tools just registered.

    Returns:
        The callable-thread tool objects registered (for callers that report
        callable names).
    """
    from .. import tools as tools_module
    from ..agents.tool_factory import get_callable_thread_tools

    # Read SEED_TOOLS off the module object so a just-reloaded module wins
    # over any cached from-import reference.
    seed_tools = list(getattr(tools_module, "SEED_TOOLS", []))
    thread_tools = get_callable_thread_tools(agent.thread_config_manager)

    # Rebuild callable tool -> thread_id map (for auto-abort on timeout).
    # Build locally then assign atomically so readers never see a partial map.
    new_map: Dict[str, str] = {}
    for tc in agent.thread_config_manager.list_callable_threads():
        if tc.callable_name:
            new_map[tc.callable_name] = tc.thread_id
    agent._callable_tool_thread_map = new_map

    agent.tool_registry = ToolRegistry()
    agent.tool_registry.register_all(seed_tools + thread_tools)

    # Loaders register into the fresh registry the agent now holds.
    agent._load_custom_tools()
    agent._load_mcp_server_tools()
    return thread_tools


def sync_agent_tools(agent: "NymeriaAgent") -> List[str]:
    """Sync callable thread tools into the tool registry.

    Rebuilds the registry via ``rebuild_tool_registry`` (seed + callable +
    custom + MCP). Call this after creating/deleting callable threads.

    Note: per-user graph builds source callable thread tools directly from
    the per-user-filtered ``thread_config_manager`` (see
    ``_build_graph_with_prompt``). ``_callable_tool_thread_map`` remains a
    legacy fallback for timeout hooks that run without runnable config;
    normal timeout handling resolves against the current user/thread scope.

    Returns:
        List of callable thread tool names now in the registry
    """
    thread_tools = rebuild_tool_registry(agent)

    agent._rebuild_default_graphs()
    try:
        from .tool_search_index import mark_tool_search_dirty

        mark_tool_search_dirty()
    except Exception:
        logger.debug("Failed to mark tool search index dirty", exc_info=True)

    all_names = [t.name for t in thread_tools]
    logger.info(
        f"Synced agent tools: {all_names} ({len(thread_tools)} callable "
        f"threads, {len(agent.tool_registry.list_tools())} total tools)"
    )
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


def resolve_temporary_tools(agent: "NymeriaAgent", tc, *, persist: bool = True) -> set:
    """Evict expired TTL'd tool entries, persist, and return the live set.

    Runs at graph-build time. Tools that were live when the current graph was
    built stay callable for the whole invocation, no surprise mid-turn
    eviction.

    ``persist=False`` computes the same live set WITHOUT the eviction write,
    for callers that only need to report what is live. Same idiom as
    ``_require_thread_access(claim=False)`` and ``resolve_team_ref(adopt=False)``:
    a read must not be a write. The read-only thread overview reaches this
    through ``select_tools_for_graph`` and was rewriting the config file from a
    stale snapshot on every CLI header repaint.
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
        if persist:
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
                    server_name=defn.name,
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
                    server_name=defn.name,
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
    delete, custom-tool reload, an LLM-credential change, a skill toggle,
    or a base-system-prompt reload. Subsequent thread messages rebuild their
    graph lazily from the up-to-date registry + profile.

    The two cache clears run under ``_graph_cache_lock`` because the lazy
    build path (``store_cached_graph_entry``) mutates the same dicts under
    that lock, including an LRU ``next(iter(cache))`` + ``del`` eviction that a
    concurrent unlocked ``clear()`` could turn into a ``KeyError`` or
    ``RuntimeError: dictionary changed size``. The default-graph rebuilds stay
    outside the lock: they do not touch the cache dicts, and holding the
    non-reentrant lock across graph construction is both unnecessary and the
    shape the careful callers already used.
    """
    with agent._graph_cache_lock:
        agent._user_graphs.clear()
        agent._async_user_graphs.clear()
    agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
    agent._default_async_graph = agent._build_async_graph_with_prompt(agent._base_system_prompt)


def sync_external_resource_edits(agent: "NymeriaAgent") -> None:
    """Propagate raw on-disk edits of tool stores into the agent runtime.

    Slice 2 of the resource-filesystem-layout plan: the custom-tool loader
    and MCP registry detect external file edits themselves (debounced
    fingerprint scans in ``refresh_if_stale``, see
    ``storage_paths.compare_fingerprint``); this chokepoint, called on
    every graph lookup, drains their pending-sync state and re-runs the same
    registry re-registration + graph rebuild the authoring tools run, so a
    raw edit reaches the next graph build exactly like a ``tool_create``
    edit. Skills need no step here: their manager reads through to disk and
    the skills fingerprint feeds the graph cache key directly.

    Non-blocking and re-entrancy safe: if another thread is already syncing
    (or this thread re-enters via a graph rebuild), skip; the next lookup
    picks up whatever remains.
    """
    lock = getattr(agent, "_external_sync_lock", None)
    if lock is None or not lock.acquire(blocking=False):
        return
    try:
        from .custom_tools import get_custom_tool_loader
        from .mcp_servers import get_mcp_server_registry

        rebuild_needed = False

        loader = get_custom_tool_loader()
        loader.refresh_if_stale()
        pending = loader.drain_registry_sync()
        if pending:
            for tool_id in sorted(pending):
                agent.tool_registry.unregister(tool_id)
                fresh = loader.get_tool(tool_id)
                if fresh is not None:
                    agent.tool_registry.register(fresh)
            rebuild_needed = True
            logger.info(
                "External custom-tool edits synced into the registry: %s",
                sorted(pending),
            )

        registry = get_mcp_server_registry()
        registry.refresh_if_stale()
        if registry.drain_registry_sync():
            load_mcp_server_tools(agent)
            rebuild_needed = True
            logger.info("External MCP server edits synced into the registry")

        # Team entity store (backlog #100): the graph depends only on
        # membership (config-side), never on team name or memory, so an
        # external store edit needs no graph rebuild; the tool search index
        # tags callables with the team name, so re-tag it. Name reads go
        # through the manager's fingerprint cache and are fresh already.
        team_manager = getattr(agent, "team_manager", None)
        if team_manager is not None and team_manager.poll_external_changes():
            try:
                from .tool_search_index import mark_tool_search_dirty

                mark_tool_search_dirty()
                logger.info(
                    "External team-store edits detected; tool search re-tag scheduled"
                )
            except Exception:
                logger.debug("Failed to mark tool search index dirty", exc_info=True)

        if rebuild_needed:
            agent._rebuild_default_graphs()
            try:
                from .tool_search_index import mark_tool_search_dirty

                mark_tool_search_dirty()
            except Exception:
                logger.debug("Failed to mark tool search index dirty", exc_info=True)
    except Exception:  # noqa: BLE001 - freshness must never break a turn
        logger.exception("External resource-edit sync failed")
    finally:
        lock.release()


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


def _reload_tools_package() -> None:
    """importlib-reload every ``nymeria.tools`` submodule, then the package.

    Picks up brand-new module files and re-executes existing ones. Safe
    against the #277 wipe because every cross-module accumulator in the
    package is reload-survivable (see the ``_TOOL_GROUPS`` comment in
    ``tools/registry.py``); the consequence is that a module DELETED from
    disk keeps its registrations until process restart, which is fine:
    deleting builtin tool modules is a deploy operation, not a runtime one.
    """
    from .. import tools as tools_module

    # Get all submodule names (including newly created files)
    tools_path = Path(tools_module.__file__).parent
    submodules = [name for _, name, _ in pkgutil.iter_modules([str(tools_path)])]
    logger.info(f"Found tool submodules on disk: {submodules}")

    # Process each submodule - reload existing, import new
    for submod_name in submodules:
        full_name = f"nymeria.tools.{submod_name}"
        if full_name in sys.modules:
            try:
                importlib.reload(sys.modules[full_name])
                logger.debug(f"Reloaded existing: {full_name}")
            except Exception as e:
                logger.warning(f"Failed to reload {full_name}: {e}")
        else:
            try:
                importlib.import_module(full_name)
                logger.info(f"Imported new module: {full_name}")
            except Exception as e:
                logger.warning(f"Failed to import new module {full_name}: {e}")

    # Reload the main tools module (__init__.py) to pick up new exports
    importlib.reload(tools_module)


def _reapply_environment_descriptions(agent: "NymeriaAgent") -> None:
    """Re-run the boot-time environment-aware description pass.

    A package reload re-creates the seed/catalog tool objects, so the
    runtime facts boot stamped into shell/file tool descriptions would
    otherwise silently vanish from the fresh objects (#277 rider).
    """
    env = getattr(agent, "execution_environment", None)
    if env is None:
        return
    try:
        from .. import tools as tools_module
        from ..tools.execution_environment import (
            configure_environment_aware_tool_descriptions,
        )

        seed = list(getattr(tools_module, "SEED_TOOLS", []))
        catalog = getattr(tools_module, "CATALOG_TOOLS", {})
        configure_environment_aware_tool_descriptions(
            [*seed, *catalog.values()], env
        )
    except Exception as e:  # noqa: BLE001 - description sugar must not break a reload
        logger.warning("Environment description re-apply failed (non-fatal): %s", e)


def reload_tools(agent: "NymeriaAgent") -> List[str]:
    """Hot-reload all tools from the tools module.

    This re-imports all tools (picking up any new files), refreshes builtin
    metadata and environment-aware descriptions, rebuilds the full registry
    via ``rebuild_tool_registry`` (seed + callable + custom + MCP), and
    rebuilds the agent's graphs so changes land on the NEXT message turn.

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

    _reload_tools_package()

    from ..tools.metadata import refresh_builtin_tool_metadata

    # Get SEED_TOOLS directly from the reloaded module object
    # (using 'from ..tools import SEED_TOOLS' could get cached references)
    SEED_TOOLS = getattr(tools_module, 'SEED_TOOLS', [])
    refresh_builtin_tool_metadata()
    new_core_names = {t.name for t in SEED_TOOLS}
    logger.info(f"SEED_TOOLS after reload: {sorted(new_core_names)}")

    # Auto-sync default_thread_tools for all users
    agent._sync_default_thread_tools(old_core_names, new_core_names)

    _reapply_environment_descriptions(agent)

    rebuild_tool_registry(agent)

    agent._rebuild_default_graphs()
    try:
        from .tool_search_index import mark_tool_search_dirty

        mark_tool_search_dirty()
    except Exception:
        logger.debug("Failed to mark tool search index dirty", exc_info=True)

    tool_names = [t["name"] for t in agent.tool_registry.list_tools()]
    catalog_count = len(getattr(tools_module, "CATALOG_TOOLS", {}))
    try:
        from ..tools.registry import all_tool_groups

        group_count = len(all_tool_groups())
    except Exception:  # noqa: BLE001
        group_count = -1
    logger.info(
        "Tools reloaded. registry=%d catalog=%d groups=%d",
        len(tool_names),
        catalog_count,
        group_count,
    )
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
    removed_core = old_core - new_core
    # Capability-expansion tools are hot-loaded on demand and never belong in
    # a default_thread_tools list, so they ride the strip set as janitorial
    # policy. They are reported only by the per-user update line below:
    # announcing them as "Removed core tools" on every reload was pure noise
    # and buried the real diagnosis in the #277 incident.
    removed = removed_core | set(CAPABILITY_EXPANSION_TOOL_NAMES)

    if added:
        logger.info(f"New core tools detected: {sorted(added)}")
    if removed_core:
        logger.info(f"Removed core tools detected: {sorted(removed_core)}")

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
