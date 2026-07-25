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
from typing import Any, Dict, List, NamedTuple, Optional, TYPE_CHECKING

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from .agent_history import (
    build_message_timestamp_map,
    format_conversation_history,
    strip_fallback_note,
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
    include_hidden_anchors: bool = False,
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
        include_hidden_anchors: If True, wakeups the show_autonomous_prompts
                                filter would drop are emitted as invisible
                                stub entries (hidden: true) carrying their
                                message_id, so live-attach viewers can trim
                                precisely (backlog #90).

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
            include_hidden_anchors=include_hidden_anchors,
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


class RewindTargetNotFound(LookupError):
    """Raised when a rewind target message id is not a user message in state.

    Distinct from "nothing to remove" so callers can report a stale target
    (e.g. a message removed by compaction) instead of silently clamp-wiping
    or pretending the rewind succeeded.
    """


class RewindResult(NamedTuple):
    """Outcome of a thread rewind."""

    removed: int
    """Underlying graph messages actually deleted."""

    exchanges: int
    """User+assistant exchanges those messages spanned."""


def _resolve_rewind_cut(
    messages: list,
    *,
    steps: Optional[int],
    to_message_id: Optional[str],
) -> Optional[int]:
    """Resolve the index of the first message a rewind should remove.

    ``to_message_id`` wins when provided: the cut lands on the
    ``HumanMessage`` with that id (inclusive), and a missing or non-user id
    raises :class:`RewindTargetNotFound`. Otherwise ``steps`` counts trailing
    exchanges from the end, clamped to what exists. Returns ``None`` when
    there is nothing to remove.
    """
    cycle_starts = [
        i for i, msg in enumerate(messages) if isinstance(msg, HumanMessage)
    ]
    if to_message_id is not None:
        for index in cycle_starts:
            if getattr(messages[index], "id", None) == to_message_id:
                return index
        raise RewindTargetNotFound(
            f"No user message with id '{to_message_id}' in thread state"
        )
    if not cycle_starts or steps is None or steps <= 0:
        return None
    return cycle_starts[-min(steps, len(cycle_starts))]


def rewind_thread(
    agent: "NymeriaAgent",
    thread_id: str,
    *,
    steps: Optional[int] = None,
    to_message_id: Optional[str] = None,
) -> RewindResult:
    """
    Remove trailing exchanges from a thread's message state.

    An exchange starts at a ``HumanMessage`` and includes every following
    ``AIMessage`` / ``ToolMessage`` up to the next ``HumanMessage`` (or the
    end of the list). Two addressing modes:

    - ``steps=N``: remove the last N exchanges (clamped when fewer exist).
      Backs the CLI ``/undo`` and ``/retry`` commands.
    - ``to_message_id``: remove the ``HumanMessage`` with that LangGraph
      message id and everything after it. Exact targeting for the GUI
      edit/rewind affordances; raises :class:`RewindTargetNotFound` when the
      id is absent instead of guessing a count.

    ``to_message_id`` takes precedence when both are given. Uses LangGraph's
    ``RemoveMessage`` + ``update_state`` via the ``add_messages`` reducer,
    the same mechanism as ``trim_context_window``. Unexpected graph errors
    propagate to the caller; use :func:`rewind_thread_exchanges` for the
    legacy swallow-to-zero contract.
    """
    config = {"configurable": {"thread_id": thread_id}}
    state = agent._default_graph.get_state(config)
    messages = state.values.get("messages", [])

    cut_index = _resolve_rewind_cut(
        messages, steps=steps, to_message_id=to_message_id
    )
    if cut_index is None:
        return RewindResult(removed=0, exchanges=0)

    messages_to_remove = messages[cut_index:]
    exchanges = sum(
        1 for msg in messages_to_remove if isinstance(msg, HumanMessage)
    )
    remove_commands = [
        RemoveMessage(id=msg.id)
        for msg in messages_to_remove
        if getattr(msg, "id", None)
    ]
    if not remove_commands:
        return RewindResult(removed=0, exchanges=0)

    agent._default_graph.update_state(
        config,
        {"messages": remove_commands},
    )

    logger.info(
        f"Thread {thread_id}: Rewound {exchanges} exchange(s) "
        f"({len(remove_commands)} messages)"
    )
    return RewindResult(removed=len(remove_commands), exchanges=exchanges)


def rewind_thread_exchanges(
    agent: "NymeriaAgent",
    thread_id: str,
    steps: int = 1,
) -> int:
    """
    Legacy steps-only rewind preserving the swallow-to-zero contract.

    Thin wrapper over :func:`rewind_thread` that returns only the removed
    message count and maps any failure to 0. No production path calls this
    anymore (the rewind API endpoint, which backs CLI /undo and /retry and
    the GUI affordances, uses :func:`rewind_thread` directly); it is kept
    for the original unit-test contract and any out-of-tree callers of the
    matching ``NymeriaAgent`` facade method. New callers should prefer
    :func:`rewind_thread`, which reports exchanges and raises on errors.
    """
    try:
        return rewind_thread(agent, thread_id, steps=steps).removed
    except Exception as e:
        logger.error(f"Error rewinding thread {thread_id}: {e}")
        return 0


def _prompt_text_of(message: HumanMessage) -> str:
    """Extract the user-visible prompt text from a ``HumanMessage``.

    Multimodal prompts store content as a block list; only the text blocks
    matter for a composer restore. The injected time/trigger prefix and any
    hook-injected context are checkpoint-only noise and are stripped, matching
    what history views show the user.
    """
    content = message.content
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part)
    else:
        text = str(content or "")
    # A model-facing fallback note appended to this prompt (a refusal swap
    # that then hit a second refusal and rewound) must never leak into the
    # composer-restored prompt: strip the stamped suffix exactly.
    text = strip_fallback_note(message, text)
    return strip_prompt_context(text).strip()


def _ai_visible_text(message: AIMessage) -> str:
    """User-visible text of an AIMessage (text blocks only, no thinking)."""
    content = message.content
    if isinstance(content, list):
        parts = [
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts).strip()
    return str(content or "").strip()


def refused_turn_tail(messages: list) -> Optional[Dict[str, Any]]:
    """Classify the thread tail when it is an empty provider-refused turn.

    The ``empty_turn_refusal`` marker is stamped by the graph's
    ``_finish_response`` (vendor/react_agent/nodes.py) when a provider refusal
    (Anthropic ``stop_reason="refusal"`` / OpenAI ``finish_reason=
    "content_filter"``) ended the response before any text or tool call.
    Partial-output refusals never carry the marker: the user already saw real
    content, so those turns are not rewound.

    Returns ``None`` when the tail is not a marked refusal, else::

        {
          "rewindable": bool,   # exchange produced NOTHING but the refusal
          "steps": int,         # trailing HumanMessage run length to rewind
          "to_message_id": ...  # FIRST HumanMessage of that run (the anchor
                                # clients truncate from; the per-turn
                                # turn_user_message_id for interactive turns)
          "prompt": str,        # all run prompts joined, composer-restorable
          "model": str,
          "notice": str,        # the in-message notice the graph attached
        }

    Rewindable means the refused AIMessage is immediately preceded by one or
    more consecutive ``HumanMessage``s (a plain prompt, or a queued-prompt
    batch) and nothing else: no tool calls, no ToolMessages, no earlier AI
    output. Anything else in the exchange means the user already saw content
    or side effects already ran (mid-turn tool refusals, DONE-continue
    re-drives, /resume re-drives on a tool tail), so the turn stays in place
    and the notice is the recovery surface (2026-07-24 review decision).
    """
    if not messages:
        return None
    last = messages[-1]
    if not isinstance(last, AIMessage):
        return None
    if not (getattr(last, "additional_kwargs", None) or {}).get(
        "empty_turn_refusal"
    ):
        return None
    model = (getattr(last, "response_metadata", None) or {}).get(
        "model_name"
    ) or ""
    notice = _ai_visible_text(last)
    run: list = []
    for msg in reversed(messages[:-1]):
        if isinstance(msg, HumanMessage):
            run.append(msg)
        else:
            break
    if not run:
        return {
            "rewindable": False,
            "steps": 0,
            "to_message_id": None,
            "prompt": "",
            "model": model,
            "notice": notice,
        }
    run.reverse()
    prompts = [p for p in (_prompt_text_of(m) for m in run) if p]
    return {
        "rewindable": True,
        "steps": len(run),
        "to_message_id": getattr(run[0], "id", None),
        "prompt": "\n\n".join(prompts),
        "model": model,
        "notice": notice,
    }


def refusal_rewind_content(model: str) -> str:
    """Human-readable explanation delivered with a refusal rewind.

    Written to work everywhere it is shown: bots deliver it as the turn's
    reply text, controlled clients (CLI, desktop, mobile) print it alongside
    their composer restore. It therefore never mentions an input box.
    """
    label = model or "The model"
    return (
        f"{label}'s safety classifier declined this turn, so the refused "
        "exchange was rewound: the conversation is back at the end of the "
        "previous turn. Try again with different phrasing; if it still "
        "refuses, rewind further, compact the thread, or switch to a "
        "different model."
    )


def refusal_gated_content(model: str) -> str:
    """Fallback explanation when a refusal is detected but not rewound and
    the in-message notice could not be extracted. Mirrors the two-route
    recovery guide in ``_REFUSAL_NOTICE`` (vendor/react_agent/nodes.py)."""
    label = model or "The model"
    return (
        f"{label} declined this turn (a provider-side refusal) and produced "
        "no reply. Refusals tend to repeat while the triggering content "
        "stays in context: rewind this thread and rephrase; if it still "
        "refuses, rewind further, compact the thread, or switch to a "
        "different model and continue from here."
    )


def maybe_rewind_refused_turn(
    agent: "NymeriaAgent",
    thread_id: str,
    messages: list,
) -> Optional[Dict[str, Any]]:
    """Rewind the trailing exchange when it ended in an empty provider refusal.

    Backlog #105: a pre-output refusal (Fable 5's safety classifiers) leaves a
    poisoned tail; Anthropic's guidance is that the refused turn must be reset
    or refusals tend to repeat. The backend does the reset authoritatively
    here, for every surface (interactive, bots, autonomous), but ONLY when the
    refused exchange produced nothing besides the refusal (``rewindable``
    above): rewinding an exchange that ran tools or delivered earlier content
    would erase things the user saw and side effects that already happened.

    Returns ``None`` when the tail is not a refusal. Otherwise a payload with
    ``"rewound"``:

    - ``True``: the exchange was removed; the caller emits ``turn_rewound``
      (fields: reason/removed/to_message_id/prompt/model/content).
    - ``False`` (gated, rewind error, or nothing removed): the refused
      message stays in place carrying the in-message notice; ``content`` is
      that notice text, which the astream caller delivers as a trailing
      ``response`` chunk because the checkpoint-patched notice never streams
      on its own (bots would otherwise render the turn as silence).
    """
    info = refused_turn_tail(messages)
    if info is None:
        return None
    not_rewound: Dict[str, Any] = {
        "rewound": False,
        "reason": "refusal",
        "model": info["model"],
        "content": info["notice"] or refusal_gated_content(info["model"]),
    }
    if not info["rewindable"]:
        logger.info(
            f"Thread {thread_id}: provider refusal NOT rewound (the refused "
            "exchange carries tool activity or earlier output); the "
            "in-message notice is the recovery surface."
        )
        return not_rewound
    try:
        result = agent.rewind_thread(thread_id, steps=info["steps"])
    except Exception as e:
        logger.warning(
            f"Thread {thread_id}: refusal rewind failed ({e}); leaving the "
            "refused turn in place with its in-message notice."
        )
        return not_rewound
    if result.removed <= 0:
        return not_rewound
    logger.info(
        f"Thread {thread_id}: provider refusal rewound the trailing exchange "
        f"({result.removed} message(s) removed, "
        f"prompts={info['steps']}, model={info['model'] or '?'})"
    )
    return {
        "rewound": True,
        "reason": "refusal",
        "removed": result.removed,
        "to_message_id": info["to_message_id"],
        "prompt": info["prompt"],
        "model": info["model"],
        "content": refusal_rewind_content(info["model"]),
    }


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
                    # Strip time context + any appended fallback note from the
                    # user message (harness context must never be indexed as
                    # the user's words).
                    user_content, _ = extract_content_parts(current_user_msg.content)
                    user_content = strip_prompt_context(user_content)
                    user_content = strip_fallback_note(current_user_msg, user_content)

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
            user_content = strip_fallback_note(current_user_msg, user_content)

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
