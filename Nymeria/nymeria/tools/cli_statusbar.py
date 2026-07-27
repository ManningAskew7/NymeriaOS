"""Agent-facing tools that read and reconfigure the user's CLI status bars.

The push half of backlog #53 (CLI modernization Phase 4). Each tool
allocates a ``command_id``, registers a future with the
:class:`CLIConfigCoordinator`, publishes a ``cli_config`` autonomous event
over ``/autonomous/stream``, and awaits the future.

Connected Rich REPL CLI sessions consume the event, apply and persist the
layout change in their local ``~/.nymeria/cli.json``, and POST the outcome
to ``POST /cli-config/{command_id}/result``. The first ack resolves the
tool; a timeout means no CLI client is connected for this user.

CATALOG tools, kept off by default; the opt-in ``cli-customization`` Skill
Kit binds them.
"""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import asyncio
import json
import logging
from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.cli_config_coordinator import (
    get_cli_config_coordinator,
    new_command_id,
)
from ..core.event_bus import publish_autonomous_event
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10

# Kept in sync with the CLI's built-in segment registry
# (triggers/cli/rendering/status_bar.py DEFAULT_SEGMENT_KEYS /
# EXTRA_SEGMENT_KEYS); the CLI re-validates on apply, so drift degrades to
# a clear ack error, not a crash.
_BUILTIN_SEGMENTS = (
    "brand", "activity", "notice", "connection", "model", "fast",
    "reasoning", "thread", "context", "tps", "queued", "cwd",
)
# Valid refs everywhere, excluded from the default top-bar order.
_EXTRA_SEGMENTS = ("turn_time", "cost")
_ALL_SEGMENTS = _BUILTIN_SEGMENTS + _EXTRA_SEGMENTS

# A single one of these as the whole segments list pins the bar empty
# (hides it), mirroring the CLI's is_off_ref_list sentinel.
_OFF_REFS = ("off", "none", "hidden")


def _format_result(payload: dict[str, Any]) -> str:
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

    command_id = new_command_id()
    coord = get_cli_config_coordinator()
    future = coord.register(
        command_id=command_id,
        user_id=user_id,
        thread_id=thread_id,
        command_type=command_type,
        metadata=args,
    )
    try:
        publish_autonomous_event(
            event_type="cli_config",
            thread_id=thread_id,
            user_id=user_id,
            task_id="",
            data={
                "command_id": command_id,
                "command_type": command_type,
                "args": args,
                "timeout_seconds": _TIMEOUT_SECONDS,
            },
        )
    except Exception:
        # No event means no CLI will ever answer; do not leave the future
        # pending until the orphan sweep.
        coord.discard(command_id)
        raise
    try:
        result = await asyncio.wait_for(future, timeout=_TIMEOUT_SECONDS + 1)
    except asyncio.TimeoutError:
        coord.discard(command_id)
        return (
            f"[Error]: No CLI client answered within {_TIMEOUT_SECONDS}s. "
            "The user does not appear to have a Nymeria CLI session open; "
            "status bars only exist in the terminal CLI."
        )
    except asyncio.CancelledError:
        coord.discard(command_id)
        raise
    return _format_result(result)


@tool
async def cli_statusbar_get(
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Read the user's current CLI status-bar layout.

    Returns JSON with the layout: "top" (list of segment refs, or null when
    the built-in default order is active), "under_prompt" (list of segment
    refs; empty means the under-prompt bar is hidden), and "turn" (the
    end-of-turn summary line printed into the transcript: null = default
    segments, empty list = hidden, list = pinned refs), plus the available
    built-in and extra segment keys.

    Requires a connected Nymeria CLI session; errors out after a short
    timeout when none is connected.
    """
    return await _dispatch(command_type="statusbar_get", args={}, config=config)


@tool
async def cli_statusbar_set(
    bar: str,
    segments: list[str],
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Reconfigure one of the user's CLI status bars.

    bar: "top" (the bar above the composer), "under" (a second bar below
    the composer, hidden by default), or "turn" (the end-of-turn summary
    line printed into the transcript after each turn, default
    "turn_time tps").
    segments: ordered segment refs. Each ref is a built-in segment key
    (brand, activity, notice, connection, model, fast, reasoning, thread,
    context, tps, queued, cwd, plus turn_time and cost, which render only
    in explicit layouts) or "text:<literal>" for a static label. An EMPTY
    list resets the bar to its default (top: built-in order, under: hidden,
    turn: default segments). A single "off" hides the bar instead.

    "script:<command>" segments (commands executed periodically on the
    user's machine) can NOT be pushed from here: they are user-installed
    only, via the local /statusbar command. To offer one, tell the user
    the exact /statusbar command to run instead.

    The change applies live in every connected CLI and persists in the
    user's cli.json. Returns the applied layout as JSON, or an error when
    a ref is invalid or no CLI session is connected.
    """
    normalized_bar = str(bar or "").strip().casefold()
    if normalized_bar not in (
        "top", "under", "under_prompt", "bottom", "turn", "summary",
    ):
        return "[Error]: bar must be 'top', 'under', or 'turn'."
    if not isinstance(segments, list) or not all(
        isinstance(ref, str) and ref.strip() for ref in segments
    ):
        return "[Error]: segments must be a list of non-empty strings."
    is_off_sentinel = (
        len(segments) == 1 and segments[0].strip().casefold() in _OFF_REFS
    )
    if is_off_sentinel and normalized_bar == "top":
        return (
            "[Error]: the top bar cannot be hidden; pass an empty segments "
            "list to reset it to the default order instead."
        )
    for ref in [] if is_off_sentinel else segments:
        text = ref.strip()
        if text.startswith("script:"):
            # Security boundary: an agent-pushed script would execute
            # periodically on the user's local machine. The CLI apply
            # branch rejects these too (defense in depth).
            return (
                "[Error]: script: segments cannot be set by the agent; "
                "they run commands on the user's machine, so the user must "
                "add them locally. Tell the user to run: /statusbar set "
                f"{normalized_bar} ... {text} ..."
            )
        if text.startswith("text:"):
            continue
        if text.casefold() not in _ALL_SEGMENTS:
            return (
                f"[Error]: Unknown segment ref: {ref}. Built-in segments: "
                f"{', '.join(_ALL_SEGMENTS)}; custom refs: "
                "text:<literal>."
            )
    return await _dispatch(
        command_type="statusbar_set",
        args={
            "bar": normalized_bar,
            "segments": [ref.strip() for ref in segments],
        },
        config=config,
    )


CLI_STATUSBAR_TOOLS = [cli_statusbar_get, cli_statusbar_set]

__all__ = ["CLI_STATUSBAR_TOOLS", "cli_statusbar_get", "cli_statusbar_set"]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="cli_statusbar", tools=tuple(CLI_STATUSBAR_TOOLS)))
