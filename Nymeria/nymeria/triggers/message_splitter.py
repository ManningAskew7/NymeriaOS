"""Shared message splitting helpers for chat-platform integrations."""

from __future__ import annotations

from typing import List


def split_markdown_message(content: str, max_length: int) -> List[str]:
    """Split text with Markdown code-block awareness.

    Used by Discord and Telegram, where generated assistant text may include
    fenced code blocks and multiline formatting.
    """
    _validate_max_length(max_length)
    if len(content) <= max_length:
        return [content]

    chunks: List[str] = []
    remaining = content

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        split_at = _find_markdown_split_point(remaining, max_length)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    return [chunk for chunk in chunks if chunk.strip()]


def split_plain_message(
    content: str,
    max_length: int,
    *,
    boundary_threshold: float = 0.3,
) -> List[str]:
    """Split plain text on sentence or word boundaries when possible."""
    _validate_max_length(max_length)
    if len(content) <= max_length:
        return [content]

    chunks: List[str] = []
    remaining = content

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        split_at = remaining.rfind(". ", 0, max_length)
        if split_at > max_length * boundary_threshold:
            split_at += 2
        else:
            split_at = remaining.rfind(" ", 0, max_length)
            if split_at > max_length * boundary_threshold:
                split_at += 1
            else:
                split_at = max_length

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return [chunk for chunk in chunks if chunk.strip()]


def split_discord_message(content: str, max_length: int = 2000) -> List[str]:
    """Split a message for Discord's text limit."""
    return split_markdown_message(content, max_length)


def split_telegram_message(content: str, max_length: int = 4096) -> List[str]:
    """Split a message for Telegram's text limit."""
    return split_markdown_message(content, max_length)


def split_twitch_message(content: str, max_length: int = 490) -> List[str]:
    """Split a message for Twitch's practical 500-character text limit."""
    return split_plain_message(content, max_length)


def _find_markdown_split_point(text: str, max_length: int) -> int:
    code_block_start = text.rfind("```", 0, max_length)
    if code_block_start > 0:
        count_before = text[:code_block_start].count("```")
        if count_before % 2 == 1:
            closing = text.find("```", code_block_start + 3)
            if closing != -1 and closing + 3 <= len(text):
                end_of_block = closing + 3
                if end_of_block <= max_length:
                    return end_of_block
            block_open = text.rfind("```", 0, code_block_start)
            if block_open > max_length * 0.3:
                return block_open

    para = text.rfind("\n\n", 0, max_length)
    if para > max_length * 0.5:
        return para + 2

    line = text.rfind("\n", 0, max_length)
    if line > max_length * 0.5:
        return line + 1

    sentence = text.rfind(". ", 0, max_length)
    if sentence > max_length * 0.5:
        return sentence + 2

    return max_length


def _validate_max_length(max_length: int) -> None:
    if max_length < 1:
        raise ValueError("max_length must be at least 1")
