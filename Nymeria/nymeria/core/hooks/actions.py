"""Canned hook actions: first-party logic that produces a typed outcome.

This pass ships one action, ``inject_context``: it renders a (static or
templated) string from the ``HookContext`` and returns the outcome that injects
it, per event:

- ``PROMPT_SUBMIT`` -> ``PromptOutcome(inject_context=text)`` (model-facing tail).
- ``POST_TOOL_USE`` -> ``PostToolOutcome(additional_context=text)`` (tool result).
- ``DONE`` -> ``DoneOutcome(continue_=True, reason=text)`` (in-loop re-drive via
  the existing enqueue path).

Actions are pure ``(ctx, params)`` functions over the frozen contract: no store,
registry, or agent handle. Template substitution uses ``core/text_format.py`` so
static text passes through unchanged and unknown ``{placeholders}`` are left
intact rather than raising.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Dict, Optional

from ..text_format import safe_format
from .base import (
    DoneOutcome,
    HookContext,
    HookEvent,
    HookOutcome,
    PostToolOutcome,
    PromptOutcome,
)

logger = logging.getLogger(__name__)


def _template_vars(ctx: HookContext) -> Dict[str, str]:
    """Build the string substitution map from a HookContext.

    Every value is coerced to a string (``None`` -> ""), so a referenced-but-
    unset field renders empty rather than leaking a live object or the literal
    placeholder.
    """
    try:
        tool_args = json.dumps(ctx.tool_args, default=str) if ctx.tool_args else ""
    except Exception:  # noqa: BLE001 - templating must never raise
        tool_args = ""
    return {
        "event": ctx.event.value if isinstance(ctx.event, HookEvent) else str(ctx.event),
        "thread_id": ctx.thread_id or "",
        "user_id": ctx.user_id or "",
        "is_autonomous": str(ctx.is_autonomous),
        "holder_kind": ctx.holder_kind or "",
        "trigger_label": ctx.trigger_label or "",
        "prompt": ctx.prompt or "",
        "tool_name": ctx.tool_name or "",
        "tool_result": ctx.tool_result_text or "",
        "tool_status": ctx.tool_status or "",
        "tool_args": tool_args,
        "final_text": ctx.final_text or "",
    }


def inject_context(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Render ``params['text']`` and return the injecting outcome for the event."""
    template = (params or {}).get("text")
    if not template:
        return None
    text = safe_format(str(template), _template_vars(ctx)).strip()
    if not text:
        return None  # nothing to inject / no empty DONE reason
    event = ctx.event
    if event is HookEvent.PROMPT_SUBMIT:
        return PromptOutcome(inject_context=text)
    if event is HookEvent.POST_TOOL_USE:
        return PostToolOutcome(additional_context=text)
    if event is HookEvent.DONE:
        # One-shot: inject the text as a single follow-up re-drive, never loop.
        # On a continuation turn this hook itself spawned, done_continuation_active
        # is True (mirrors Claude Code's stop_hook_active) -- return None so we do
        # not re-continue and ride the hard cap up to MAX_DONE_CONTINUATIONS.
        prov = ctx.provenance
        if prov is not None and prov.done_continuation_active:
            return None
        return DoneOutcome(continue_=True, reason=text)
    return None  # PRE_TOOL_USE and anything else: not an injection target


# The action table. The bridge looks actions up by name; adding an action is a
# one-line addition here plus an ``EVENT_ACTIONS`` entry in ``hook_manager``.
ActionFn = Callable[[HookContext, dict], Optional[HookOutcome]]
ACTIONS: Dict[str, ActionFn] = {
    "inject_context": inject_context,
}
