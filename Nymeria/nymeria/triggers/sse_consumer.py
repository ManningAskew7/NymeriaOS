"""Shared SSE event consumer for bot and CLI trigger platforms.

Centralises event-type routing, field extraction, tool-call counting,
and the "flush before status" discipline so that individual platform
handlers only implement rendering.

Usage — interactive chat stream::

    handler = MyPlatformHandler(...)
    await consume_sse_stream(api.chat_stream(...), handler)

Usage — autonomous single-event dispatch::

    count = await dispatch_event(event, handler, tool_call_count)
"""

from __future__ import annotations

import inspect
import re
from typing import Any, AsyncIterable, Dict, List, Protocol, runtime_checkable

_ATTACH_RE = re.compile(r"\[attach:(.+?)\]")


def parse_attach_paths(result: str) -> List[str]:
    """Extract file paths from ``[attach:/path]`` tags in tool output."""
    if not isinstance(result, str):
        return []
    return _ATTACH_RE.findall(result)


def format_auth_prompt_message(event: Dict[str, Any]) -> str:
    """Render a safe default credential-setup prompt for text chat surfaces."""
    display_name = str(
        event.get("display_name") or event.get("provider") or "a service"
    )
    expires_at = event.get("expires_at")
    connect_url = event.get("connect_url")
    if isinstance(connect_url, str) and connect_url.strip():
        expiry = f"\n\nLink expires: {expires_at}" if expires_at else ""
        return (
            f"Credential setup requested for {display_name}.\n\n"
            f"Open this secure one-time link: {connect_url.strip()}"
            f"{expiry}\n\n"
            "Do not paste secrets into chat."
        )
    return (
        f"Credential setup requested for {display_name}, but this chat app "
        "cannot show the secure setup form because NYMERIA_PUBLIC_URL is not "
        "configured on the server. Use the desktop app's credential prompt or "
        "ask the server admin to set NYMERIA_PUBLIC_URL. Do not paste secrets "
        "into chat."
    )


# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SSEEventHandler(Protocol):
    """Platform-specific rendering callbacks for SSE events.

    The dispatcher calls ``flush_text(final=True)`` **before** status
    events (compacting, compacted, context_attached, error, tool_reload)
    so handlers don't need to remember to flush.
    """

    async def flush_text(self, final: bool = False) -> None:
        """Flush buffered response text."""
        ...

    async def on_thinking(self) -> None: ...

    async def on_response_chunk(self, content: str) -> None: ...

    async def on_compacting(self, message: str) -> None: ...

    async def on_compacted(
        self,
        summary: str,
        messages_removed: int,
        title: str,
    ) -> None: ...

    async def on_tool_call(
        self,
        name: str,
        args: Dict[str, Any],
        call_id: str,
        count: int,
    ) -> None: ...

    async def on_tool_result(
        self,
        call_id: str,
        result: str,
        attachments: List[str],
    ) -> None: ...

    async def on_tool_reload(self, tools: List[str], ttl: str) -> None: ...

    async def on_workspace_artifact(self, path: str) -> None: ...

    async def on_error(self, content: str) -> None: ...

    async def on_iteration_limit(self, content: str) -> None: ...

    async def on_done(self, tool_call_count: int) -> None: ...

    async def on_stream_end(self, tool_call_count: int) -> None: ...


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def dispatch_event(
    event: Dict[str, Any],
    handler: SSEEventHandler,
    tool_call_count: int,
) -> int:
    """Route a single SSE event to the matching handler callback.

    Returns the (possibly incremented) *tool_call_count* so callers can
    thread it through a sequence of events.
    """
    etype = event.get("type", "")

    if etype == "thinking" or etype == "tool_call_delta":
        await handler.on_thinking()

    elif etype == "dispatched":
        dispatched_to = event.get("dispatched_to")
        target = dispatched_to if isinstance(dispatched_to, dict) else {}
        title = str(event.get("title") or target.get("title") or "thread")
        await handler.on_response_chunk(f"[Response from {title}]\n\n")

    elif etype == "response":
        content = event.get("content", "")
        if content:
            await handler.on_response_chunk(content)

    elif etype == "auth_prompt":
        await handler.flush_text(final=True)
        callback = getattr(handler, "on_auth_prompt", None)
        if callable(callback):
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        else:
            await handler.on_response_chunk(format_auth_prompt_message(event))
            await handler.flush_text(final=True)

    elif etype == "compacting":
        await handler.flush_text(final=True)
        await handler.on_compacting(
            event.get("message") or "Compacting context...",
        )

    elif etype == "compacted":
        await handler.flush_text(final=True)
        await handler.on_compacted(
            event.get("summary", ""),
            int(event.get("messages_removed") or 0),
            title="Context compacted",
        )

    elif etype == "context_attached":
        await handler.flush_text(final=True)
        await handler.on_compacted(
            event.get("summary", ""),
            0,
            title="Context summary attached",
        )

    elif etype == "tool_call":
        tool_call_count += 1
        await handler.on_tool_call(
            name=event.get("name", "?"),
            args=event.get("args", {}),
            call_id=event.get("id", ""),
            count=tool_call_count,
        )

    elif etype == "tool_result":
        result = event.get("result", "")
        await handler.on_tool_result(
            call_id=event.get("id", ""),
            result=result,
            attachments=parse_attach_paths(result),
        )

    elif etype == "tool_reload":
        await handler.flush_text(final=True)
        await handler.on_tool_reload(
            tools=event.get("tools", []),
            ttl=event.get("ttl", ""),
        )

    elif etype == "workspace_artifact":
        path = event.get("path")
        if isinstance(path, str) and path:
            await handler.on_workspace_artifact(path)

    elif etype == "error":
        await handler.flush_text(final=True)
        await handler.on_error(event.get("content", "Unknown error"))

    elif etype == "iteration_limit":
        content = event.get("content", "")
        if content:
            await handler.on_iteration_limit(content)

    elif etype == "done":
        await handler.on_done(tool_call_count)

    return tool_call_count


async def consume_sse_stream(
    events: AsyncIterable[Dict[str, Any]],
    handler: SSEEventHandler,
) -> None:
    """Consume an async SSE event stream and dispatch to *handler*.

    After the stream is exhausted (or on ``done``),
    ``handler.on_stream_end()`` is called so the handler can flush any
    remaining buffered text.
    """
    tool_call_count = 0
    async for event in events:
        tool_call_count = await dispatch_event(event, handler, tool_call_count)
    await handler.on_stream_end(tool_call_count)
