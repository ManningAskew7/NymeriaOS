"""Bridge: turn stored hook definitions into an active in-process registry.

This is the seam between the persisted product surface (``core/hook_manager``)
and the store-agnostic engine (``core/hooks``). It builds a fresh
``HookRegistry`` from a list of definitions the caller has already filtered
(scope + enabled). The dispatch engine never learns about the store; the caller
threads the built registry to the fire points for that turn.

Definitions are duck-typed: each needs ``event`` (str), ``matcher``,
``logic.action``, ``logic.text``, ``name``, and ``id``.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from .actions import ACTIONS
from .base import HookContext, HookEvent, HookOutcome
from .registry import HookRegistry

logger = logging.getLogger(__name__)


def _make_action_fn(definition):
    """Build the per-definition hook callable.

    A dedicated function (not an inline loop closure) so each callable binds its
    own ``definition``/``params`` rather than the loop variable.
    """
    action = ACTIONS.get(definition.logic.action)
    if action is None:
        return None
    params = {"text": definition.logic.text}

    def _fn(ctx: HookContext) -> Optional[HookOutcome]:
        return action(ctx, params)

    _fn.__name__ = f"{definition.logic.action}[{definition.name or definition.id}]"
    return _fn


def build_registry(definitions: Iterable) -> HookRegistry:
    """Build a fresh registry registering one mutate-plane hook per definition."""
    registry = HookRegistry()
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
        registry.register(
            event,
            fn,
            matcher=definition.matcher,
            name=definition.name or definition.id,
            observe=False,
        )
    return registry
