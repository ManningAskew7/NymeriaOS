"""Context-window usage stats and token-usage rehydration.

Extracted from ``NymeriaAgent``. Two free functions take the agent
instance as their first argument; ``NymeriaAgent`` keeps thin
facade methods so external callers (chat router, bots, CLI,
command_service, agent_compaction) continue to use
``agent.get_context_stats(thread_id)`` and
``agent._rehydrate_token_usage(thread_id)`` unchanged.

Distinct from ``agent_context.py``, which handles context-window
trimming + RAG indexing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, TYPE_CHECKING

from langchain_core.messages import AIMessage

from ..config.model_capabilities import get_context_limit
from .token_usage import extract_from_message, extract_last_from_messages

if TYPE_CHECKING:
    from .agent import NymeriaAgent  # noqa: F401

logger = logging.getLogger(__name__)


def rehydrate_token_usage(agent: "NymeriaAgent", thread_id: str) -> None:
    """
    Estimate token usage from checkpoint messages when tracker has no data.

    This handles the case where the server was restarted and the in-memory
    token tracker is empty, but the thread has conversation history in the
    checkpoint database with usage metadata. Also seeds the cumulative USD
    cost from the persisted ``ThreadMetadata.total_cost_usd_micros``.
    """
    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = agent._default_graph.get_state(config)
        messages = state.values.get("messages", [])

        total_input = 0
        total_output = 0
        last_input = 0
        last_output = 0

        for msg in messages:
            if not isinstance(msg, AIMessage):
                continue
            inp, out = extract_from_message(msg)
            total_input += inp
            total_output += out

        last_input, last_output = extract_last_from_messages(messages)

        if total_input or total_output:
            # Record with last-call values (sets both last_* and adds to cumulative)
            agent._token_tracker.record_usage(thread_id, last_input, last_output)
            # Patch cumulative totals to reflect full history
            usage = agent._token_tracker.get_usage(thread_id)
            usage.total_input_tokens = total_input
            usage.total_output_tokens = total_output
            # Reset the cost high-water index so new turns post-restart only
            # bill messages added from this point forward; the cumulative
            # total comes from persisted metadata below.
            usage.last_recorded_message_index = len(messages)
            logger.debug(f"Rehydrated token usage for thread {thread_id}: cumulative={total_input}+{total_output}, last_call={last_input}+{last_output}")

        _rehydrate_cost_from_metadata(agent, thread_id)
    except Exception as e:
        logger.debug(f"Could not rehydrate token usage for thread {thread_id}: {e}")


def _rehydrate_cost_from_metadata(agent: "NymeriaAgent", thread_id: str) -> None:
    """Seed the in-memory cumulative cost from persisted ThreadMetadata."""
    try:
        manager = getattr(agent, "thread_metadata_manager", None)
        if manager is None:
            return
        # ThreadMetadata is keyed per-user; we don't know the owner from
        # thread_id alone. Walk known users and pick the first hit.
        for path in manager.metadata_dir.glob("*.json"):
            user_id = path.stem
            meta = manager.get_thread(user_id, thread_id)
            if meta is None:
                continue
            micros = getattr(meta, "total_cost_usd_micros", 0) or 0
            if micros > 0:
                usage = agent._token_tracker.get_usage(thread_id)
                # Ensure the usage row exists in the tracker so the seeded
                # cost survives the read.
                agent._token_tracker._usage.setdefault(thread_id, usage)
                usage.total_cost_usd = float(micros) / 1_000_000.0
            return
    except Exception as exc:  # noqa: BLE001 - best-effort.
        logger.debug("Cost rehydration skipped for %s: %s", thread_id, exc)


def get_context_stats(agent: "NymeriaAgent", thread_id: str) -> Dict[str, Any]:
    """
    Get context window usage statistics for a thread.

    Args:
        thread_id: Thread identifier

    Returns:
        Dict with context stats
    """
    usage = agent._token_tracker.get_usage(thread_id)

    # If tracker has no data for this thread, try rehydrating from checkpoint.
    # Route through agent._rehydrate_token_usage(...) (the facade) -- NOT the
    # free function directly -- to preserve monkey-patch seams in tests that
    # patch NymeriaAgent._rehydrate_token_usage.
    if usage.context_tokens == 0 and usage.total_tokens == 0:
        agent._rehydrate_token_usage(thread_id)
        usage = agent._token_tracker.get_usage(thread_id)

    # Use per-thread effective model for correct context limit calculation
    llm_config = agent._get_llm_config_for_thread(thread_id)
    effective_model = llm_config.model
    model_limit = get_context_limit(effective_model)
    context_used = usage.context_tokens  # Last call's prompt_tokens = actual window usage

    return {
        "thread_id": thread_id,
        "model": effective_model,
        "total_tokens": context_used,
        "input_tokens": usage.last_input_tokens,
        "output_tokens": usage.last_output_tokens,
        "cumulative_tokens": usage.total_tokens,
        "context_limit": model_limit,
        "usage_percentage": round(context_used / model_limit * 100, 1) if model_limit else 0,
        "compaction_count": usage.compaction_count,
        "last_compaction": usage.last_compaction_at.isoformat() if usage.last_compaction_at else None,
        "context_management": agent.settings.context_management,
        "cost_usd_last": usage.last_cost_usd,
        "cost_usd_cumulative": round(usage.total_cost_usd, 6),
        "cost_unavailable": usage.cost_unavailable,
    }
