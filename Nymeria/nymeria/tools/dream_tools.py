"""Tools that are only useful inside a dream cycle.

Each tool here refuses to run unless the calling thread's ``ThreadConfig`` has
``shadow_parent_id`` set — i.e. it is a shadow thread spawned by the dream
scheduler. This keeps the surface area unreachable from normal user threads
even if a user manually enabled the tool name.

The parent thread is the *target* of every operation: ``thread_instructions_set``
writes to the parent's ``ThreadConfig.instructions``, not the shadow's own.
"""
from .registry import ToolGroup, register_tool_group

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_thread_id_or_none, get_user_id

logger = logging.getLogger(__name__)

MAX_INSTRUCTIONS_CHARS = 5000


def _resolve_parent_thread(config: Optional[RunnableConfig]):
    """Return (agent, parent_thread_id, error_message).

    Caller checks ``error_message`` first; if truthy, the other fields are None.
    """
    from ..core.agent import get_current_agent

    agent = get_current_agent()
    if agent is None:
        return None, None, "[Error]: No active agent; cannot resolve dream context."

    shadow_thread_id = get_thread_id_or_none(config)
    if not shadow_thread_id:
        return None, None, "[Error]: No thread context; this tool is dream-only."

    tc = agent.thread_config_manager.get_config(shadow_thread_id)
    if tc is None or not tc.shadow_parent_id:
        return None, None, (
            "[Error]: This tool only runs in a shadow (dream) thread. "
            "The current thread has no ``shadow_parent_id`` set."
        )

    return agent, tc.shadow_parent_id, None


@tool
def thread_instructions_set(
    new_text: str,
    change_summary: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Overwrite the parent thread's per-thread instructions (appended to soul.md).

    Dream-only. Refuses to run unless called from a shadow thread whose
    ``ThreadConfig.shadow_parent_id`` points at a real parent. The full
    ``new_text`` replaces any existing instructions atomically — include
    everything you want to keep.

    Args:
        new_text: Replacement text for the parent's
            ``ThreadConfig.instructions`` field. Max 5000 chars. Empty string
            clears the field entirely.
        change_summary: One-sentence description of what you changed and why.
            Surfaces in the dream summary card. Required.

    Returns:
        "[Saved]: Updated thread instructions (<N> chars). Summary: <text>"
        on success. "[Error]: <reason>" on failure.
    """
    if not isinstance(new_text, str):
        return "[Error]: new_text must be a string."
    if not isinstance(change_summary, str) or not change_summary.strip():
        return "[Error]: change_summary is required and must be non-empty."
    if len(new_text) > MAX_INSTRUCTIONS_CHARS:
        return (
            f"[Error]: new_text exceeds {MAX_INSTRUCTIONS_CHARS} chars "
            f"(got {len(new_text)}). Tighten the text and try again."
        )

    agent, parent_thread_id, err = _resolve_parent_thread(config)
    if err:
        return err
    user_id = get_user_id(config)

    parent_tc = agent.thread_config_manager.get_config(parent_thread_id)
    if parent_tc is None:
        from ..core.thread_config import ThreadConfig

        parent_tc = ThreadConfig(thread_id=parent_thread_id)

    parent_tc.instructions = new_text if new_text else None

    if not agent.thread_config_manager.save_config(parent_tc):
        return (
            f"[Error]: Failed to save updated instructions for "
            f"thread {parent_thread_id}."
        )

    try:
        agent.invalidate_thread_config_cache(parent_thread_id)
    except Exception as e:
        logger.warning(
            "thread_instructions_set: cache invalidate failed for %s: %s",
            parent_thread_id,
            e,
        )

    try:
        from ..core.event_bus import publish_sync_event

        publish_sync_event(
            event_type="thread_config_updated",
            thread_id=parent_thread_id,
            user_id=user_id,
            data={
                "source": "dream",
                "fields": ["instructions"],
                "change_summary": change_summary.strip(),
            },
        )
    except Exception as e:
        logger.debug(
            "thread_instructions_set: sync event publish failed for %s: %s",
            parent_thread_id,
            e,
        )

    logger.info(
        "Dream updated instructions for thread %s (%d chars). Summary: %s",
        parent_thread_id,
        len(new_text),
        change_summary.strip()[:200],
    )
    return (
        f"[Saved]: Updated thread instructions ({len(new_text)} chars). "
        f"Summary: {change_summary.strip()}"
    )


DREAM_TOOLS = [thread_instructions_set]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="dream", tools=tuple(DREAM_TOOLS)))
