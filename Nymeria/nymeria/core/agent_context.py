"""Context-window inspection, trimming, and pre-trim memory flushing.

Extracted from ``NymeriaAgent``. Each function takes the agent instance as
its first argument and is wired back as a thin facade on the class so
external callers (the threads router, CLI transport, ticker,
``agent_compaction``, ``command_service``) keep their existing call shape.

The cluster reads the sync graph's checkpointer state, counts conversation
cycles (one per ``HumanMessage``), decides when the sliding window should
trim, and — when trimming — indexes the about-to-be-removed turns into RAG
so important facts survive deletion.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from .agent_history import (
    build_message_timestamp_map,
    format_conversation_history,
    strip_prompt_context,
)
from .agent_text_extract import extract_content_parts

if TYPE_CHECKING:
    from .agent import NymeriaAgent  # noqa: F401

logger = logging.getLogger(__name__)


def get_conversation_history(
    agent: "NymeriaAgent",
    thread_id: str,
    include_internal: bool = False,
    show_autonomous_prompts: bool = False,
    show_prompt_metadata: bool = False,
) -> List[Dict[str, Any]]:
    """
    Get the conversation history for a thread.

    Formats messages for frontend display:
    - Converts LangChain message types to role-based format
    - Consolidates consecutive AIMessages into "turns" with intermediate_content
    - Embeds tool results into their corresponding tool calls
    - Skips standalone ToolMessages (they're attached to assistant messages)
    - Filters out internal system messages by default (autonomous wake-ups, compaction prompts)

    Args:
        thread_id: Conversation thread ID
        include_internal: If False (default), filters out internal system messages.
                          Set to True for debugging to see all messages.
        show_autonomous_prompts: If True, include autonomous_wakeup prompts
                                 (but still hide compact_prompt/auto_resume).

    Returns:
        List of messages formatted for the frontend
    """
    try:
        state = agent._default_graph.get_state({"configurable": {"thread_id": thread_id}})
        messages = state.values.get("messages", [])

        # Build per-message timestamp map from checkpoint history
        target_ids = {msg.id for msg in messages if msg.id}
        timestamp_map = build_message_timestamp_map(agent._default_graph, thread_id, target_ids)

        formatted = format_conversation_history(
            messages,
            thread_id=thread_id,
            timestamp_map=timestamp_map,
            include_internal=include_internal,
            show_autonomous_prompts=show_autonomous_prompts,
            show_prompt_metadata=show_prompt_metadata,
            clean_tool_result=agent._clean_tool_result_for_display,
            extract_workspace_artifacts=agent._extract_workspace_artifacts,
        )

        _mark_in_flight_tail(agent, thread_id, messages, formatted)
        return formatted

    except Exception as e:
        logger.error(f"Error getting history: {e}")
        return []


def get_raw_checkpoint(agent: "NymeriaAgent", thread_id: str) -> Dict[str, Any]:
    """Dump the latest deserialized LangGraph checkpoint for debugging.

    Unlike :func:`get_conversation_history`, this applies no display projection:
    every message is returned verbatim (tool calls, tool results, metadata) so
    that what is actually persisted can be verified. Only the latest
    ``StateSnapshot`` is returned because its ``messages`` channel already holds
    the full multi-turn history. An unknown thread yields an empty ``messages``
    list (LangGraph returns an empty snapshot rather than raising).
    """
    state = agent._default_graph.get_state({"configurable": {"thread_id": thread_id}})
    messages = state.values.get("messages", [])
    configurable = (state.config or {}).get("configurable", {})
    return {
        "thread_id": thread_id,
        "checkpoint_id": configurable.get("checkpoint_id"),
        "checkpoint_ns": configurable.get("checkpoint_ns", ""),
        "next": list(state.next),
        "config": state.config,
        "metadata": state.metadata,
        "created_at": state.created_at,
        "parent_config": state.parent_config,
        "message_count": len(messages),
        "values": {
            "messages": [m.model_dump(mode="json") for m in messages],
        },
    }


def _thread_is_processing(agent: "NymeriaAgent", thread_id: str) -> bool:
    """True when a turn currently holds the thread lock (mirrors the threads router)."""
    thread_locks = getattr(agent, "_thread_locks", None)
    if thread_locks is None:
        return False
    try:
        return thread_locks.get_lock_info(thread_id) is not None
    except Exception as e:
        logger.warning("Failed to inspect processing state for %s: %s", thread_id, e)
        return False


def _mark_in_flight_tail(
    agent: "NymeriaAgent",
    thread_id: str,
    raw_messages: List[Any],
    formatted: List[Dict[str, Any]],
) -> None:
    """Flag the tail assistant turn as still in-flight so the frontend continues
    its streaming bubble across a thread switch instead of splitting it.

    The frontend cannot tell a mid-turn switch (the displayed tail *is* the turn
    being generated -> continue the bubble) from a back-to-back handoff (the
    displayed tail is a prior reply and a new turn is starting -> fresh bubble),
    because autonomous wake-up inputs are filtered out of history. The backend
    can: mark the tail only when the thread is processing AND the raw checkpoint
    tail is assistant/tool output. A handoff that has queued a new turn but not
    yet produced assistant output leaves a HumanMessage/SystemMessage at the raw
    tail, so it is correctly left unmarked.
    """
    if not formatted or not raw_messages:
        return
    if formatted[-1].get("role") != "assistant":
        return
    if not isinstance(raw_messages[-1], (AIMessage, ToolMessage)):
        return
    if not _thread_is_processing(agent, thread_id):
        return
    formatted[-1]["processing"] = True


def should_reset_context(
    agent: "NymeriaAgent",
    thread_id: str,
    max_cycles: Optional[int] = None,
) -> bool:
    """
    Check if context window is getting too long and needs a reset.

    For long-running autonomous tasks, Nymeria should use a checklist file
    to maintain state, and we start fresh threads periodically.

    Args:
        thread_id: Conversation thread ID
        max_cycles: Number of cycles before reset (defaults to settings.sliding_window_cycles)

    Returns:
        True if context should be reset
    """
    if max_cycles is None:
        max_cycles = agent.settings.sliding_window_cycles

    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = agent._default_graph.get_state(config)
        messages = state.values.get("messages", [])

        # Count cycles (HumanMessage count)
        cycle_count = sum(1 for msg in messages if isinstance(msg, HumanMessage))
        return cycle_count >= max_cycles

    except Exception as e:
        logger.error(f"Error checking context: {e}")
        return False


def get_context_cycle_count(agent: "NymeriaAgent", thread_id: str) -> int:
    """
    Get the current number of conversation cycles in a thread.

    Args:
        thread_id: Conversation thread ID

    Returns:
        Number of cycles (HumanMessage count)
    """
    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = agent._default_graph.get_state(config)
        messages = state.values.get("messages", [])
        return sum(1 for msg in messages if isinstance(msg, HumanMessage))
    except Exception as e:
        logger.error(f"Error getting cycle count: {e}")
        return 0


def trim_context_window(
    agent: "NymeriaAgent",
    thread_id: str,
    max_cycles: Optional[int] = None,
    user_id: str = "default",
) -> int:
    """
    Trim the context window to keep only the last N cycles.

    This implements a sliding window that removes old messages to prevent
    context overflow during long-running autonomous tasks.

    A cycle consists of:
    - 1 HumanMessage
    - 1 or more AIMessage/ToolMessage responses

    Uses LangGraph's RemoveMessage to properly delete messages when
    using the add_messages reducer.

    If RAG is enabled with auto_flush, triggers a memory flush before
    trimming to preserve important facts.

    Args:
        thread_id: Conversation thread ID
        max_cycles: Maximum cycles to keep (defaults to settings.sliding_window_cycles)
        user_id: User ID for RAG memory flush

    Returns:
        Number of messages removed (0 if no trimming needed)
    """
    if max_cycles is None:
        max_cycles = agent.settings.sliding_window_cycles

    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = agent._default_graph.get_state(config)
        messages = state.values.get("messages", [])

        if not messages:
            return 0

        # Count cycles (each HumanMessage starts a new cycle)
        cycle_starts = []
        for i, msg in enumerate(messages):
            if isinstance(msg, HumanMessage):
                cycle_starts.append(i)

        cycle_count = len(cycle_starts)

        # Check if trimming is needed
        if cycle_count <= max_cycles:
            logger.debug(f"Thread {thread_id}: {cycle_count} cycles <= {max_cycles}, no trim needed")
            return 0

        # Calculate how many cycles to remove
        cycles_to_remove = cycle_count - max_cycles

        # Find the index where we should start keeping messages
        # We keep from cycle_starts[cycles_to_remove] onwards
        keep_from_index = cycle_starts[cycles_to_remove]

        # Get messages to remove (everything before keep_from_index)
        messages_to_remove = messages[:keep_from_index]
        messages_removed = len(messages_to_remove)

        # Pre-compaction memory flush: Index messages about to be removed in RAG
        # This preserves important context that would otherwise be lost
        agent._flush_memories_before_trim(
            user_id=user_id,
            thread_id=thread_id,
            messages_to_remove=messages_to_remove,
        )

        logger.info(
            f"Thread {thread_id}: Trimming context window - "
            f"removing {cycles_to_remove} cycles ({messages_removed} messages), "
            f"keeping {max_cycles} cycles"
        )

        # Use RemoveMessage to delete old messages
        # This works with the add_messages reducer
        remove_commands = [RemoveMessage(id=msg.id) for msg in messages_to_remove]

        # Update the state with RemoveMessage commands
        agent._default_graph.update_state(
            config,
            {"messages": remove_commands},
        )

        return messages_removed

    except Exception as e:
        logger.error(f"Error trimming context window for thread {thread_id}: {e}")
        return 0


def rewind_thread_exchanges(
    agent: "NymeriaAgent",
    thread_id: str,
    steps: int = 1,
) -> int:
    """
    Remove the last N user+assistant exchanges from a thread's message state.

    An exchange starts at a ``HumanMessage`` and includes every following
    ``AIMessage`` / ``ToolMessage`` up to the next ``HumanMessage`` (or the end
    of the list). Removing the last N exchanges therefore deletes everything
    from the Nth-from-last ``HumanMessage`` onward, which is what ``/undo``
    (steps=1) and ``/retry`` (steps=1, then re-send) need.

    Uses LangGraph's ``RemoveMessage`` + ``update_state`` via the
    ``add_messages`` reducer, the same mechanism as ``trim_context_window``.
    If the thread has fewer than ``steps`` exchanges, all available cycles
    are removed.

    Args:
        agent: NymeriaAgent instance.
        thread_id: Conversation thread ID.
        steps: Number of trailing exchanges to remove (default 1).

    Returns:
        Number of messages actually removed (0 if nothing to remove).
    """
    if steps <= 0:
        return 0

    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = agent._default_graph.get_state(config)
        messages = state.values.get("messages", [])

        if not messages:
            return 0

        cycle_starts = [
            i for i, msg in enumerate(messages) if isinstance(msg, HumanMessage)
        ]
        if not cycle_starts:
            return 0

        cycles_to_remove = min(steps, len(cycle_starts))
        remove_from_index = cycle_starts[-cycles_to_remove]
        messages_to_remove = messages[remove_from_index:]

        remove_commands = [
            RemoveMessage(id=msg.id)
            for msg in messages_to_remove
            if getattr(msg, "id", None)
        ]
        if not remove_commands:
            return 0

        agent._default_graph.update_state(
            config,
            {"messages": remove_commands},
        )

        logger.info(
            f"Thread {thread_id}: Rewound {cycles_to_remove} exchange(s) "
            f"({len(remove_commands)} messages)"
        )
        return len(remove_commands)

    except Exception as e:
        logger.error(f"Error rewinding thread {thread_id}: {e}")
        return 0


def flush_memories_before_trim(
    agent: "NymeriaAgent",
    user_id: str,
    thread_id: str,
    messages_to_remove: list,
) -> None:
    """
    Index messages about to be trimmed in RAG to preserve important context.

    This runs BEFORE context is trimmed to ensure important facts aren't lost.
    Unlike the original plan's "secret prompt" approach, we directly index
    the conversation turns being removed rather than asking the agent to
    extract facts (which would add latency and be unreliable).

    Args:
        user_id: User identifier
        thread_id: Thread identifier
        messages_to_remove: List of messages about to be removed
    """
    memory_index = agent._get_memory_index(user_id)
    if not memory_index:
        return

    try:
        profile = agent.profile_manager.get_profile(user_id)
        rag_prefs = profile.get_rag_preferences()

        # Check if auto_flush is enabled
        if not rag_prefs.get("auto_flush", True):
            return

        # Index conversation turns from messages being removed
        # Group HumanMessage + following AIMessages into turns
        current_user_msg = None
        current_ai_parts = []

        for msg in messages_to_remove:
            if isinstance(msg, HumanMessage):
                # Skip internal markers (compact prompts, auto-resume, time-context
                # injections). They aren't real user turns and pollute the index.
                if getattr(msg, "additional_kwargs", {}).get("internal"):
                    continue
                # Save previous turn if exists
                if current_user_msg and current_ai_parts:
                    # Strip time context from user message
                    user_content, _ = extract_content_parts(current_user_msg.content)
                    user_content = strip_prompt_context(user_content)

                    turn_content = f"User: {user_content}\n\nAssistant: {' '.join(current_ai_parts)}"
                    memory_index.add_chunk(
                        content=turn_content,
                        metadata={
                            "role": "conversation_turn",
                            "source": "pre_trim_flush",
                        },
                        chunk_type="conversation",
                        user_id=user_id,
                        thread_id=thread_id,
                    )

                # Start new turn
                current_user_msg = msg
                current_ai_parts = []

            elif isinstance(msg, AIMessage):
                # Extract only display text; drop tool_use / image / thinking
                # blocks so structured-content dict reprs and base64 blobs never
                # reach the index (str(msg.content) used to serialize them).
                ai_text, _ = extract_content_parts(msg.content)
                if ai_text and ai_text.strip():
                    current_ai_parts.append(ai_text)

        # Don't forget the last turn
        if current_user_msg and current_ai_parts:
            user_content, _ = extract_content_parts(current_user_msg.content)
            user_content = strip_prompt_context(user_content)

            turn_content = f"User: {user_content}\n\nAssistant: {' '.join(current_ai_parts)}"
            memory_index.add_chunk(
                content=turn_content,
                metadata={
                    "role": "conversation_turn",
                    "source": "pre_trim_flush",
                },
                chunk_type="conversation",
                user_id=user_id,
                thread_id=thread_id,
            )

        logger.debug(f"Pre-trim flush completed for user {user_id}, thread {thread_id}")

    except Exception as e:
        logger.warning(f"Pre-trim memory flush failed: {e}")
