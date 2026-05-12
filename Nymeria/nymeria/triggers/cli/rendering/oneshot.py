"""Oneshot renderer for non-interactive CLI output."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterable
from typing import Any, Literal, TextIO

from ..events import (
    DispatchedEvent,
    DoneEvent,
    ErrorEvent,
    NormalizedEvent,
    ResponseEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
)

OutputFormat = Literal["plain", "json"]


class OneshotRenderer:
    """Minimal renderer that writes response content to stdout.

    In plain mode, only assistant response text goes to stdout; tool calls,
    thinking, and diagnostics go to stderr when verbose is set.

    In JSON mode, newline-delimited JSON objects go to stdout for every
    significant event type.
    """

    def __init__(
        self,
        *,
        output_format: OutputFormat = "plain",
        verbose: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        self.output_format = output_format
        self.verbose = verbose
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self._had_error = False
        self._stdout_needs_newline = False

    async def consume(self, events: AsyncIterable[NormalizedEvent]) -> bool:
        """Consume the full event stream, returning True on success."""

        async for event in events:
            self._handle_event(event)
        self._finish()
        return not self._had_error

    def _handle_event(self, event: NormalizedEvent) -> None:
        if self.output_format == "json":
            self._handle_json(event)
        else:
            self._handle_plain(event)

    def _handle_plain(self, event: NormalizedEvent) -> None:
        if isinstance(event, ResponseEvent) and event.content:
            self.stdout.write(event.content)
            self.stdout.flush()
            self._stdout_needs_newline = not event.content.endswith("\n")
        elif isinstance(event, ErrorEvent):
            self._had_error = True
            self.stderr.write(f"Error: {event.content or event.code}\n")
            self.stderr.flush()
        elif self.verbose:
            if isinstance(event, DispatchedEvent):
                target = event.title or event.target_thread_id or "thread"
                self.stderr.write(f"[dispatched] {target}\n")
                self.stderr.flush()
            elif isinstance(event, ThinkingEvent) and event.content:
                self.stderr.write(f"[thinking] {event.content}\n")
                self.stderr.flush()
            elif isinstance(event, ToolCallEvent):
                self.stderr.write(f"> {event.name}\n")
                self.stderr.flush()
            elif isinstance(event, ToolResultEvent):
                status = f" ({event.status})" if event.status != "success" else ""
                preview = str(event.result or "")[:120]
                self.stderr.write(f"< {event.name}{status} {preview}\n")
                self.stderr.flush()

    def _handle_json(self, event: NormalizedEvent) -> None:
        obj = self._event_to_json(event)
        if obj is None:
            return
        if isinstance(event, ErrorEvent):
            self._had_error = True
        line = json.dumps(obj, ensure_ascii=False, default=str)
        self.stdout.write(line + "\n")
        self.stdout.flush()

    def _event_to_json(self, event: NormalizedEvent) -> dict[str, Any] | None:
        if isinstance(event, ResponseEvent):
            if not event.content:
                return None
            return {"type": "response", "content": event.content}
        if isinstance(event, ThinkingEvent):
            if not event.content:
                return None
            return {"type": "thinking", "content": event.content}
        if isinstance(event, ToolCallEvent):
            return {
                "type": "tool_call",
                "id": event.id,
                "name": event.name,
                "args": event.args,
            }
        if isinstance(event, ToolResultEvent):
            return {
                "type": "tool_result",
                "id": event.id,
                "name": event.name,
                "result": event.result,
                "status": event.status,
            }
        if isinstance(event, DispatchedEvent):
            return {
                "type": "dispatched",
                "thread_id": event.thread_id,
                "target_thread_id": event.target_thread_id,
                "title": event.title,
                "original_thread_id": event.original_thread_id,
                "matched_ref": event.matched_ref,
                "dispatched_to": {
                    "thread_id": event.target_thread_id,
                    "title": event.title,
                    "original_thread_id": event.original_thread_id,
                },
            }
        if isinstance(event, ErrorEvent):
            return {
                "type": "error",
                "content": event.content,
                "code": event.code,
            }
        if isinstance(event, DoneEvent):
            return {
                "type": "done",
                "model": event.model,
                "status": event.status,
                "dispatched_to": event.dispatched_to,
            }
        return None

    def _finish(self) -> None:
        if self._stdout_needs_newline:
            self.stdout.write("\n")
            self.stdout.flush()
            self._stdout_needs_newline = False


__all__ = ["OneshotRenderer", "OutputFormat"]
