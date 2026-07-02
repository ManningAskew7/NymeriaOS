"""Single source of truth for the lifecycle-hook action/event taxonomy.

A pure-data leaf (stdlib only) importable by BOTH the store layer
(``core/hook_manager.py``, which must not import the hooks engine) and the
engine (``core/hooks/actions.py``) without creating a cycle. The maps that were
previously hand-synced (``EVENT_ACTIONS``/``TEXT_ACTIONS`` in the store,
``ACTION_PLANES`` in the engine) now derive from ``ACTION_SPECS`` here;
``GET /hooks/schema`` exposes the taxonomy to clients, and
``tests/test_hook_spec.py`` pins the remaining independent copies (the engine's
``ACTIONS`` function table, the store's ``HOOK_LOGIC_BY_ACTION`` variants, the
frontend ``HOOK_EVENT_ACTIONS`` map) in lockstep.

Adding an ACTION: one ``ActionSpec`` here, the function + ``ACTIONS`` entry in
``core/hooks/actions.py``, a logic variant + ``HOOK_LOGIC_BY_ACTION`` entry
(+ union member) in ``core/hook_manager.py``, and, if it authors through new
flat fields, ``params_from_fields``. Adding an EVENT additionally needs the
engine contract (``core/hooks/base.py`` enum/outcome/reduce), a fire point, and
the ``HookEventName`` Literal in the store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set, Tuple

# The fixed event set (mirrors core/hooks/base.py HookEvent values; pinned by
# tests/test_hook_spec.py).
EVENTS: Tuple[str, ...] = ("prompt_submit", "pre_tool_use", "post_tool_use", "done")

# Events that fire around a tool call, where a tool-name matcher applies.
TOOL_EVENTS: Tuple[str, ...] = ("pre_tool_use", "post_tool_use")


@dataclass(frozen=True)
class ActionSpec:
    """One canned action's place in the taxonomy."""

    name: str
    plane: str  # "mutate" | "observe"
    events: Tuple[str, ...]  # events the action may attach to
    text_action: bool = False  # sole config is a single `text` field (alias-authorable)


ACTION_SPECS: Dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        ActionSpec(
            "inject_context", "mutate",
            ("prompt_submit", "post_tool_use", "done"), text_action=True,
        ),
        ActionSpec("block_if_matches", "mutate", ("pre_tool_use",)),
        ActionSpec("rewrite_arg", "mutate", ("pre_tool_use",)),
        ActionSpec("notify", "observe", ("post_tool_use", "done"), text_action=True),
        ActionSpec("create_todo", "observe", ("post_tool_use", "done"), text_action=True),
        ActionSpec("webhook", "observe", ("post_tool_use", "done")),
    )
}


def event_actions() -> Dict[str, Set[str]]:
    """Derive the event -> legal-action-set map (``hook_manager.EVENT_ACTIONS``)."""
    out: Dict[str, Set[str]] = {event: set() for event in EVENTS}
    for spec in ACTION_SPECS.values():
        for event in spec.events:
            out[event].add(spec.name)
    return out


def action_planes() -> Dict[str, str]:
    """Derive the action -> plane map (``core/hooks/actions.ACTION_PLANES``)."""
    return {name: spec.plane for name, spec in ACTION_SPECS.items()}


def text_actions() -> Tuple[str, ...]:
    """Actions whose sole logic config is a single ``text`` field."""
    return tuple(name for name, spec in ACTION_SPECS.items() if spec.text_action)
