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


def _seed_cost_from_meta(agent: "NymeriaAgent", thread_id: str, meta: Any) -> bool:
    """Seed the tracker's cumulative cost from one ThreadMetadata row.

    Returns True when a positive cost was found and seeded (so the caller can
    stop scanning); a missing or zero cost returns False.
    """
    micros = getattr(meta, "total_cost_usd_micros", 0) or 0
    if micros <= 0:
        return False
    usage = agent._token_tracker.get_usage(thread_id)
    # Ensure the usage row exists in the tracker so the seeded cost survives
    # the read.
    agent._token_tracker._usage.setdefault(thread_id, usage)
    usage.total_cost_usd = float(micros) / 1_000_000.0
    return True


def _rehydrate_cost_from_metadata(agent: "NymeriaAgent", thread_id: str) -> None:
    """Seed the in-memory cumulative cost from persisted ThreadMetadata.

    Fast path: read the registered thread owner's metadata directly (one DB
    lookup + one file read). Fall back to scanning every user's metadata file
    when the owner is unknown, has no metadata for this thread, or has a zero
    recorded cost: turn cost is persisted under the turn's user_id, which is
    not guaranteed to equal the registered owner (callable, dream-shadow,
    shared, or reassigned threads). The fallback reproduces the original
    "first metadata row wins" scan exactly.
    """
    try:
        manager = getattr(agent, "thread_metadata_manager", None)
        if manager is None:
            return

        # Fast path: go straight to the registered owner's metadata file.
        accounts_repo = getattr(agent, "accounts_repo", None)
        owner = None
        if accounts_repo is not None:
            try:
                owner = accounts_repo.get_thread_owner(thread_id)
            except Exception:  # noqa: BLE001 - owner lookup is best-effort.
                owner = None
        if owner is not None:
            meta = manager.get_thread(owner, thread_id)
            if meta is not None and _seed_cost_from_meta(agent, thread_id, meta):
                return

        # Fallback: scan all users' metadata and stop at the first hit.
        for path in manager.metadata_dir.glob("*.json"):
            user_id = path.stem
            meta = manager.get_thread(user_id, thread_id)
            if meta is None:
                continue
            _seed_cost_from_meta(agent, thread_id, meta)
            return
    except Exception as exc:  # noqa: BLE001 - best-effort.
        logger.debug("Cost rehydration skipped for %s: %s", thread_id, exc)


def record_turn_usage(
    agent: "NymeriaAgent",
    thread_id: str,
    user_id: str,
    messages: list,
) -> tuple[int, int, bool]:
    """Record a finished turn's token usage and USD cost.

    Extracts input/output tokens and the per-turn cost from ``messages``, then
    (only when there is something to record) updates the in-memory token tracker
    and the persisted per-thread cost ledger. Factored out of the three
    byte-identical copies that lived inline in ``chat`` and ``astream`` (the
    success and error/overflow paths).

    Returns ``(input_tokens, output_tokens, recorded)`` where ``recorded`` is
    True iff something was recorded, so the astream-success caller can gate its
    debug log on the exact same condition the recording used.
    """
    input_tok, output_tok = agent._extract_tokens_from_response(messages)
    llm_config_for_cost = agent._get_llm_config_for_thread(thread_id)
    cost_usd, cost_unavailable = agent._compute_turn_cost(
        thread_id, messages, llm_config_for_cost
    )
    recorded = bool(input_tok or output_tok or cost_usd is not None or cost_unavailable)
    if recorded:
        agent._token_tracker.record_usage(
            thread_id,
            input_tok,
            output_tok,
            cost_usd=cost_usd,
            cost_unavailable=cost_unavailable,
        )
        agent._record_turn_cost(thread_id, user_id, cost_usd, cost_unavailable)
    return input_tok, output_tok, recorded


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
