"""Token usage tracking per conversation thread.

Two distinct quantities per thread, kept deliberately separate (they were
conflated before the 2026-07-04 repair pass):

- **Context occupancy** (``last_input_tokens``, exposed as the
  ``context_tokens`` property): the final model call's prompt-side tokens,
  i.e. how full the context window is. Drives auto-compaction triggers and
  the clients' context bars. Reset by compaction and re-estimated by
  ``/prune``.
- **Per-turn consumption** (``turn_input_tokens`` / ``turn_output_tokens``):
  tokens billed by this turn, summed across *all* of the turn's model calls
  (the same message slice the USD cost calculator prices, so the two can
  never diverge). Cumulative totals are sums of turn consumption and survive
  compaction. ``turn_recorded`` distinguishes "this turn recorded 0/0"
  (extraction found nothing) from real zeros, so clients do not accumulate
  stale values.
"""

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional

from .time_utils import utc_now


@dataclass
class ThreadTokenUsage:
    """Token usage statistics for a conversation thread."""

    thread_id: str
    # Cumulative consumption: sums of per-turn billed tokens. Survives
    # compaction; rebuilt best-effort from checkpoint history after restart.
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # Context occupancy: the final model call's prompt_tokens.
    last_input_tokens: int = 0
    # The model whose call (or estimate) produced ``last_input_tokens``.
    # ``get_context_stats`` compares it against the thread's current
    # effective model so a mid-thread model switch can re-estimate
    # occupancy instead of dividing prior-model tokens by the new model's
    # limit (token-audit defect #11). ``None`` means unknown/legacy.
    context_model: Optional[str] = None
    # This turn's consumption, summed across the turn's model calls.
    # ``turn_recorded`` is False when the last finished turn extracted no
    # usage metadata (the turn_* zeros are then "unknown", not "zero").
    turn_input_tokens: int = 0
    turn_output_tokens: int = 0
    turn_recorded: bool = False
    # Wall-clock seconds the turn spent consuming model streams (excludes
    # tool execution and retry backoff). ``None`` when unknown, e.g. a turn
    # whose usage extraction found nothing, or history seeded post-restart.
    turn_llm_seconds: Optional[float] = None
    last_compaction_at: Optional[datetime] = None
    compaction_count: int = 0
    # USD cost accounting. ``None`` for ``last_cost_usd`` distinguishes
    # "latest call had no known dollar cost" from "billed at $0.00".
    # ``cost_unavailable`` describes the latest recorded call, so switching
    # away from OAuth-subscription/local-LLM endpoints can resume reporting.
    last_cost_usd: Optional[float] = None
    total_cost_usd: float = 0.0
    cost_unavailable: bool = False
    # High-water message index shared by the cost calculator and the per-turn
    # token sums: only AIMessages past it belong to the current turn.
    last_recorded_message_index: int = 0

    @property
    def total_tokens(self) -> int:
        """Cumulative consumption total (input + output)."""
        return self.total_input_tokens + self.total_output_tokens

    @property
    def context_tokens(self) -> int:
        """Actual context window usage — last call's input tokens."""
        return self.last_input_tokens


class TokenTracker:
    """
    Track token usage per thread for context window monitoring.

    Used by NymeriaAgent to determine when auto-compaction should be triggered
    based on context occupancy approaching the model's context limit, and to
    surface per-turn and cumulative consumption to clients.

    Mutating methods hold an internal lock; the ``lock`` property is exposed
    for callers that need a read-advance sequence to be atomic against
    concurrent rehydration (e.g. the turn-slice index bump in
    ``NymeriaAgent._compute_turn_usage_and_cost``).
    """

    def __init__(self):
        self._usage: Dict[str, ThreadTokenUsage] = {}
        self._lock = threading.Lock()

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def _row(self, thread_id: str) -> ThreadTokenUsage:
        """Return the persistent usage row, creating it if needed.

        Callers must hold ``self._lock``.
        """
        row = self._usage.get(thread_id)
        if row is None:
            row = ThreadTokenUsage(thread_id=thread_id)
            self._usage[thread_id] = row
        return row

    def record_turn(
        self,
        thread_id: str,
        *,
        turn_input_tokens: int,
        turn_output_tokens: int,
        context_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        cost_unavailable: bool = False,
        turn_llm_seconds: Optional[float] = None,
        context_model: Optional[str] = None,
    ) -> None:
        """
        Record a finished turn.

        Args:
            thread_id: Thread identifier
            turn_input_tokens: Input tokens billed by this turn, summed
                across all of the turn's model calls
            turn_output_tokens: Output tokens generated this turn, summed
                across all of the turn's model calls
            context_tokens: The final model call's prompt-side tokens
                (context occupancy). ``None`` or 0 keeps the previous
                occupancy estimate rather than zeroing the context bar
                (extraction can legitimately find nothing on a turn).
            cost_usd: USD cost of this turn. ``None`` means rates were
                unavailable; cumulative totals are left unchanged and
                ``last_cost_usd`` is cleared.
            cost_unavailable: True when the thread is routed through an
                OAuth-subscription or local endpoint where pay-per-token
                cost is not meaningful. Sets the latest-call flag and skips
                accumulation, even if ``cost_usd`` happens to be set.
            turn_llm_seconds: Wall-clock seconds spent consuming the turn's
                model streams. Stored only for a recorded turn with a
                positive duration; a tokens/s rate needs both sides.
            context_model: The model that produced ``context_tokens``.
                Stamped only when fresh occupancy is written.
        """
        with self._lock:
            usage = self._row(thread_id)
            found = bool(turn_input_tokens or turn_output_tokens)
            usage.turn_input_tokens = turn_input_tokens if found else 0
            usage.turn_output_tokens = turn_output_tokens if found else 0
            usage.turn_recorded = found
            usage.turn_llm_seconds = (
                float(turn_llm_seconds)
                if found and turn_llm_seconds is not None and turn_llm_seconds > 0
                else None
            )
            if found:
                usage.total_input_tokens += turn_input_tokens
                usage.total_output_tokens += turn_output_tokens
            if context_tokens is not None and context_tokens > 0:
                usage.last_input_tokens = context_tokens
                if context_model:
                    usage.context_model = context_model
            usage.cost_unavailable = bool(cost_unavailable)
            usage.last_cost_usd = None
            if cost_unavailable:
                return
            if cost_usd is not None:
                usage.last_cost_usd = float(cost_usd)
                usage.total_cost_usd += float(cost_usd)

    def seed_rehydrated(
        self,
        thread_id: str,
        *,
        total_input_tokens: int,
        total_output_tokens: int,
        context_tokens: int,
        message_index: int,
        context_model: Optional[str] = None,
    ) -> None:
        """Seed a thread's row from checkpoint history after a restart.

        Turn fields stay zeroed with ``turn_recorded=False``: the last
        turn's consumption is unknowable post-restart, and clients must not
        re-accumulate it.
        """
        with self._lock:
            usage = self._row(thread_id)
            usage.total_input_tokens = total_input_tokens
            usage.total_output_tokens = total_output_tokens
            usage.last_input_tokens = context_tokens
            usage.last_recorded_message_index = message_index
            if context_model:
                usage.context_model = context_model

    def set_context_estimate(
        self,
        thread_id: str,
        estimated_tokens: int,
        context_model: Optional[str] = None,
    ) -> None:
        """Replace the context-occupancy estimate (e.g. after ``/prune`` or
        a mid-thread model switch)."""
        with self._lock:
            usage = self._row(thread_id)
            usage.last_input_tokens = max(0, int(estimated_tokens))
            if context_model:
                usage.context_model = context_model

    def get_usage(self, thread_id: str) -> ThreadTokenUsage:
        """
        Get token usage for a thread.

        Args:
            thread_id: Thread identifier

        Returns:
            ThreadTokenUsage for the thread (creates empty one if not found)
        """
        return self._usage.get(thread_id, ThreadTokenUsage(thread_id=thread_id))

    def should_compact(self, thread_id: str, model_limit: int, threshold: float) -> bool:
        """
        Check if a thread should be compacted based on token usage.

        Args:
            thread_id: Thread identifier
            model_limit: Model's context window size in tokens
            threshold: Threshold percentage (0.0-1.0) for triggering compaction

        Returns:
            True if token usage exceeds threshold, False otherwise
        """
        usage = self.get_usage(thread_id)
        return usage.context_tokens >= (model_limit * threshold)

    def reset_after_compact(
        self,
        thread_id: str,
        remaining_tokens: int,
        remaining_message_count: Optional[int] = None,
        context_model: Optional[str] = None,
    ) -> None:
        """
        Reset context occupancy after compaction.

        Cumulative consumption and the last turn's fields survive: compaction
        shrinks the context window, it does not un-consume tokens.

        Args:
            thread_id: Thread identifier
            remaining_tokens: Estimated tokens remaining after compaction
            remaining_message_count: Length of the post-compaction message
                list. When provided, the turn-slice high-water index is reset
                to it so the next turn bills only genuinely new messages
                (the retained tail was already counted before compaction;
                leaving the pre-compaction index in place made the first
                post-compaction turn slice empty and silently drop its cost).
        """
        with self._lock:
            usage = self._row(thread_id)
            usage.last_input_tokens = remaining_tokens
            # Aware UTC, not datetime.now(): this value is serialized straight
            # onto the wire as context_stats.last_compaction, where a naive ISO
            # string silently carried the SERVER's zone and read as UTC to
            # every client.
            usage.last_compaction_at = utc_now()
            usage.compaction_count += 1
            if context_model:
                usage.context_model = context_model
            if remaining_message_count is not None:
                usage.last_recorded_message_index = remaining_message_count

    def clear_thread(self, thread_id: str) -> None:
        """
        Clear token tracking for a thread.

        Args:
            thread_id: Thread identifier
        """
        with self._lock:
            if thread_id in self._usage:
                del self._usage[thread_id]

    def get_all_threads(self) -> Dict[str, ThreadTokenUsage]:
        """
        Get usage for all tracked threads.

        Returns:
            Dict mapping thread_id to ThreadTokenUsage
        """
        with self._lock:
            return self._usage.copy()
