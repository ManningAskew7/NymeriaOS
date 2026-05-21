"""Token usage tracking per conversation thread.

Tracks cumulative token usage (input + output) for each thread to determine
when auto-compaction should be triggered.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Optional


@dataclass
class ThreadTokenUsage:
    """Token usage statistics for a conversation thread."""

    thread_id: str
    total_input_tokens: int = 0      # Cumulative (for cost reporting)
    total_output_tokens: int = 0     # Cumulative (for cost reporting)
    last_input_tokens: int = 0       # Latest call's prompt_tokens (= actual context window usage)
    last_output_tokens: int = 0      # Latest call's completion_tokens
    last_compaction_at: Optional[datetime] = None
    compaction_count: int = 0
    # USD cost accounting. ``None`` for ``last_cost_usd`` distinguishes
    # "latest call had no known dollar cost" from "billed at $0.00".
    # ``cost_unavailable`` describes the latest recorded call, so switching
    # away from OAuth-subscription/local-LLM endpoints can resume reporting.
    last_cost_usd: Optional[float] = None
    total_cost_usd: float = 0.0
    cost_unavailable: bool = False
    # High-water message index used by the cost calculator to avoid
    # double-counting AIMessages across ReAct iterations within a single turn.
    last_recorded_message_index: int = 0

    @property
    def total_tokens(self) -> int:
        """Cumulative total — used for cost reporting."""
        return self.total_input_tokens + self.total_output_tokens

    @property
    def context_tokens(self) -> int:
        """Actual context window usage — last call's input tokens."""
        return self.last_input_tokens


class TokenTracker:
    """
    Track token usage per thread for context window monitoring.

    Used by NymeriaAgent to determine when auto-compaction should be triggered
    based on cumulative token usage approaching the model's context limit.
    """

    def __init__(self):
        self._usage: Dict[str, ThreadTokenUsage] = {}

    def record_usage(
        self,
        thread_id: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: Optional[float] = None,
        cost_unavailable: bool = False,
    ) -> None:
        """
        Record token usage for a thread.

        Args:
            thread_id: Thread identifier
            input_tokens: Number of input tokens from this turn
            output_tokens: Number of output tokens from this turn
            cost_usd: USD cost of this turn. ``None`` means rates were
                unavailable; cumulative totals are left unchanged and
                ``last_cost_usd`` is cleared.
            cost_unavailable: True when the thread is routed through an
                OAuth-subscription or local endpoint where pay-per-token
                cost is not meaningful. Sets the latest-call flag and skips
                accumulation, even if ``cost_usd`` happens to be set.
        """
        if thread_id not in self._usage:
            self._usage[thread_id] = ThreadTokenUsage(thread_id=thread_id)
        usage = self._usage[thread_id]
        usage.total_input_tokens += input_tokens
        usage.total_output_tokens += output_tokens
        usage.last_input_tokens = input_tokens    # Overwrite — tracks latest call only
        usage.last_output_tokens = output_tokens
        usage.cost_unavailable = bool(cost_unavailable)
        usage.last_cost_usd = None
        if cost_unavailable:
            return
        if cost_usd is not None:
            usage.last_cost_usd = float(cost_usd)
            usage.total_cost_usd += float(cost_usd)

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

    def reset_after_compact(self, thread_id: str, remaining_tokens: int) -> None:
        """
        Reset token tracking after compaction.

        Args:
            thread_id: Thread identifier
            remaining_tokens: Estimated tokens remaining after compaction
        """
        if thread_id not in self._usage:
            self._usage[thread_id] = ThreadTokenUsage(thread_id=thread_id)

        self._usage[thread_id].total_input_tokens = remaining_tokens
        self._usage[thread_id].total_output_tokens = 0
        self._usage[thread_id].last_input_tokens = remaining_tokens
        self._usage[thread_id].last_output_tokens = 0
        self._usage[thread_id].last_compaction_at = datetime.now()
        self._usage[thread_id].compaction_count += 1

    def clear_thread(self, thread_id: str) -> None:
        """
        Clear token tracking for a thread.

        Args:
            thread_id: Thread identifier
        """
        if thread_id in self._usage:
            del self._usage[thread_id]

    def get_all_threads(self) -> Dict[str, ThreadTokenUsage]:
        """
        Get usage for all tracked threads.

        Returns:
            Dict mapping thread_id to ThreadTokenUsage
        """
        return self._usage.copy()
