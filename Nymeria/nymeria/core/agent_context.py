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

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from .agent_history import (
    CONTEXT_PREFIX_PATTERN,
    build_message_timestamp_map,
    format_conversation_history,
)

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

        return format_conversation_history(
            messages,
            thread_id=thread_id,
            timestamp_map=timestamp_map,
            include_internal=include_internal,
            show_autonomous_prompts=show_autonomous_prompts,
            show_prompt_metadata=show_prompt_metadata,
            clean_tool_result=agent._clean_tool_result_for_display,
            extract_workspace_artifacts=agent._extract_workspace_artifacts,
        )

    except Exception as e:
        logger.error(f"Error getting history: {e}")
        return []


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
        flush_memories_before_trim(
            agent,
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
                    user_content = current_user_msg.content if isinstance(current_user_msg.content, str) else str(current_user_msg.content)
                    user_content = CONTEXT_PREFIX_PATTERN.sub('', user_content)

                    turn_content = f"User: {user_content}\n\nAssistant: {''.join(current_ai_parts)}"
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
                if msg.content:
                    current_ai_parts.append(msg.content if isinstance(msg.content, str) else str(msg.content))

        # Don't forget the last turn
        if current_user_msg and current_ai_parts:
            user_content = current_user_msg.content if isinstance(current_user_msg.content, str) else str(current_user_msg.content)
            user_content = CONTEXT_PREFIX_PATTERN.sub('', user_content)

            turn_content = f"User: {user_content}\n\nAssistant: {''.join(current_ai_parts)}"
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
