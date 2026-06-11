"""Shared helpers for chat-bot voice replies.

Platform bots that speak their replies (Telegram today; other platforms can
adopt the same path) synthesize the final response text via ``POST
/voice/tts``. Markdown reads terribly when spoken, so the text is flattened to
plain prose first, and capped well under the TTS providers' input limits
(OpenAI rejects >4096 chars).
"""

from __future__ import annotations

import re

# Providers cap TTS input around 4-5k chars (OpenAI: 4096); leave headroom.
MAX_SPEECH_CHARS = 4000

# Telegram's sendVoice accepts OGG/Opus, MP3, and M4A containers; anything
# else must go out as a regular audio file.
VOICE_MESSAGE_MIMES = {"audio/ogg", "audio/opus", "audio/mpeg", "audio/mp4"}

# An unterminated fence (e.g. a stream cut mid-code-block) is still code:
# match to end-of-text when no closing fence exists.
_FENCED_CODE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# Paired-marker removal only: a lone * or _ (math, snake_case identifiers,
# URLs with underscores) must survive, so markers inside words never match.
_STRONG_RE = re.compile(r"(\*\*|__|~~)")
_INLINE_EMPH_RE = re.compile(r"(?<![\w*])[*_]([^*_\n]+)[*_](?![\w*])")
_HEADER_RE = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_QUOTE_OR_BULLET_RE = re.compile(r"^\s*(?:>|[-*+]\s|\d+\.\s)\s*", re.MULTILINE)
_HRULE_RE = re.compile(r"^\s*([-*_]\s*){3,}$", re.MULTILINE)
# Table separator rows (| --- | :-: |): only dashes/colons/pipes/spaces,
# with at least one dash.
_TABLE_RULE_RE = re.compile(r"^(?=.*-)[\s|:-]+$", re.MULTILINE)
_TABLE_PIPE_RE = re.compile(r"\s*\|\s*")
_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")
_MULTI_NEWLINE_RE = re.compile(r"\n{2,}")


def strip_markdown_for_speech(text: str, max_chars: int = MAX_SPEECH_CHARS) -> str:
    """Flatten markdown to speakable plain text, at most ``max_chars`` long."""
    out = _FENCED_CODE_RE.sub(" (code omitted) ", text)
    out = _INLINE_CODE_RE.sub(r"\1", out)
    out = _IMAGE_RE.sub(r"\1", out)
    out = _LINK_RE.sub(r"\1", out)
    out = _HRULE_RE.sub("", out)
    out = _TABLE_RULE_RE.sub("", out)
    out = _HEADER_RE.sub("", out)
    out = _QUOTE_OR_BULLET_RE.sub("", out)
    out = _STRONG_RE.sub("", out)
    out = _INLINE_EMPH_RE.sub(r"\1", out)
    out = _TABLE_PIPE_RE.sub(", ", out)
    out = _MULTI_SPACE_RE.sub(" ", out)
    out = _MULTI_NEWLINE_RE.sub("\n", out)
    out = out.strip()
    if len(out) > max_chars:
        clipped = out[: max_chars - 1]
        head = clipped.rsplit(" ", 1)[0].rstrip()
        out = (head or clipped) + "…"
    return out


def is_voice_message_mime(content_type: str) -> bool:
    """True when a TTS response can be sent as a platform voice message."""
    mime = (content_type or "").split(";")[0].strip().lower()
    return mime in VOICE_MESSAGE_MIMES


__all__ = [
    "MAX_SPEECH_CHARS",
    "VOICE_MESSAGE_MIMES",
    "is_voice_message_mime",
    "strip_markdown_for_speech",
]
