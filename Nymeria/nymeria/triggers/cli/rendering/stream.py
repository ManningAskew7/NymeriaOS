"""Deprecated stream-renderer compatibility shim.

The CLI now routes chat through ``AgentClient`` transports and reducer-backed
renderers. This module remains only so older imports of ``StreamRenderer`` do
not fail while callers migrate to ``RichReplRenderer`` or ``PlainRenderer``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .rich_repl import RichReplRenderer


class StreamRenderer:
    """Compatibility wrapper around the reducer-backed Rich REPL renderer."""

    def __init__(self, state: Any) -> None:
        self.state = state
        self._renderer = RichReplRenderer(
            console=getattr(state, "console", None),
            error_console=getattr(state, "console", None),
        )

    def render_stream(self, events: Iterable[Any]) -> None:
        """Render normalized/raw-compatible events through the reducer path."""

        self._renderer.render_events(events)
        self._renderer.flush_response()


__all__ = ["StreamRenderer"]
