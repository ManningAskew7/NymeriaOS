"""Shared helpers for same-turn tool reload routing."""

from __future__ import annotations

from typing import Optional, Sequence, Union

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


def should_emit_reload_command(
    new_tool_names: Sequence[str],
    *,
    thread_id: Optional[str] = None,
) -> bool:
    """Decide whether a tool-enable call site should emit Command(goto=END).

    Rebuild mode: always True — the graph must rebuild to bind new tools.
    Dynamic mode: always False. The model node rebinds tools per step from the
    live resolver, and SafeToolNode refreshes its dispatch table from the same
    resolver before rejecting a post-build tool call. This makes newly enabled
    and newly created tools callable in the next agent step without a
    graph-rebuild/resume round trip.

    Defensive fall-through: if the current agent is unavailable, return True
    so the legacy reload path still triggers.
    """
    from .agent import get_current_agent

    agent = get_current_agent()
    if agent is None:
        return True
    # Read flag live from settings (not a cached instance attribute) so a
    # PATCH /settings flip takes effect on the very next reload-decision.
    settings = getattr(agent, "settings", None)
    if not bool(getattr(settings, "dynamic_tool_binding", False)):
        return True
    return False


def command_or_text(
    text: str,
    queued_reload: bool,
    tool_call_id: Optional[str],
    new_tool_names: Optional[Sequence[str]] = None,
    thread_id: str = "",
) -> Union[str, Command]:
    """Emit ``Command(goto=END)`` only when a same-turn graph rebuild is required.

    Returns the plain ``text`` unless a reload is queued, a ``tool_call_id`` is
    available, and ``should_emit_reload_command`` says the rebuild must happen
    (legacy rebuild mode). In dynamic-binding mode the next agent step resolves
    tools and skill metadata from the live resolver, so the plain text is
    returned and the graph keeps running. Skill-only changes pass an empty
    ``new_tool_names``.

    This is the canonical owner of the reload-gating decision that tool, skill,
    and MCP enable paths share; callers route through it instead of re-deriving
    the same short-circuit rule.
    """
    if queued_reload and tool_call_id and should_emit_reload_command(
        new_tool_names or [],
        thread_id=thread_id,
    ):
        return tool_reload_command(text, tool_call_id)
    return text
