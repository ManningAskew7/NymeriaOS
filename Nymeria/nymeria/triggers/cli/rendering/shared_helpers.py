"""Leaf helpers shared by the Rich, plain, and transcript CLI renderers.

These were byte-identical (or annotation-only divergent) copies in rich_repl.py,
plain.py, and transcript.py. Centralizing them keeps a fix to one renderer's
dispatch-reference text or response-length map from drifting out of sync with
the others. Dependency-light: only the CLI state model.
"""

from __future__ import annotations

from ..state import AssistantMessage, CLIUIState, select_response_content


def _dispatch_reference_text(message: AssistantMessage) -> str:
    content = str(message.dispatch_info.get("content") or "").strip()
    if content:
        return content
    title = str(message.dispatch_info.get("title") or "").strip()
    thread_id = str(message.dispatch_info.get("thread_id") or "").strip()
    if title:
        return f"Response from {title}"
    if thread_id:
        return f"Response from {thread_id}"
    return ""


def _assistant_response_lengths(state: CLIUIState) -> dict[str, int]:
    return {
        message.id: len(select_response_content(message))
        for message in state.messages
        if isinstance(message, AssistantMessage)
    }
