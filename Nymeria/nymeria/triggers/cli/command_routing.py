"""Shared CLI command-routing helpers used by both shell implementations.

The Rich REPL (``app.py``) and the legacy full-screen shell
(``rendering/full_screen_legacy.py``) both need to classify ``/``-prefixed
chat-stream commands and render the queued-message notice. Keeping these here
(rather than copied per shell) ensures the two shells classify commands
identically. This is a dependency-free leaf module to avoid import cycles.
"""

from __future__ import annotations

from typing import Any


def queued_notice(count: int) -> str:
    """Status-bar notice text for ``count`` queued (busy-deferred) submissions."""

    if count == 1:
        return "Queued message (1)"
    return f"Queued messages ({count})"


def chat_stream_command_from_result(result: Any) -> str:
    """Extract a queued chat-stream command name from a command result payload."""

    payload = getattr(result, "payload", {}) or {}
    command = payload.get("chat_stream_command")
    return str(command or "").strip()


def is_chat_stream_command(registry: Any, raw_input: str) -> bool:
    """Whether ``raw_input`` resolves to a command whose execution streams chat."""

    try:
        match = registry.resolve(raw_input)
    except Exception:  # noqa: BLE001 - fall back to normal command handling.
        return False
    command = getattr(match, "command", None)
    metadata = getattr(command, "metadata", {}) or {}
    return str(metadata.get("execution_kind") or "") == "chat_stream"
