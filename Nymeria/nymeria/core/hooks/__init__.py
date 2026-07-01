"""Lifecycle-hooks engine (spine pass).

Public surface for the fire points and tests. The engine ships the machinery
only: the frozen contract, an in-process registry, and the dispatch planes. The
persisted record, authoring surface, canned actions, presets, enable model, and
``nym`` workflow substrate are later passes on top of this contract.

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
