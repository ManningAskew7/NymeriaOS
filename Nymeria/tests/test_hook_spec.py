"""Lockstep tests for the hook taxonomy single source (core/hook_spec.py).

The taxonomy has unavoidable independent copies (the engine's ``ACTIONS``
function table, the store's ``HOOK_LOGIC_BY_ACTION`` variants and
``HookEventName`` Literal, the frontend ``HOOK_EVENT_ACTIONS`` map). These
tests pin every copy reachable from the backend checkout so a new action or
event cannot land half-wired.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from nymeria.core.hook_manager import (
    EVENT_ACTIONS,
    HOOK_LOGIC_BY_ACTION,
    TEXT_ACTIONS,
    HookEventName,
)
from nymeria.core.hook_spec import (
    ACTION_SPECS,
    EVENTS,
    TOOL_EVENTS,
    action_planes,
    event_actions,
    text_actions,
)
from nymeria.core.hooks import ACTION_PLANES, ACTIONS
from nymeria.core.hooks.base import EVENT_OUTCOME_TYPES


# --- backend lockstep --------------------------------------------------------

def test_action_specs_match_engine_action_table():
    assert set(ACTION_SPECS) == set(ACTIONS)


def test_action_specs_match_logic_variants():
    assert set(ACTION_SPECS) == set(HOOK_LOGIC_BY_ACTION)


def test_action_planes_derive_from_specs():
    assert ACTION_PLANES == action_planes()
    assert set(ACTION_PLANES.values()) <= {"mutate", "observe"}


def test_event_actions_derive_from_specs():
    assert EVENT_ACTIONS == event_actions()
    assert TEXT_ACTIONS == text_actions()


def test_events_cover_engine_event_set():
    assert set(EVENTS) == {e.value for e in EVENT_OUTCOME_TYPES}
    assert set(get_args(HookEventName)) == set(EVENTS)
    assert set(TOOL_EVENTS) <= set(EVENTS)


def test_every_spec_event_is_known_and_nonempty():
    for spec in ACTION_SPECS.values():
        assert spec.events, spec.name
        assert set(spec.events) <= set(EVENTS), spec.name
        assert spec.plane in ("mutate", "observe"), spec.name


def test_text_actions_have_a_text_field():
    for action in TEXT_ACTIONS:
        assert "text" in HOOK_LOGIC_BY_ACTION[action].model_fields, action


def test_plane_for_resolves_per_event():
    from nymeria.core.hook_spec import plane_by_event, plane_for
    # Single-plane actions report their base plane on every legal event.
    assert plane_for("inject_context", "prompt_submit") == "mutate"
    assert plane_for("notify", "done") == "observe"
    # run_command flips: mutate on the in-band events, observe on the after events.
    assert plane_for("run_command", "prompt_submit") == "mutate"
    assert plane_for("run_command", "pre_tool_use") == "mutate"
    assert plane_for("run_command", "post_tool_use") == "observe"
    assert plane_for("run_command", "done") == "observe"
    # Unknown action falls back to mutate; plane_by_event covers all legal events.
    assert plane_for("nope", "done") == "mutate"
    assert plane_by_event("run_command") == {
        "prompt_submit": "mutate",
        "pre_tool_use": "mutate",
        "post_tool_use": "observe",
        "done": "observe",
    }


def test_observe_events_are_a_subset_of_events():
    for spec in ACTION_SPECS.values():
        assert set(spec.observe_events) <= set(spec.events), spec.name


# --- frontend lockstep ---------------------------------------------------------

_FRONTEND_TAXONOMY = (
    Path(__file__).resolve().parents[2]
    / "nymeria-desktop" / "src" / "lib" / "utils" / "hooks.ts"
)


def _parse_frontend_event_actions(text: str) -> dict:
    block = re.search(r"HOOK_EVENT_ACTIONS[^=]*=\s*\{(.*?)\n\};", text, re.DOTALL)
    assert block, "HOOK_EVENT_ACTIONS not found in utils/hooks.ts"
    out: dict = {}
    for line in block.group(1).splitlines():
        m = re.match(r"\s*(\w+):\s*\[(.*)\],?\s*$", line)
        if m:
            out[m.group(1)] = {
                v.strip().strip("'\"") for v in m.group(2).split(",") if v.strip()
            }
    return out


def test_frontend_taxonomy_matches_backend():
    """The desktop HOOK_EVENT_ACTIONS map must equal backend EVENT_ACTIONS.

    utils/hooks.ts is EXACT_MATCH in scripts/check_cross_app_drift.py, so
    desktop parity covers the mobile copy transitively.
    """
    if not _FRONTEND_TAXONOMY.exists():
        pytest.skip("frontend tree not present in this checkout")
    parsed = _parse_frontend_event_actions(
        _FRONTEND_TAXONOMY.read_text(encoding="utf-8")
    )
    assert parsed == {event: set(actions) for event, actions in EVENT_ACTIONS.items()}
