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
