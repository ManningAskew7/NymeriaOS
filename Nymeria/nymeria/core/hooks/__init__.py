"""Lifecycle-hooks engine.

Public surface for the fire points and tests: the frozen contract (``base``),
the in-process registry, the two dispatch planes, the canned actions, and the
store->registry bridge. The persisted record + authoring surfaces live in
``core/hook_manager.py`` (plus the ``/hooks`` REST router, the ``hook_config``
tool, and the ``/hook`` command); the enable model lives in
``core/agent_safety.py``. The ``nym`` workflow substrate (a second logic
substrate behind the same contract) is the remaining deferred pass.

Design doc: ``docs/private/plans/lifecycle-hooks.md``.
"""

from __future__ import annotations

from .base import (
    EVENT_OUTCOME_TYPES,
    DoneOutcome,
    HookContext,
    HookEvent,
    HookOutcome,
    HookProvenance,
    PostToolOutcome,
    PreToolOutcome,
    PromptOutcome,
)
from .dispatch import (
    adispatch,
    adispatch_observe,
    default_registry,
    default_scratch,
    dispatch,
    dispatch_observe,
    register,
    reset,
    tool_hooks_active,
    unregister,
)
from .actions import (
    ACTION_PLANES,
    ACTIONS,
    block_if_matches,
    create_todo,
    inject_context,
    notify,
    rewrite_arg,
    webhook,
)
from .bridge import build_registry
from .registry import HookRegistry, Registration
from .scratch import ScratchStore

__all__ = [
    # contract
    "HookEvent",
    "HookProvenance",
    "HookContext",
    "HookOutcome",
    "PromptOutcome",
    "PreToolOutcome",
    "PostToolOutcome",
    "DoneOutcome",
    "EVENT_OUTCOME_TYPES",
    # registry / scratch
    "HookRegistry",
    "Registration",
    "ScratchStore",
    # dispatch
    "adispatch",
    "dispatch",
    "adispatch_observe",
    "dispatch_observe",
    "register",
    "unregister",
    "reset",
    "tool_hooks_active",
    "default_registry",
    "default_scratch",
    # actions + bridge (product surface)
    "inject_context",
    "block_if_matches",
    "rewrite_arg",
    "notify",
    "create_todo",
    "webhook",
    "ACTIONS",
    "ACTION_PLANES",
    "build_registry",
]
