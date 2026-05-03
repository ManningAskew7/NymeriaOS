"""Provider-agnostic token usage extraction from LangChain AI messages.

Handles metadata shapes from Anthropic, OpenAI, OpenRouter, CLIProxy, and
LangChain's standardised ``usage_metadata``.
"""

from __future__ import annotations

from typing import List, Tuple

from langchain_core.messages import AIMessage


def extract_from_message(msg: AIMessage) -> Tuple[int, int]:
    """Extract (input_tokens, output_tokens) from a single AIMessage.

    Checks in order:
    1. ``usage_metadata`` — LangChain's standardised location.
    2. ``response_metadata.usage`` — Anthropic native format.
    3. ``response_metadata.token_usage`` — OpenAI / OpenRouter format.
    """
    if hasattr(msg, "usage_metadata") and msg.usage_metadata:
        um = msg.usage_metadata
        inp = getattr(um, "input_tokens", 0) or (
            um.get("input_tokens", 0) if isinstance(um, dict) else 0
        )
        out = getattr(um, "output_tokens", 0) or (
            um.get("output_tokens", 0) if isinstance(um, dict) else 0
        )
        if inp or out:
            return inp, out

    if hasattr(msg, "response_metadata") and msg.response_metadata:
        meta = msg.response_metadata
        usage = meta.get("usage", {})
        if usage.get("input_tokens") or usage.get("output_tokens"):
            return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        token_usage = meta.get("token_usage", {})
        if token_usage.get("prompt_tokens") or token_usage.get("completion_tokens"):
            return token_usage.get("prompt_tokens", 0), token_usage.get("completion_tokens", 0)

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
