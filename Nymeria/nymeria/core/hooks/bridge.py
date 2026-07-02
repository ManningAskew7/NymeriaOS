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

import logging
from typing import Callable, Iterable, Optional

from .actions import ACTION_PLANES, ACTIONS
from .base import HookContext, HookEvent, HookOutcome
from .registry import HookRegistry

logger = logging.getLogger(__name__)


def _make_action_fn(definition):
    """Build the per-definition hook callable.

    A dedicated function (not an inline loop closure) so each callable binds its
    own ``definition``/``params`` rather than the loop variable. The action's
    params are the logic variant's fields minus the ``action`` discriminator, so
    each action function receives exactly its own params.
    """
    action = ACTIONS.get(definition.logic.action)
    if action is None:
        return None
    try:
        params = definition.logic.model_dump(exclude={"action"})
    except Exception:  # noqa: BLE001 - a malformed logic must not crash the build
        params = {}

    def _fn(ctx: HookContext) -> Optional[HookOutcome]:
        return action(ctx, params)

    _fn.__name__ = f"{definition.logic.action}[{definition.name or definition.id}]"
    return _fn


def build_registry(
    definitions: Iterable,
    recorder: Optional[Callable[..., None]] = None,
) -> HookRegistry:
    """Build a fresh registry registering one hook per definition.

    Each hook lands on its action's plane (``ACTION_PLANES``): mutate-plane
    actions return an in-band outcome; observe-plane actions run fire-and-forget.
    ``recorder`` (usually ``hook_manager.make_execution_recorder``) is attached
    to the registry so dispatch reports each run to the per-user execution log;
    None records nothing.
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
        observe = ACTION_PLANES.get(definition.logic.action, "mutate") == "observe"
        registry.register(
            event,
            fn,
            matcher=definition.matcher,
            name=definition.name or definition.id,
            observe=observe,
            definition_id=definition.id,
        )
    return registry
