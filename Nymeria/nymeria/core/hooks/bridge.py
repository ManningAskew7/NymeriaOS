"""Bridge: turn stored hook definitions into an active in-process registry.

This is the seam between the persisted product surface (``core/hook_manager``)
and the store-agnostic engine (``core/hooks``). It builds a fresh
``HookRegistry`` from a list of definitions the caller has already filtered
(scope + enabled). The dispatch engine never learns about the store; the caller
threads the built registry to the fire points for that turn.

Definitions are duck-typed: each needs ``event`` (str), ``matcher``, a
``logic`` pydantic model (with ``action`` + per-action params), ``name``, and
``id``.
"""

from __future__ import annotations

import inspect
import logging
from typing import Callable, Iterable, Optional, Tuple, cast

from ..conditions import evaluate_conditions
from ..hook_spec import plane_for
from .actions import ACTIONS, _coerce_conditions, context_usage_fields
from .base import EVENT_OUTCOME_TYPES, HookContext, HookEvent, HookOutcome
from .registry import HookRegistry

logger = logging.getLogger(__name__)

# Scratch key prefix for the per-definition ``once`` sentinel.
_ONCE_KEY_PREFIX = "hook_once:"

# The static field surface of ``fire_condition_data`` (for the /hooks/schema
# taxonomy and docs): meta fields are always present; context fields appear
# when the fire point knows them; tool args ride under ``args.<name>``.
FIRE_CONDITION_META_FIELDS = (
    "event", "thread_id", "user_id", "is_autonomous", "holder_kind",
    "trigger_label", "tool_name", "tool_status", "prompt", "final_text",
)
FIRE_CONDITION_CONTEXT_FIELDS = (
    "context_tokens", "context_limit", "compact_trigger_tokens",
    "context_pct_of_trigger", "context_pct_of_limit",
)


def fire_condition_data(ctx: HookContext) -> dict:
    """The data dict a definition-level fire gate evaluates against.

    Meta fields live at the top level; tool args nest under ``args`` (the
    evaluator's dotted paths resolve them, e.g. ``args.command``) so a tool arg
    can never shadow a meta field; the numeric context-usage fields are raw
    numbers so the numeric operators compare numerically (absent when unknown,
    which makes a numeric condition a non-match rather than a compare vs 0).
    """
    data: dict = {
        "event": ctx.event.value if isinstance(ctx.event, HookEvent) else str(ctx.event),
        "thread_id": ctx.thread_id or "",
        "user_id": ctx.user_id or "",
        "is_autonomous": ctx.is_autonomous,
        "holder_kind": ctx.holder_kind or "",
        "trigger_label": ctx.trigger_label or "",
        "tool_name": ctx.tool_name or "",
        "tool_status": ctx.tool_status or "",
        "prompt": ctx.prompt or "",
        "final_text": ctx.final_text or "",
        "args": dict(ctx.tool_args) if isinstance(ctx.tool_args, dict) else {},
    }
    data.update(context_usage_fields(ctx))
    return data


class _FireGate:
    """Definition-level WHEN gate: ``fire_conditions`` + ``once`` semantics.

    Runs in the bridge wrapper BEFORE the hook's logic, so it gates every logic
    substrate uniformly (canned actions now, the workflow substrate later) at
    in-process cost. ``once`` is a per-thread scratch sentinel: set when the
    gate fires, cleared (re-armed) when the conditions stop matching. Sentinel
    writes travel as ``scratch_patch`` on the outcome (the contract's
    writes-in-outcome rule), riding a synthesized no-op outcome of the event's
    type when there is nothing else to return. Never raises: a malformed gate
    makes the hook a no-op, mirroring the guardrail actions' posture.
    """

    def __init__(self, definition, event: HookEvent) -> None:
        # None = malformed shape (hook stays silent); [] = no conditions.
        self.conditions = _coerce_conditions(getattr(definition, "fire_conditions", None))
        self.once = bool(getattr(definition, "once", False))
        self.key = f"{_ONCE_KEY_PREFIX}{definition.id}"
        self.outcome_type = EVENT_OUTCOME_TYPES[event]

    @property
    def active(self) -> bool:
        return self.conditions is None or bool(self.conditions) or self.once

    def _noop(self, patch: dict) -> HookOutcome:
        # Every outcome type defaults to a no-op (allow / nothing injected /
        # no continuation), so it can carry a scratch patch without affecting
        # the turn; reduction filters it out.
        return self.outcome_type(scratch_patch=patch)

    def check(self, ctx: HookContext) -> Tuple[bool, Optional[HookOutcome]]:
        """Return ``(run_logic, short_circuit_outcome)``. Never raises."""
        if self.conditions is None:
            return False, None  # malformed fire_conditions -> hook is a no-op
        try:
            matched = evaluate_conditions(fire_condition_data(ctx), self.conditions)
        except Exception:  # noqa: BLE001 - a gate must never crash a turn
            logger.warning("hook fire gate evaluation failed; skipping hook", exc_info=True)
            return False, None
        fired = bool(ctx.scratch.get(self.key)) if self.once else False
        if not matched:
            if fired:
                return False, self._noop({self.key: False})  # re-arm
            return False, None
        if fired:
            return False, None  # already fired this crossing
        return True, None

    def stamp(self, outcome: Optional[HookOutcome]) -> Optional[HookOutcome]:
        """Ensure a fired ``once`` gate persists its sentinel on the outcome.

        Deliberate asymmetry: an action that RAISES (fault-policied by the
        dispatcher) or returns an illegal outcome type (dropped by ``_accept``)
        never reaches a persisted sentinel, so a failed fire does not consume
        the shot and the hook re-fires on the next matching event. A
        persistently failing PRE guardrail therefore keeps failing closed
        rather than silently disarming after one crash.
        """
        if not self.once:
            return outcome
        if outcome is None:
            return self._noop({self.key: True})
        if isinstance(outcome, self.outcome_type):
            patch = getattr(outcome, "scratch_patch", None)
            if isinstance(patch, dict):
                patch.setdefault(self.key, True)
            elif patch is None:
                outcome.scratch_patch = {self.key: True}
        return outcome


def _make_action_fn(definition, event: HookEvent):
    """Build the per-definition hook callable.

    A dedicated function (not an inline loop closure) so each callable binds its
    own ``definition``/``params`` rather than the loop variable. The action's
    params are the logic variant's fields minus the ``action`` discriminator,
    plus bridge-injected ``__definition_id``/``__definition_name`` metadata
    (never persisted; actions that surface their hook's identity, e.g.
    ``require_approval``'s pending record, read them, others ignore them).

    The definition-level fire gate (``fire_conditions``/``once``) wraps the
    action here, so it applies to every substrate; definitions without a gate
    take the ungated fast path unchanged.

    A coroutine action gets an ``async`` wrapper so the dispatcher still sees
    a coroutine function (a sync wrapper would hide it from
    ``inspect.iscoroutinefunction`` and the pool offload would mis-treat the
    returned coroutine object as an outcome).
    """
    action = ACTIONS.get(definition.logic.action)
    if action is None:
        return None
    try:
        params = definition.logic.model_dump(exclude={"action"})
    except Exception:  # noqa: BLE001 - a malformed logic must not crash the build
        params = {}
    params["__definition_id"] = definition.id
    params["__definition_name"] = definition.name or definition.id
    gate: Optional[_FireGate] = _FireGate(definition, event)
    if gate is not None and not gate.active:
        gate = None

    if inspect.iscoroutinefunction(action):
        async def _afn(ctx: HookContext) -> Optional[HookOutcome]:
            if gate is not None:
                run, short = gate.check(ctx)
                if not run:
                    return short
                return gate.stamp(await action(ctx, params))
            return await action(ctx, params)

        _afn.__name__ = f"{definition.logic.action}[{definition.name or definition.id}]"
        return _afn

    def _fn(ctx: HookContext) -> Optional[HookOutcome]:
        # Sync branch: the coroutine case returned above, so this cannot be
        # an awaitable at runtime.
        if gate is not None:
            run, short = gate.check(ctx)
            if not run:
                return short
            return gate.stamp(cast(Optional[HookOutcome], action(ctx, params)))
        return cast(Optional[HookOutcome], action(ctx, params))

    _fn.__name__ = f"{definition.logic.action}[{definition.name or definition.id}]"
    return _fn


def build_registry(
    definitions: Iterable,
    recorder: Optional[Callable[..., None]] = None,
) -> HookRegistry:
    """Build a fresh registry registering one hook per definition.

    Each hook lands on the plane resolved for its (action, event) pair
    (``plane_for``): mutate-plane hooks return an in-band outcome; observe-plane
    hooks run fire-and-forget. Most actions are single-plane; ``run_command``
    flips per event. ``recorder`` (usually
    ``hook_manager.make_execution_recorder``) is attached to the registry so
    dispatch reports each run to the per-user execution log; None records
    nothing.
    """
    registry = HookRegistry()
    registry.recorder = recorder
    for definition in definitions:
        try:
            event = HookEvent(definition.event)
        except ValueError:
            logger.warning("hook %r has unknown event %r; skipping", definition.id, definition.event)
            continue
        fn = _make_action_fn(definition, event)
        if fn is None:
            logger.warning(
                "hook %r has unknown action %r; skipping", definition.id, definition.logic.action
            )
            continue
        observe = plane_for(definition.logic.action, definition.event) == "observe"
        registry.register(
            event,
            fn,
            matcher=definition.matcher,
            name=definition.name or definition.id,
            observe=observe,
            definition_id=definition.id,
            timeout=_reg_timeout(definition),
        )
    return registry


def _reg_timeout(definition) -> Optional[float]:
    """Per-hook dispatcher budget for a definition, or None for the default.

    ``run_command`` carries an author-configured ``timeout_seconds``; the
    dispatcher budget is set a hair above it (+0.5s) so the action's own
    subprocess kill fires first and returns a clean outcome, rather than the
    dispatcher timing the whole hook out.
    """
    logic = getattr(definition, "logic", None)
    secs = getattr(logic, "timeout_seconds", None)
    if secs is None:
        return None
    try:
        return float(secs) + 0.5
    except (TypeError, ValueError):
        return None
