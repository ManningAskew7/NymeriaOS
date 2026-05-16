"""Shared helpers for same-turn tool reload routing."""

from __future__ import annotations

from typing import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langgraph.graph import END
from langgraph.types import Command


TOOL_RELOAD_QUEUED_KEY = "nymeria_tool_reload_queued"


def mark_tool_reload_message(message: ToolMessage) -> ToolMessage:
    """Mark a tool result as the boundary that should trigger graph reload."""
    message.additional_kwargs[TOOL_RELOAD_QUEUED_KEY] = True
    return message


def tool_reload_command(content: str, tool_call_id: str) -> Command:
    """Return a graph-ending command with a marked ToolMessage update."""
    return Command(
        goto=END,
        update={
            "messages": [
                mark_tool_reload_message(
                    ToolMessage(content=content, tool_call_id=tool_call_id)
                )
            ]
        },
    )


def latest_tool_batch_queued_reload(messages: Sequence[BaseMessage]) -> bool:
    """Return True when the latest contiguous tool-result batch queued reload."""
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            if message.additional_kwargs.get(TOOL_RELOAD_QUEUED_KEY) is True:
                return True
            continue
        if isinstance(message, (AIMessage, HumanMessage)):
            return False
    return False


def should_emit_reload_command(new_tool_names: Sequence[str]) -> bool:
    """Decide whether a tool-enable call site should emit Command(goto=END).

    Rebuild mode: always True — the graph must rebuild to bind new tools.
    Dynamic mode: True only when a newly-enabled tool is NOT in the current
    graph's precomputed tool superset (e.g., a tool just created by
    tool_create or a freshly-installed MCP). For tools already in the
    superset, the model node rebinds them on its next invocation without
    a graph rebuild, so a plain-string return is sufficient.

    Defensive fall-through: if the agent or its superset attribute is
    unavailable, return True so the legacy reload path still triggers.
    """
    from .agent import get_current_agent

    agent = get_current_agent()
    if agent is None:
        return True
    if not getattr(agent, "_dynamic_tool_binding", False):
        return True
    superset = getattr(agent, "_current_tool_superset_names", None)
    if not superset:
        return True
    return not all(name in superset for name in new_tool_names)
