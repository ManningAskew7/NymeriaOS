"""Agent-facing tool that renders an interactive HTML form on the user's
desktop and blocks until they answer (backlog #5).

The bridge half mirrors the ``chrome_*`` tool family: allocate a
``prompt_id``, register a future with the :class:`UiPromptCoordinator`,
publish a ``ui_prompt`` autonomous event over ``/autonomous/stream``, and
await the future. The nymeria-desktop app renders the HTML in a sandboxed
iframe (``sandbox="allow-scripts"``, null origin, CSP ``connect-src
'none'``; Alpine.js + Tailwind v4 + DaisyUI are inlined into the srcdoc)
and POSTs the user's submission to ``/ui-prompts/{prompt_id}/result``.

Desktop-only by design: no other client renders the event, so on a
desktop-less deployment the tool simply times out. Single-process
limitation: the coordinator lives in this API process.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta
from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.event_bus import publish_autonomous_event
from ..core.time_utils import utc_now
from ..core.ui_prompt_coordinator import (
    MAX_TIMEOUT_SECONDS,
    get_ui_prompt_coordinator,
    new_prompt_id,
)
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 300
_MIN_TIMEOUT_SECONDS = 15
# 256 KiB: bounds the SSE frame / event-bus payload; real forms are a few KB.
_MAX_HTML_BYTES = 256 * 1024


def _format_result(payload: dict[str, Any]) -> str:
    """Convert the resolved coordinator result to a string the agent reads."""
    try:
        return json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return str(payload)


def _publish_closure(prompt_id: str, thread_id: str, user_id: str, status: str) -> None:
    """Best-effort ``ui_prompt_result`` so open modals retract on terminal
    outcomes no client initiated (timeout, abort, sweep). Client-initiated
    outcomes are published by the result endpoint instead."""
    try:
        publish_autonomous_event(
            event_type="ui_prompt_result",
            thread_id=thread_id,
            user_id=user_id,
            task_id="",
            data={"prompt_id": prompt_id, "status": status},
        )
    except Exception:
        logger.exception("failed to publish ui_prompt_result closure for %s", prompt_id)


@tool
async def ui_prompt(
    html: str,
    title: Optional[str] = None,
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Show an interactive HTML form to the user in the desktop app and wait
    for their answer.

    Write a self-contained HTML fragment (it becomes the body of a sandboxed
    document with no network access). Tailwind utility classes, DaisyUI
    components, and Alpine.js directives all work. A plain <form> with a
    submit button is auto-wired: on submit its field values (keyed by input
    name) come back as this tool's result. For script-driven UIs call
    nymeria.submit({...}) or nymeria.cancel() instead.

    Only the desktop app renders this; if the user is not there, the call
    times out. Returns JSON: {"ok", "status", "values"} where status is
    "submitted" or "cancelled".

    html: the HTML fragment to render.
    title: short modal heading, e.g. "Choose deployment options".
    timeout_seconds: how long to wait for the user (15-600, default 300).
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)

    # gating hook: a future connection-capability check slots in here,
    # parallel to is_chrome_connected (core/chrome_subscribers.py), failing
    # fast when no desktop client is attached and feeding
    # select_tools_for_graph. Ungated for now by design.

    if not html or not html.strip():
        return "[Error]: html must be a non-empty HTML fragment."
    if len(html.encode("utf-8", errors="replace")) > _MAX_HTML_BYTES:
        return (
            f"[Error]: html is too large (limit {_MAX_HTML_BYTES // 1024} KiB). "
            "Trim the markup; assets like Tailwind and DaisyUI are already "
            "provided by the renderer."
        )

    timeout_s = max(_MIN_TIMEOUT_SECONDS, min(int(timeout_seconds), MAX_TIMEOUT_SECONDS))
    prompt_id = new_prompt_id()
    clean_title = (title or "").strip()
    coord = get_ui_prompt_coordinator()
    future = coord.register(
        prompt_id=prompt_id,
        user_id=user_id,
        thread_id=thread_id,
        title=clean_title,
    )
    expires_at = (utc_now() + timedelta(seconds=timeout_s)).isoformat()
    publish_autonomous_event(
        event_type="ui_prompt",
        thread_id=thread_id,
        user_id=user_id,
        task_id="",
        data={
            "prompt_id": prompt_id,
            "title": clean_title,
            "html": html,
            "timeout_seconds": timeout_s,
            "expires_at": expires_at,
        },
    )
    try:
        result = await asyncio.wait_for(future, timeout=timeout_s + 1)
    except asyncio.TimeoutError:
        coord.discard(prompt_id)
        _publish_closure(prompt_id, thread_id, user_id, "timeout")
        return (
            f"[Error]: ui_prompt timed out after {timeout_s}s with no response "
            "from the user. They may not be at the desktop app; continue "
            "without the answers or ask in chat instead."
        )
    except asyncio.CancelledError:
        coord.discard(prompt_id)
        _publish_closure(prompt_id, thread_id, user_id, "cancelled")
        raise
    status = result.get("status") if isinstance(result, dict) else None
    if status in ("aborted", "swept"):
        # Nobody POSTed a result, so no client published a retraction.
        _publish_closure(prompt_id, thread_id, user_id, str(status))
    return _format_result(result)


UI_PROMPT_TOOLS = [ui_prompt]


__all__ = [
    "UI_PROMPT_TOOLS",
    "ui_prompt",
]
