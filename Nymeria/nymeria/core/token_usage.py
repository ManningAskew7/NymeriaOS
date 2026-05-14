"""Provider-agnostic token usage extraction from LangChain AI messages.

Handles metadata shapes from Anthropic, OpenAI, OpenRouter, CLIProxy, and
LangChain's standardised ``usage_metadata``.
"""

from __future__ import annotations

from typing import Any, List, Tuple

from langchain_core.messages import AIMessage


def _usage_value(usage: Any, *names: str) -> int:
    """Return the first integer-ish token count from a usage metadata object."""
    if not usage:
        return 0

    for name in names:
        value = None
        if isinstance(usage, dict):
            value = usage.get(name)
        else:
            value = getattr(usage, name, None)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def extract_from_message(msg: AIMessage) -> Tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a single AIMessage.

    Checks in order:
    1. ``usage_metadata`` — LangChain's standardised location.
    2. ``response_metadata.usage`` — Anthropic native format.
    3. ``response_metadata.token_usage`` — OpenAI / OpenRouter format.
    """
    if hasattr(msg, "usage_metadata") and msg.usage_metadata:
        um = msg.usage_metadata
        inp = _usage_value(um, "input_tokens", "prompt_tokens")
        out = _usage_value(um, "output_tokens", "completion_tokens")
        if inp or out:
            return inp, out

    if hasattr(msg, "response_metadata") and msg.response_metadata:
        meta = msg.response_metadata
        usage = meta.get("usage", {})
        inp = _usage_value(usage, "input_tokens", "prompt_tokens")
        out = _usage_value(usage, "output_tokens", "completion_tokens")
        if inp or out:
            return inp, out
        token_usage = meta.get("token_usage", {})
        inp = _usage_value(token_usage, "prompt_tokens", "input_tokens")
        out = _usage_value(token_usage, "completion_tokens", "output_tokens")
        if inp or out:
            return inp, out

    return 0, 0


def extract_last_from_messages(messages: List) -> Tuple[int, int]:
    """Return (input_tokens, output_tokens) from the most recent AIMessage
    in *messages* that carries token metadata.
    """
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        inp, out = extract_from_message(msg)
        if inp or out:
            return inp, out
    return 0, 0
