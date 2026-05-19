"""Tool-reload state: turn bookkeeping and resume-message construction.

Extracted from ``NymeriaAgent``. Each function takes the agent instance
as its first argument; thin facades on the class preserve the original
call shape so ``chat()`` and ``chat_stream()`` keep working unchanged.

The cluster handles in-turn tool reload state: clearing stale
pending-reload entries at turn start, formatting human-readable TTL and
source labels for the resume message, and building the synthetic
``HumanMessage`` that re-enters the agent loop after a fresh graph is
built with newly-bound tools.
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from langchain_core.messages import HumanMessage

from .agent import _create_human_message

if TYPE_CHECKING:
    from .agent import NymeriaAgent  # noqa: F401

logger = logging.getLogger(__name__)


def prepare_tool_reload_state_for_turn(
    agent: "NymeriaAgent",
    thread_id: str,
    caller: str,
) -> None:
    """Reset per-turn reload counters and discard stale reload requests.

    A pending reload should only be consumed by the same top-level turn that
    created it. If one is present before a new chat/astream invocation starts,
    it leaked from an older path and must not be applied to the new user
    message.
    """
    pending = getattr(agent, "_pending_tool_reload", None)
    if isinstance(pending, dict):
        stale = pending.pop(thread_id, None)
        if stale:
            logger.warning(
                "%s: discarded stale pending tool reload before new %s turn: %s",
                thread_id,
                caller,
                stale.get("new_tools", []),
            )

    turn_counts = getattr(agent, "_turn_reload_count", None)
    if isinstance(turn_counts, dict):
        turn_counts[thread_id] = 0


def tool_reload_ttl_phrase(agent: "NymeriaAgent", ttl_seconds: Optional[int]) -> str:
    if ttl_seconds is None:
        return "permanently"
    h, rem = divmod(ttl_seconds, 3600)
    m, _ = divmod(rem, 60)
    if h > 0 and m > 0:
        return f"for the next {h}h {m}m"
    if h > 0:
        return f"for the next {h}h"
    return f"for the next {m}m"


def tool_reload_source_label(agent: "NymeriaAgent", reload_info: dict) -> str:
    source = reload_info.get("source") or "tool_search"
    if source == "tool_enable":
        return 'tool_enable(action="enable")'
    if source == "skill_kit":
        skill_name = reload_info.get("skill_name")
        if skill_name:
            return f'Skill Kit "{skill_name}"'
        return "a Skill Kit"
    if source == "mcp_install":
        return "MCP server installation"
    if source == "skill_install":
        skill_name = reload_info.get("skill_name")
        if skill_name:
            return f'skill_manage enabling Skill "{skill_name}"'
        return "skill_manage"
    if source == "skill_kit_create":
        skill_name = reload_info.get("skill_name")
        if skill_name:
            return f'skill_kit_create publishing Skill Kit "{skill_name}"'
        return "skill_kit_create"
    if source == "skill_config":
        skill_name = reload_info.get("skill_name")
        if skill_name:
            return f'skill_config publishing Skill Kit "{skill_name}"'
        return "skill_config"
    if source == "tool_create":
        return "tool_create publishing a new tool"
    return 'tool_enable(action="enable")'


def create_tool_reload_resume_message(
    agent: "NymeriaAgent",
    reload_info: dict,
) -> HumanMessage:
    new_tools = reload_info.get("new_tools", [])
    ttl_key = reload_info.get("ttl", "2h")
    ttl_seconds = reload_info.get("ttl_seconds")
    source = reload_info.get("source") or "tool_search"
    skill_name = reload_info.get("skill_name")
    reason = reload_info.get("reason")
    source_label = agent._tool_reload_source_label(reload_info)
    reason_text = f" Reason: {reason}." if reason else ""
    ttl_phrase = agent._tool_reload_ttl_phrase(ttl_seconds)
    if new_tools:
        resume_text = (
            f"[System: tool reload complete. The following tools are now "
            f"bound to you {ttl_phrase}: {', '.join(new_tools)}. This is "
            f"the automatic resume after {source_label}.{reason_text} "
            "Continue the user's original task now; you may call these "
            "newly-loaded tools in this resumed step.]"
        )
    else:
        resume_text = (
            f"[System: capability reload complete. The thread's skill "
            f"list and tool schemas have been refreshed. This is the "
            f"automatic resume after {source_label}.{reason_text} "
            "Continue the user's original task now.]"
        )
    resume_msg = _create_human_message(
        resume_text,
        internal=True,
        internal_type="tool_reload_resume",
    )
    resume_msg.additional_kwargs["tool_reload_tools"] = new_tools
    resume_msg.additional_kwargs["tool_reload_ttl"] = ttl_key
    resume_msg.additional_kwargs["tool_reload_source"] = source
    if skill_name:
        resume_msg.additional_kwargs["tool_reload_skill_name"] = skill_name
    if reason:
        resume_msg.additional_kwargs["tool_reload_reason"] = reason
    resume_msg.additional_kwargs["tool_reload_ttl_seconds"] = ttl_seconds
    return resume_msg
