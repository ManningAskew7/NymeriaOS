"""Selectors for the reducer-owned CLI/TUI state model."""

from __future__ import annotations

import time
from typing import Any

from .model import (
    AssistantActivityPhase,
    AssistantMessage,
    CLIUIState,
    MessageStep,
    ResponseStep,
    ThinkingStep,
    ToolCallStep,
)

QUIET_TO_FORMULATING_SECONDS = 1.0
QUIET_TYPING_TO_FINALIZING_SECONDS = 1.5


def select_last_assistant_message(state: CLIUIState) -> AssistantMessage | None:
    """Return the last assistant message in the transcript."""

    for message in reversed(state.messages):
        if isinstance(message, AssistantMessage):
            return message
    return None


def select_response_content(message: AssistantMessage) -> str:
    """Concatenate response steps for history/search-friendly content."""

    return "".join(
        step.content for step in message.steps if isinstance(step, ResponseStep)
    )


def select_intermediate_content(message: AssistantMessage) -> str:
    """Concatenate thinking steps for legacy renderer compatibility."""

    return "".join(
        step.content for step in message.steps if isinstance(step, ThinkingStep)
    )


def select_tool_calls(message: AssistantMessage) -> tuple[ToolCallStep, ...]:
    """Return tool call steps in transcript order."""

    return tuple(step for step in message.steps if isinstance(step, ToolCallStep))


def select_running_tool_calls(state: CLIUIState) -> tuple[ToolCallStep, ...]:
    """Return current running tool calls for status surfaces."""

    return tuple(
        call for call in state.active_tool_calls.values() if call.status == "running"
    )


def select_activity_phase(
    state: CLIUIState,
    *,
    now: float | None = None,
    quiet_to_formulating_seconds: float = QUIET_TO_FORMULATING_SECONDS,
    quiet_typing_to_finalizing_seconds: float = QUIET_TYPING_TO_FINALIZING_SECONDS,
) -> AssistantActivityPhase | None:
    """Return the current visible activity phase.

    The reducer stores the direct phase; this selector applies the two
    quiet-time transitions:

    - ``processing`` quiet >= 1s becomes ``formulating`` (no visible output
      this LLM call: the warm-up label; note tool-call arguments also stream
      under ``processing``, since only the first delta per call reaches the
      client). ``thinking`` is deliberately sticky: the reducer sets it from
      the ``llm_call_started`` status event BEFORE any delta arrives (the
      provider's prompt-processing wait, measured 3-8s+ on large contexts),
      and it must hold through that window and through any delta gap
      (summarizer pauses, provider hiccups) instead of flapping back to
      "Formulating".
    - ``typing`` quiet >= 1.5s becomes ``finalizing``: after the last visible
      token the server still runs post-turn work (checkpoint, stats, hooks)
      before the done event, and claiming "Streaming" through that window was
      stale. A mid-stream stall flips back to typing on the next chunk.
    """

    message = select_last_assistant_message(state)
    if message is None or message.status != "streaming":
        return None

    phase = message.activity_phase
    current_time = time.monotonic() if now is None else now
    quiet_seconds = current_time - message.activity_updated_at

    if phase == "typing":
        if quiet_seconds >= quiet_typing_to_finalizing_seconds:
            return "finalizing"
        return "typing"

    if phase == "processing" and quiet_seconds >= quiet_to_formulating_seconds:
        return "formulating"

    return phase


def select_context_usage(state: CLIUIState) -> dict[str, Any]:
    """Return context stats with a percentage when token fields are available."""

    stats = dict(state.context_stats)
    used = _first_number(stats, "used_tokens", "total_tokens", "input_tokens")
    maximum = _first_number(stats, "max_tokens", "context_limit", "context_window", "limit")
    if used is not None and maximum:
        stats["percent_used"] = min(100.0, max(0.0, (used / maximum) * 100))
    elif "percent_used" not in stats:
        backend_pct = _first_number(stats, "usage_percentage")
        if backend_pct is not None:
            stats["percent_used"] = backend_pct
    return stats


def _first_number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def step_text(step: MessageStep) -> str:
    """Small renderer convenience for transcript snapshot tests."""

    if isinstance(step, (ThinkingStep, ResponseStep)):
        return step.content
    if isinstance(step, ToolCallStep):
        return step.name
    return ""


__all__ = [
    "QUIET_TO_FORMULATING_SECONDS",
    "QUIET_TYPING_TO_FINALIZING_SECONDS",
    "select_activity_phase",
    "select_context_usage",
    "select_intermediate_content",
    "select_last_assistant_message",
    "select_response_content",
    "select_running_tool_calls",
    "select_tool_calls",
    "step_text",
]
