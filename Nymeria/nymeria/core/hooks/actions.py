"""Canned hook actions: first-party logic that produces a typed outcome.

Each action is a pure ``(ctx, params)`` function over the frozen contract (no
store, registry, or agent handle). Actions in this module:

- ``inject_context`` (mutate): render a string and inject it, per event:
  ``PROMPT_SUBMIT`` -> ``PromptOutcome``, ``POST_TOOL_USE`` -> ``PostToolOutcome``
  (additional_context), ``DONE`` -> ``DoneOutcome`` (in-loop re-drive).
- ``block_if_matches`` (mutate, pre_tool_use): deny the tool call when all
  conditions match its args -> ``PreToolOutcome(decision="deny")``.
- ``rewrite_arg`` (mutate, pre_tool_use): rewrite matched args ->
  ``PreToolOutcome(decision="modify", updated_args=...)``.

Template substitution uses ``core/text_format.py`` so static text passes through
unchanged and unknown ``{placeholders}`` are left intact rather than raising.
Condition matching uses ``core/conditions.py`` (shared with the trigger stack).

Fault policy note: a raising PRE hook is treated as a ``deny`` (fail-closed) by
the dispatcher, so the guardrail actions are written never-raise -- a malformed
condition/params makes the hook a no-op (allow), not a block-everything.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Dict, Optional

from ..conditions import HookCondition, evaluate_conditions
from ..text_format import safe_format
from .base import (
    DoneOutcome,
    HookContext,
    HookEvent,
    HookOutcome,
    PostToolOutcome,
    PreToolOutcome,
    PromptOutcome,
)

logger = logging.getLogger(__name__)


def _coerce_conditions(raw) -> Optional[list]:
    """Build ``HookCondition`` objects from a params list; None on bad shape.

    Returns None (not a partial/garbage list) for anything that is not a list of
    dicts / HookConditions, so the caller no-ops (allow) instead of letting a bad
    element reach ``evaluate_conditions`` and raise -- which, under PRE
    fail-closed, would deny every matching tool call. In practice the CRUD/load
    validators reject non-dict conditions upstream; this is the backstop.
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        return None
    out = []
    try:
        for c in raw:
            if isinstance(c, HookCondition):
                out.append(c)
            elif isinstance(c, dict):
                out.append(HookCondition(**c))
            else:
                return None  # a non-dict element is a bad shape -> no-op
    except Exception:  # noqa: BLE001 - a malformed condition must not raise here
        return None
    return out


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


def block_if_matches(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Deny a tool call when all conditions match its args (else allow).

    Empty conditions = always deny (a blanket guardrail on the matched tools).
    Never raises: malformed conditions make the hook a no-op rather than a
    fail-closed block of every call.
    """
    params = params or {}
    conds = _coerce_conditions(params.get("conditions"))
    if conds is None:
        return None
    if not evaluate_conditions(ctx.tool_args or {}, conds):
        return None  # conditions not met -> allow
    reason = safe_format(
        str(params.get("reason") or "blocked by a lifecycle hook"), _template_vars(ctx)
    )
    return PreToolOutcome(decision="deny", reason=reason)


def rewrite_arg(ctx: HookContext, params: dict) -> Optional[HookOutcome]:
    """Rewrite one or more tool-call args when conditions match (else no-op).

    ``updates`` is ``arg-name -> {placeholder}-templated value``; only the named
    args are changed (the dispatcher shallow-merges them over the call's args).
    Empty conditions = always rewrite. Never raises.
    """
    params = params or {}
    conds = _coerce_conditions(params.get("conditions"))
    if conds is None:
        return None
    if not evaluate_conditions(ctx.tool_args or {}, conds):
        return None
    spec = params.get("updates")
    if not isinstance(spec, dict) or not spec:
        return None
    vars_ = _template_vars(ctx)
    updates = {str(k): safe_format(str(v), vars_) for k, v in spec.items()}
    if not updates:
        return None
    return PreToolOutcome(decision="modify", updated_args=updates)


# The action table. The bridge looks actions up by name; adding an action is a
# one-line addition here plus an ``EVENT_ACTIONS`` entry in ``hook_manager``.
ActionFn = Callable[[HookContext, dict], Optional[HookOutcome]]
ACTIONS: Dict[str, ActionFn] = {
    "inject_context": inject_context,
    "block_if_matches": block_if_matches,
    "rewrite_arg": rewrite_arg,
}

# Each action's dispatch plane. Mutate-plane actions return an in-band outcome
# the fire point applies; observe-plane actions (added in a later slice) run
# fire-and-forget. The bridge reads this to register a hook on the right plane.
ACTION_PLANES: Dict[str, str] = {
    "inject_context": "mutate",
    "block_if_matches": "mutate",
    "rewrite_arg": "mutate",
}
