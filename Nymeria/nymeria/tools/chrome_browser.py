"""Agent-facing tools that drive the user's real Chrome via the Nymeria
Browser extension.

These are the bridge half of the ``chrome_*`` tool family. Each tool
allocates a ``command_id``, registers a future with the
:class:`BrowserCommandCoordinator`, publishes a ``browser_command``
autonomous event over ``/autonomous/stream``, and awaits the future.

The Chrome extension at ``/opt/NymeriaOS/nymeria-browser/`` consumes
the event, executes the action via Chrome APIs and the Chrome DevTools
Protocol, and POSTs the result to ``/browser-commands/{id}/result``.

These tools are intentionally separate from the server-side Playwright
``browser_*`` tools in :mod:`nymeria.tools.browser`. The agent can have
either or both enabled per-thread:

* ``browser_*`` — headless Chromium running on the Nymeria server. Fresh
  profile. Survives the user closing their laptop. Works for autonomous
  ticker tasks.
* ``chrome_*`` — the user's actual logged-in Chrome. Inherits Gmail /
  GitHub / Linear / bank sessions. Requires the extension to be running.

Single-process limitation: the coordinator lives in this API process.
Phase 2 ships single-worker.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.browser_command_coordinator import (
    get_browser_command_coordinator,
    new_command_id,
)
from ..core.chrome_subscribers import is_chrome_connected
from ..core.event_bus import publish_autonomous_event
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)


# Per-command-type timeouts (seconds). Defaults keep tools snappy; the
# escape hatch and screenshots get more headroom.
_TIMEOUTS: dict[str, int] = {
    "tabs": 5,
    "navigate": 30,
    "history": 10,
    "snapshot": 15,
    "act": 15,
    "press_key": 5,
    "scroll": 5,
    "extract_text": 15,
    "screenshot": 20,
    "console": 5,
    "dialog": 5,
    "cdp": 60,
}


def _format_result(payload: dict[str, Any]) -> str:
    """Convert the resolved coordinator result to a string the agent reads."""
    try:
        return json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return str(payload)


async def _dispatch(
    *,
    command_type: str,
    args: dict[str, Any],
    config: Optional[RunnableConfig],
) -> str:
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)

    if not is_chrome_connected(user_id):
        return (
            "[Error]: No Nymeria browser extension connected for this user. "
            "Open the extension popup and click Connect."
        )

    timeout_s = _TIMEOUTS.get(command_type, 15)
    command_id = new_command_id()
    coord = get_browser_command_coordinator()
    future = coord.register(
        command_id=command_id,
        user_id=user_id,
        thread_id=thread_id,
        command_type=command_type,
        metadata=args,
    )
    publish_autonomous_event(
        event_type="browser_command",
        thread_id=thread_id,
        user_id=user_id,
        task_id="",
        data={
            "command_id": command_id,
            "command_type": command_type,
            "args": args,
            "timeout_seconds": timeout_s,
        },
    )
    try:
        result = await asyncio.wait_for(future, timeout=timeout_s + 1)
    except asyncio.TimeoutError:
        coord.discard(command_id)
        return (
            f"[Error]: Browser command '{command_type}' timed out after "
            f"{timeout_s}s. The extension may be slow or disconnected."
        )
    except asyncio.CancelledError:
        coord.discard(command_id)
        raise
    return _format_result(result)


# ---------- 1. chrome_tabs ----------


@tool
async def chrome_tabs(
    action: str,
    tab_id: Optional[int] = None,
    url: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Manage browser tabs in the user's Chrome.

    action: one of "list", "create", "switch", "close", "reload".
    tab_id: required for switch / close / reload.
    url: required for create.

    Returns JSON with the tab list (action="list") or the affected tab.
    """
    args: dict[str, Any] = {"action": action}
    if tab_id is not None:
        args["tab_id"] = tab_id
    if url is not None:
        args["url"] = url
    return await _dispatch(command_type="tabs", args=args, config=config)


# ---------- 2. chrome_navigate ----------


@tool
async def chrome_navigate(
    tab_id: int,
    url: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Navigate a Chrome tab to ``url``.

    tab_id: integer Chrome tab id (use chrome_tabs(action="list") to find).
    url: absolute http:// or https:// URL.
    """
    return await _dispatch(
        command_type="navigate",
        args={"tab_id": tab_id, "url": url},
        config=config,
    )


# ---------- 3. chrome_history ----------


@tool
async def chrome_history(
    tab_id: int,
    direction: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Move a Chrome tab one step back or forward in its history.

    direction: "back" or "forward".
    """
    return await _dispatch(
        command_type="history",
        args={"tab_id": tab_id, "direction": direction},
        config=config,
    )


# ---------- 4. chrome_snapshot ----------


@tool
async def chrome_snapshot(
    tab_id: int,
    detail: str = "interactive",
    scope_selector: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Read the accessibility tree of a Chrome tab.

    Returns a compact YAML-style snapshot with ``[ref=e1]`` annotations
    on interactive elements. Use the ref tags as targets for chrome_act.

    detail: "interactive" (default), "full", or "minimal".
    scope_selector: optional CSS selector to scope the snapshot.
    """
    args: dict[str, Any] = {"tab_id": tab_id, "detail": detail}
    if scope_selector is not None:
        args["scope_selector"] = scope_selector
    return await _dispatch(command_type="snapshot", args=args, config=config)


# ---------- 5. chrome_act ----------


@tool
async def chrome_act(
    tab_id: int,
    target: str,
    method: str,
    value: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Perform an action against a Chrome element.

    target: snapshot ref like ``"@e5"``, or ``"css=.btn"``, or
        ``"xpath=//button"``.
    method: one of "click", "fill", "select", "hover", "check", "uncheck",
        "scroll_into_view".
    value: required for "fill" (text to type) and "select" (option label/value).
    """
    args: dict[str, Any] = {"tab_id": tab_id, "target": target, "method": method}
    if value is not None:
        args["value"] = value
    return await _dispatch(command_type="act", args=args, config=config)


# ---------- 6. chrome_press_key ----------


@tool
async def chrome_press_key(
    tab_id: int,
    key: str,
    modifiers: Optional[list[str]] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Press a key in a Chrome tab.

    key: CDP key name ("Enter", "Tab", "Escape", "ArrowDown", "a", "1", …).
    modifiers: subset of ["Ctrl", "Shift", "Alt", "Meta"].
    """
    args: dict[str, Any] = {"tab_id": tab_id, "key": key}
    if modifiers:
        args["modifiers"] = modifiers
    return await _dispatch(command_type="press_key", args=args, config=config)


# ---------- 7. chrome_scroll ----------


@tool
async def chrome_scroll(
    tab_id: int,
    direction: str,
    amount_px: int = 500,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Scroll a Chrome tab.

    direction: "up", "down", "left", or "right".
    amount_px: pixels to scroll (default 500).
    """
    return await _dispatch(
        command_type="scroll",
        args={"tab_id": tab_id, "direction": direction, "amount_px": amount_px},
        config=config,
    )


# ---------- 8. chrome_extract_text ----------


@tool
async def chrome_extract_text(
    tab_id: int,
    selector: Optional[str] = None,
    max_chars: int = 50000,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Return the visible text content of a Chrome tab or scoped element.

    selector: optional CSS selector. None means document.body.
    max_chars: hard cap on returned text length.
    """
    args: dict[str, Any] = {"tab_id": tab_id, "max_chars": max_chars}
    if selector is not None:
        args["selector"] = selector
    return await _dispatch(command_type="extract_text", args=args, config=config)


# ---------- 9. chrome_screenshot ----------


@tool
async def chrome_screenshot(
    tab_id: int,
    full_page: bool = False,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Capture a PNG screenshot of a Chrome tab.

    full_page: when True, stitches the full scrollable area; otherwise
    captures the visible viewport only.

    Returns JSON containing base64-encoded PNG bytes.
    """
    return await _dispatch(
        command_type="screenshot",
        args={"tab_id": tab_id, "full_page": full_page},
        config=config,
    )


# ---------- 10. chrome_console ----------


@tool
async def chrome_console(
    tab_id: int,
    clear: bool = False,
    only_errors: bool = False,
    limit: int = 50,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Read buffered console messages and uncaught exceptions for a Chrome tab.

    Use this to detect silent click failures where a click "succeeded"
    but a fetch errored or JS threw.

    clear: when True, drop the buffer after returning it.
    only_errors: when True, only return console.error and uncaught exceptions.
    limit: max number of messages to return.
    """
    return await _dispatch(
        command_type="console",
        args={
            "tab_id": tab_id,
            "clear": clear,
            "only_errors": only_errors,
            "limit": limit,
        },
        config=config,
    )


# ---------- 11. chrome_dialog ----------


@tool
async def chrome_dialog(
    tab_id: int,
    action: str,
    prompt_text: Optional[str] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Handle a pending native JS dialog (alert/confirm/prompt) in a Chrome tab.

    action: "accept" or "dismiss".
    prompt_text: text to type into a JS prompt() dialog before accepting.
    """
    args: dict[str, Any] = {"tab_id": tab_id, "action": action}
    if prompt_text is not None:
        args["prompt_text"] = prompt_text
    return await _dispatch(command_type="dialog", args=args, config=config)


# ---------- 12. chrome_cdp ----------


@tool
async def chrome_cdp(
    tab_id: int,
    method: str,
    params: Optional[dict] = None,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Raw Chrome DevTools Protocol escape hatch. Last-resort tool.

    Use when no other ``chrome_*`` tool covers what you need (e.g.
    Network.setExtraHTTPHeaders, Page.setBypassCSP, Emulation.setDeviceMetrics).
    Most agents should NOT reach for this; prefer the typed tools.

    method: CDP method name, e.g. "Page.captureScreenshot".
    params: method parameters object.
    """
    return await _dispatch(
        command_type="cdp",
        args={"tab_id": tab_id, "method": method, "params": params or {}},
        config=config,
    )


CHROME_BROWSER_TOOLS = [
    chrome_tabs,
    chrome_navigate,
    chrome_history,
    chrome_snapshot,
    chrome_act,
    chrome_press_key,
    chrome_scroll,
    chrome_extract_text,
    chrome_screenshot,
    chrome_console,
    chrome_dialog,
    chrome_cdp,
]


__all__ = [
    "CHROME_BROWSER_TOOLS",
    "chrome_tabs",
    "chrome_navigate",
    "chrome_history",
    "chrome_snapshot",
    "chrome_act",
    "chrome_press_key",
    "chrome_scroll",
    "chrome_extract_text",
    "chrome_screenshot",
    "chrome_console",
    "chrome_dialog",
    "chrome_cdp",
]
