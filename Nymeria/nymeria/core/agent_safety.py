"""Turn-safety inspection: iteration limits, runaway-tool detection, and stop events.

Extracted from ``NymeriaAgent``. Each function takes the agent instance as
its first argument when it needs class state (constants like
``TURN_SAME_TOOL_RESULT_LIMIT``, ``MAIN_AGENT_MAX_ITERATIONS``, or the
``thread_config_manager``); stateless helpers take only their direct
inputs. Thin facades on the class preserve the original call shape so
``chat()``, ``chat_stream()``, and the test-time monkey-patches keep
working unchanged.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage

from ..vendor.react_agent.nodes import (
    TURN_SAFETY_REASON_MAX_ITERATIONS,
    TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
    TurnSafetyResult,
    analyze_turn_safety as _graph_analyze_turn_safety,
)

if TYPE_CHECKING:
    from .agent import NymeriaAgent  # noqa: F401

logger = logging.getLogger(__name__)


def count_current_turn_tool_calls(messages: List) -> int:
    """Count tool-call AI messages since the most recent HumanMessage."""
    current_turn_messages = []
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            break
        current_turn_messages.append(msg)

    return sum(
        len(msg.tool_calls) for msg in current_turn_messages
        if isinstance(msg, AIMessage) and msg.tool_calls
    )


def check_iteration_limit_hit(
    agent: "NymeriaAgent",
    messages: List,
    max_iterations: int,
) -> bool:
    """Check if routing stopped because the turn exceeded max_iterations."""
    return agent._analyze_turn_safety(messages, max_iterations).should_stop


def analyze_turn_safety(
    agent: "NymeriaAgent",
    messages: List,
    max_iterations: int,
) -> TurnSafetyResult:
    """Return turn safety status using the same logic as the graph router."""
    return _graph_analyze_turn_safety(
        messages,
        max_iterations=max_iterations,
        repeated_tool_result_limit=agent.TURN_SAME_TOOL_RESULT_LIMIT,
    )


def recursion_limit_for_iterations(max_iterations: int) -> int:
    """LangGraph recursion must have room for agent/tool node pairs."""
    return max(150, (max_iterations * 2) + 25)


def main_iterations_cap(agent: "NymeriaAgent") -> int:
    """Resolve the main-agent iteration cap.

    ``settings.agent_max_iterations`` wins when available; the class constant
    is the fallback for agents constructed without settings (tests, stubs).
    Only genuine int/str values count, so mock settings objects fall through.
    """
    settings = getattr(agent, "settings", None)
    raw = getattr(settings, "agent_max_iterations", None)
    configured = 0
    if isinstance(raw, int) and not isinstance(raw, bool):
        configured = raw
    elif isinstance(raw, str):
        try:
            configured = int(raw)
        except ValueError:
            configured = 0
    if configured > 0:
        return configured
    return agent.MAIN_AGENT_MAX_ITERATIONS


def max_iterations_for_thread(
    agent: "NymeriaAgent",
    thread_id: Optional[str],
) -> int:
    if not thread_id:
        return main_iterations_cap(agent)
    try:
        tc = agent.thread_config_manager.get_config(thread_id)
        if tc and tc.callable and tc.callable_name:
            return tc.callable_max_iterations or agent.CALLABLE_DEFAULT_MAX_ITERATIONS
    except Exception as e:
        logger.debug(f"Could not resolve max iterations for thread {thread_id}: {e}")
    return main_iterations_cap(agent)


def get_effective_sequential_tools(
    global_default: bool,
    thread_id: Optional[str],
    *,
    thread_config_manager: Any | None = None,
) -> bool:
    """Resolve sequential (ordered, one-at-a-time) tool execution for a thread.

    Per-thread ``sequential_tool_execution`` override if set (True/False), else
    the global default. Mirrors ``image_limits.get_effective_image_window_size``:
    injectable manager, never raises (a config-lookup failure falls back to the
    global default so it cannot break a turn).
    """
    if not thread_id or thread_config_manager is None:
        return global_default
    try:
        tc = thread_config_manager.get_config(thread_id)
    except Exception:  # noqa: BLE001 - never let config lookup break a turn
        return global_default
    override = getattr(tc, "sequential_tool_execution", None) if tc else None
    if override is None:
        return global_default
    return bool(override)


def get_effective_hook_enabled(
    definition: Any,
    thread_id: Optional[str],
    *,
    thread_config_manager: Any | None = None,
    settings: Any | None = None,
) -> bool:
    """Resolve whether one hook definition is enabled for a thread.

    Precedence (highest first): the master kill switch (per-thread
    ``hooks_enabled`` override, else global ``Settings.hooks_enabled``) -> the
    per-thread per-hook override (``ThreadConfig.hook_overrides[id]``) -> the
    hook's own ``enabled`` default. Mirrors ``get_effective_sequential_tools``:
    never raises (a config-lookup failure falls back safely so it cannot break a
    turn).
    """
    master = bool(getattr(settings, "hooks_enabled", True)) if settings is not None else True
    tc = None
    if thread_id and thread_config_manager is not None:
        try:
            tc = thread_config_manager.get_config(thread_id)
        except Exception:  # noqa: BLE001 - never let config lookup break a turn
            tc = None
    if tc is not None:
        thread_master = getattr(tc, "hooks_enabled", None)
        if thread_master is not None:
            master = bool(thread_master)
    if not master:
        return False
    if tc is not None:
        override = (getattr(tc, "hook_overrides", None) or {}).get(getattr(definition, "id", None))
        if override is not None:
            return bool(override)
    return bool(getattr(definition, "enabled", True))


def graph_run_config(
    agent: "NymeriaAgent",
    thread_id: str,
    user_id: str,
    callbacks: Optional[List[Any]] = None,
    *,
    hook_is_autonomous: Optional[bool] = None,
    hook_holder_kind: Optional[str] = None,
    hook_trigger_label: Optional[str] = None,
) -> Dict[str, Any]:
    max_iterations = agent._max_iterations_for_thread(thread_id)
    settings = getattr(agent, "settings", None)
    sequential_tools = get_effective_sequential_tools(
        bool(getattr(settings, "sequential_tool_execution", False)),
        thread_id,
        thread_config_manager=getattr(agent, "thread_config_manager", None),
    )
    config: Dict[str, Any] = {
        "recursion_limit": agent._recursion_limit_for_iterations(max_iterations),
        "configurable": {
            "thread_id": thread_id,
            "user_id": user_id,
            "sequential_tools": sequential_tools,
            # When on, SafeToolNode appends each tool result's server-measured
            # duration to the text the model sees (global setting; per-turn read).
            "tool_timing_in_results": bool(
                getattr(settings, "tool_timing_in_results", False)
            ),
        },
    }
    # Thread the turn source through to tool-event hooks. ``_build_tool_hook_ctx``
    # (nodes.py) reads these off ``configurable`` so a PRE/POST tool hook can scope
    # by autonomous-vs-interactive / holder / trigger. Stamped only when the caller
    # knows the source (the graph-run sites), so read-only/no-source callers leave
    # the config unchanged.
    if hook_is_autonomous is not None:
        config["configurable"]["hook_is_autonomous"] = bool(hook_is_autonomous)
    if hook_holder_kind is not None:
        config["configurable"]["hook_holder_kind"] = hook_holder_kind
    if hook_trigger_label is not None:
        config["configurable"]["hook_trigger_label"] = hook_trigger_label
    # Stamp the per-turn hook registry so the tool node (which only sees the run
    # config) can dispatch this thread's enabled hooks. Only when non-None, so a
    # turn with no enabled hooks leaves the config byte-identical to before.
    # Resolved via getattr (like settings/thread_config_manager above) so a
    # minimal facade agent without the helper degrades to no hooks, not a crash.
    resolve_hooks = getattr(agent, "_hook_registry_for_turn", None)
    hook_registry = resolve_hooks(thread_id, user_id) if callable(resolve_hooks) else None
    if hook_registry is not None:
        config["configurable"]["hook_registry"] = hook_registry
        # Context-usage signal for tool-event hooks: the model window and the
        # resolved auto-compact trigger are turn-stable, so stamp them once
        # here; occupancy is stamped as the turn-entry fallback and refreshed
        # mid-turn by ``_build_tool_hook_ctx`` from the latest AIMessage usage.
        # Computed ONLY when a hook registry exists, so the zero-hook hot path
        # stays byte-identical.
        stats_fn = getattr(agent, "_hook_context_stats", None)
        stats = stats_fn(thread_id) if callable(stats_fn) else None
        if isinstance(stats, dict):
            config["configurable"]["hook_context_tokens"] = stats.get("context_tokens")
            config["configurable"]["hook_context_limit"] = stats.get("context_limit")
            config["configurable"]["hook_compact_trigger_tokens"] = stats.get(
                "compact_trigger_tokens"
            )
    if callbacks is not None:
        config["callbacks"] = callbacks
    return config


def turn_safety_content(agent: "NymeriaAgent", safety: TurnSafetyResult) -> str:
    if safety.reason == TURN_SAFETY_REASON_REPEATED_TOOL_RESULT:
        tool_name = safety.repeated_tool_name or "a tool"
        repeat_count = safety.repeated_count or agent.TURN_SAME_TOOL_RESULT_LIMIT
        return (
            f"Stopped because `{tool_name}` was called with the same arguments "
            f"and returned the same result {repeat_count} times in a row. "
            "This looks like a runaway tool loop, so I stopped before running it again."
        )

    return (
        f"I reached the maximum number of steps "
        f"({safety.max_iterations}) and had to stop. "
        "My task may be incomplete. You can ask me to continue where I left off."
    )


def turn_safety_event(
    agent: "NymeriaAgent",
    safety: TurnSafetyResult,
    scope: str = "main_agent",
) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "type": "iteration_limit",
        "scope": scope,
        "reason": safety.reason or TURN_SAFETY_REASON_MAX_ITERATIONS,
        "content": agent._turn_safety_content(safety),
        "max_iterations": safety.max_iterations,
        "tool_call_count": safety.tool_call_count,
    }
    if safety.repeated_tool_name:
        event["repeated_tool_name"] = safety.repeated_tool_name
    if safety.repeated_count:
        event["repeated_count"] = safety.repeated_count
    return event
