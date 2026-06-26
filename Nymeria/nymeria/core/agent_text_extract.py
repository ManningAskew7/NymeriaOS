"""Text and reasoning extraction primitives for NymeriaAgent.

Pure, dependency-light helpers lifted out of ``agent_history.py`` (slice 02 F5):
the data-URL MIME helper, the reasoning-text extractors, content-part
extraction, and the streaming inline-thinking sanitizer. This module is a leaf:
it imports nothing from the agent family, so both the history-projection
pipeline (``agent_history.py``) and the streaming path (``agent_streaming.py``)
can depend on it without a circular import. ``agent_history.py`` re-exports these
names so existing imports and test seams keep resolving against it.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List


def extract_mime_from_data_url(data_url: str) -> str:
    """Extract MIME type from a data URL like 'data:image/png;base64,...'."""
    if data_url.startswith("data:"):
        header = data_url.split(",", 1)[0]
        mime = header[5:]
        if ";" in mime:
            mime = mime.split(";", 1)[0]
        return mime
    return "application/octet-stream"


def _walk_reasoning_value(value: Any, sink: Callable[[str], None]) -> None:
    """Recursively feed plaintext reasoning to ``sink``, skipping encrypted blocks.

    Shared by ``extract_reasoning_text_from_block`` and
    ``extract_reasoning_text_from_details``: non-empty strings are sent to the
    sink, dicts recurse on the first present of text/content/reasoning/summary
    (dropping ``reasoning.encrypted`` blocks entirely), and lists recurse
    element-wise. ``extract_reasoning_parts`` deliberately does NOT use this
    helper: it carries a different field precedence (summary before reasoning),
    a seen-set dedupe, and no encrypted-skip, so it keeps its own walk.
    """
    if isinstance(value, str):
        if value:
            sink(value)
    elif isinstance(value, dict):
        if value.get("type") == "reasoning.encrypted":
            return
        _walk_reasoning_value(
            value.get("text")
            or value.get("content")
            or value.get("reasoning")
            or value.get("summary"),
            sink,
        )
    elif isinstance(value, list):
        for item in value:
            _walk_reasoning_value(item, sink)


def extract_reasoning_text_from_block(block: Dict[str, Any]) -> List[str]:
    """Extract plaintext reasoning from one Responses API reasoning block."""
    content_parts: List[str] = []
    summary_parts: List[str] = []

    _walk_reasoning_value(block.get("reasoning"), content_parts.append)
    _walk_reasoning_value(block.get("content"), content_parts.append)

    summary = block.get("summary")
    if isinstance(summary, str):
        _walk_reasoning_value(summary, summary_parts.append)
    elif isinstance(summary, list):
        for part in summary:
            _walk_reasoning_value(part, summary_parts.append)

    sections: List[str] = []
    if content_parts:
        sections.append("".join(content_parts))
    if summary_parts:
        sections.append("\n\n".join(summary_parts))

    joined = "\n\n".join(section for section in sections if section)
    return [joined] if joined else []


def extract_reasoning_text_from_details(details: Any) -> List[str]:
    """Extract displayable plaintext from OpenRouter reasoning_details metadata."""
    parts: List[str] = []
    _walk_reasoning_value(details, parts.append)
    joined = "".join(parts)
    return [joined] if joined else []


THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def strip_inline_thinking_text(text: str) -> str:
    """Remove provider-leaked inline thinking tags from assistant text."""
    if not isinstance(text, str) or not text:
        return text
    if THINK_OPEN not in text and THINK_CLOSE not in text:
        return text

    response_parts: List[str] = []
    i = 0

    while i < len(text):
        open_idx = text.find(THINK_OPEN, i)
        close_idx = text.find(THINK_CLOSE, i)

        if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
            i = close_idx + len(THINK_CLOSE)
            while i < len(text) and text[i].isspace():
                i += 1
            continue

        if open_idx == -1:
            response_parts.append(text[i:])
            break

        response_parts.append(text[i:open_idx])
        i = open_idx + len(THINK_OPEN)

        close_after_open = text.find(THINK_CLOSE, i)
        if close_after_open == -1:
            break

        i = close_after_open + len(THINK_CLOSE)
        while i < len(text) and text[i].isspace():
            i += 1

    return "".join(response_parts)


def trailing_marker_prefix_length(text: str) -> int:
    """Return suffix length that may be the start of a think marker."""
    max_keep = min(len(text), max(len(THINK_OPEN), len(THINK_CLOSE)) - 1)
    for length in range(max_keep, 0, -1):
        suffix = text[-length:]
        if THINK_OPEN.startswith(suffix) or THINK_CLOSE.startswith(suffix):
            return length
    return 0


class InlineThinkingTextStripper:
    """Streaming sanitizer for provider-leaked inline thinking text."""

    def __init__(self) -> None:
        self._inside_thinking = False
        self._maybe_dangling_thinking = False
        self._buffer = ""

    @property
    def is_holding_possible_inline_thinking(self) -> bool:
        return self._maybe_dangling_thinking and bool(self._buffer)

    @property
    def buffered_length(self) -> int:
        return len(self._buffer)

    def mark_possible_inline_thinking(self) -> None:
        """Buffer text after an empty reasoning placeholder until safe."""
        if not self._inside_thinking and not self._buffer:
            self._maybe_dangling_thinking = True

    def process_text(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return ""

        self._buffer += text

        if self._maybe_dangling_thinking:
            open_idx = self._buffer.find(THINK_OPEN)
            close_idx = self._buffer.find(THINK_CLOSE)
            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
                self._buffer = self._buffer[close_idx + len(THINK_CLOSE):].lstrip()
                self._maybe_dangling_thinking = False
            elif open_idx != -1:
                self._maybe_dangling_thinking = False
            else:
                return ""

        return self._drain()

    def flush(self) -> str:
        self._maybe_dangling_thinking = False
        return self._drain(final=True)

    def _drain(self, final: bool = False) -> str:
        output: List[str] = []

        while self._buffer:
            if self._inside_thinking:
                close_idx = self._buffer.find(THINK_CLOSE)
                if close_idx == -1:
                    if final:
                        self._buffer = ""
                    else:
                        keep = trailing_marker_prefix_length(self._buffer)
                        self._buffer = self._buffer[-keep:] if keep else ""
                    break
                self._buffer = self._buffer[close_idx + len(THINK_CLOSE):].lstrip()
                self._inside_thinking = False
                continue

            open_idx = self._buffer.find(THINK_OPEN)
            close_idx = self._buffer.find(THINK_CLOSE)
            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
                self._buffer = self._buffer[close_idx + len(THINK_CLOSE):].lstrip()
                continue

            if open_idx != -1:
                output.append(self._buffer[:open_idx])
                self._buffer = self._buffer[open_idx + len(THINK_OPEN):]
                self._inside_thinking = True
                continue

            if final:
                output.append(self._buffer)
                self._buffer = ""
                break

            keep = trailing_marker_prefix_length(self._buffer)
            if keep:
                output.append(self._buffer[:-keep])
                self._buffer = self._buffer[-keep:]
            else:
                output.append(self._buffer)
                self._buffer = ""
            break

        return "".join(output)

    def reset(self) -> None:
        self._inside_thinking = False
        self._maybe_dangling_thinking = False
        self._buffer = ""


def extract_content_parts(content: Any) -> tuple[str, List[str]]:
    """Extract display text and thinking blocks from AIMessage.content."""
    if isinstance(content, str):
        return strip_inline_thinking_text(content), []
    if isinstance(content, list):
        text_parts = []
        thinking_parts = []
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type in ("text", "output_text"):
                    text = strip_inline_thinking_text(block.get("text", ""))
                    if text:
                        text_parts.append(text)
                elif block_type == "thinking":
                    thinking_parts.append(block.get("thinking", ""))
                elif block_type == "reasoning":
                    thinking_parts.extend(extract_reasoning_text_from_block(block))
            elif isinstance(block, str):
                text = strip_inline_thinking_text(block)
                if text:
                    text_parts.append(text)
        return "\n".join(text_parts), thinking_parts
    return str(content), []


def extract_reasoning_parts(msg: Any) -> List[str]:
    """Extract OpenAI-compatible reasoning saved on AIMessage metadata."""
    additional_kwargs = getattr(msg, "additional_kwargs", None) or {}
    parts: List[str] = []
    seen: set[str] = set()

    # NOTE: intentionally NOT _walk_reasoning_value. This walk has a different
    # field precedence (summary before reasoning), a seen-set dedupe, and no
    # reasoning.encrypted skip; merging it would change behavior.
    def add_text(value: Any) -> None:
        if isinstance(value, str):
            text = value
        elif isinstance(value, dict):
            summary = value.get("summary")
            if isinstance(summary, list):
                for item in summary:
                    add_text(item)
                return
            text = (
                value.get("text")
                or value.get("content")
                or summary
                or value.get("reasoning")
            )
        elif isinstance(value, list):
            for item in value:
                add_text(item)
            return
        else:
            text = None

        if isinstance(text, str) and text and text not in seen:
            parts.append(text)
            seen.add(text)

    for text in extract_reasoning_text_from_details(
        additional_kwargs.get("reasoning_details")
    ):
        add_text(text)

    for key in ("reasoning_content", "reasoning"):
        value = additional_kwargs.get(key)
        if isinstance(value, list):
            for item in value:
                add_text(item)
        else:
            add_text(value)

    return parts
