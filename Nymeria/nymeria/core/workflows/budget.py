"""Workflow run budgets: one object carries every cap.

Plan: ``docs/private/plans/workflow-tools.md`` (WorkflowBudget table). Defaults
are module constants for phase 1; per-definition overrides clamped by settings
arrive with the persistence phase. The per-verb timeout defaults to the node
``tool_timeout`` so a workflow tool call is never more patient than a direct
tool call would be.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_WALL_CLOCK_SECONDS = 600
DEFAULT_MAX_CALLS = 50
DEFAULT_MAX_AI_CALLS = 10
DEFAULT_MAX_DEPTH = 2
DEFAULT_STATE_CAP_BYTES = 256 * 1024
# Mirrors run_command's _RUN_COMMAND_READ_CAP: what we RETAIN of child logs.
DEFAULT_LOG_CAP_CHARS = 50_000
# Cap on a single verb result relayed into the child (mirrors node truncation
# in spirit; workflows should pass data, not dump it into context).
DEFAULT_RESULT_CAP_CHARS = 30_000


class WorkflowBudgetExceeded(Exception):
    """A run hit one of its caps. ``cap`` names which one."""

    def __init__(self, cap: str, message: str) -> None:
        super().__init__(message)
        self.cap = cap
        self.message = message


@dataclass(frozen=True)
class WorkflowBudget:
    """Every cap for one workflow run. Immutable; counters live in usage."""

    wall_clock_seconds: float = DEFAULT_WALL_CLOCK_SECONDS
    # None resolves lazily to settings.tool_timeout at charge time, so tests
    # and callers can pin it without touching settings.
    verb_timeout_seconds: Optional[float] = None
    max_calls: int = DEFAULT_MAX_CALLS
    max_ai_calls: int = DEFAULT_MAX_AI_CALLS
    max_depth: int = DEFAULT_MAX_DEPTH
    state_cap_bytes: int = DEFAULT_STATE_CAP_BYTES
    log_cap_chars: int = DEFAULT_LOG_CAP_CHARS
    result_cap_chars: int = DEFAULT_RESULT_CAP_CHARS

    def resolve_verb_timeout(self, *, ai: bool = False) -> float:
        """Per-verb timeout: ``tool_timeout`` for plain verbs, wall clock for AI.

        An AI verb (``nym.llm``, ``nym.thread``) legitimately runs a sub-agent
        turn that outlives a tool timeout, and it is separately capped by
        ``max_ai_calls`` and the run's wall-clock watchdog, so its per-frame
        ceiling is the wall clock rather than ``tool_timeout``.
        """
        if ai:
            return max(1.0, float(self.wall_clock_seconds))
        if self.verb_timeout_seconds is not None:
            return max(1.0, float(self.verb_timeout_seconds))
        try:
            from ...config import get_settings

            return max(1.0, float(get_settings().tool_timeout))
        except Exception:  # noqa: BLE001 - settings may be unavailable in tests
            return 60.0


@dataclass
class BudgetUsage:
    """Mutable consumption counters for one run; reported in the envelope."""

    calls_used: int = 0
    ai_calls_used: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)
    wall_seconds: float = 0.0

    def charge_call(self, budget: WorkflowBudget, *, ai: bool = False) -> None:
        """Count one ``nym.*`` call, raising when a cap is hit.

        The charge happens BEFORE dispatch, so the call that would exceed the
        cap never executes (a cap is a hard ceiling, not a soft warning).
        """
        if self.calls_used + 1 > budget.max_calls:
            raise WorkflowBudgetExceeded(
                "max_calls",
                f"workflow exceeded its total call cap ({budget.max_calls})",
            )
        if ai and self.ai_calls_used + 1 > budget.max_ai_calls:
            raise WorkflowBudgetExceeded(
                "max_ai_calls",
                f"workflow exceeded its AI call cap ({budget.max_ai_calls})",
            )
        self.calls_used += 1
        if ai:
            self.ai_calls_used += 1

    def finish(self) -> None:
        self.wall_seconds = round(time.monotonic() - self.started_monotonic, 3)

    def snapshot(self) -> dict:
        return {
            "calls_used": self.calls_used,
            "ai_calls_used": self.ai_calls_used,
            "wall_seconds": self.wall_seconds,
        }
