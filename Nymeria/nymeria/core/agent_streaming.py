"""Small streaming classification helpers for NymeriaAgent.astream()."""

from __future__ import annotations

from typing import Any


TOOL_CALL_CONTENT_DELTA_TYPES = frozenset({
    "function_call",
    "tool_call",
    "tool_call_chunk",
    "tool_use",
    "input_json_delta",
})


def has_tool_call_delta(tool_call_chunks: Any) -> bool:
    """Return True when LangChain exposed provider tool-call chunks."""
    return bool(tool_call_chunks)


def has_tool_call_content_delta(content: Any) -> bool:
    """Return True when streamed content blocks represent tool-call deltas."""
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in TOOL_CALL_CONTENT_DELTA_TYPES:
            return True
    return False


class ReasoningChunkDeduper:
    """Deduplicate reasoning chunks within a single model call."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def should_emit(self, text: Any) -> bool:
        if not isinstance(text, str) or not text:
            return False
        if text in self._seen:
            return False
        self._seen.add(text)
        return True

    def reset(self) -> None:
        self._seen.clear()
