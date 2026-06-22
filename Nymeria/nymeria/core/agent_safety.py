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


def graph_run_config(
    agent: "NymeriaAgent",
    thread_id: str,
    user_id: str,
    callbacks: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    max_iterations = agent._max_iterations_for_thread(thread_id)
    config: Dict[str, Any] = {
        "recursion_limit": agent._recursion_limit_for_iterations(max_iterations),
        "configurable": {"thread_id": thread_id, "user_id": user_id},
    }
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
