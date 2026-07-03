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
from typing import Callable, Iterable, Optional, cast

from ..hook_spec import plane_for
from .actions import ACTIONS
from .base import HookContext, HookEvent, HookOutcome
from .registry import HookRegistry

logger = logging.getLogger(__name__)


def _make_action_fn(definition):
    """Build the per-definition hook callable.

    A dedicated function (not an inline loop closure) so each callable binds its
    own ``definition``/``params`` rather than the loop variable. The action's
    params are the logic variant's fields minus the ``action`` discriminator,
    plus bridge-injected ``__definition_id``/``__definition_name`` metadata
    (never persisted; actions that surface their hook's identity, e.g.
    ``require_approval``'s pending record, read them, others ignore them).

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

    if inspect.iscoroutinefunction(action):
        async def _afn(ctx: HookContext) -> Optional[HookOutcome]:
            return await action(ctx, params)

        _afn.__name__ = f"{definition.logic.action}[{definition.name or definition.id}]"
        return _afn

    def _fn(ctx: HookContext) -> Optional[HookOutcome]:
        # Sync branch: the coroutine case returned above, so this cannot be
        # an awaitable at runtime.
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
        fn = _make_action_fn(definition)
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
