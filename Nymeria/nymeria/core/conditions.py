"""Shared field-condition model and evaluator.

A ``HookCondition`` is a single ``field / operator / value`` filter evaluated
against a flat (or dotted-nested) data dict. This is the same shape the trigger
stack has always used to filter incoming events; it is lifted here so the
lifecycle-hooks stack (``block_if_matches`` / ``rewrite_arg`` gating on tool
args) and the trigger stack share one implementation with no drift, mirroring
how ``core/text_format.py`` unified template substitution.

``evaluate_conditions`` preserves the trigger evaluator's exact semantics (AND
logic, ``str(...)`` coercion, case-fold unless ``case_sensitive``, regex via
``re.search`` swallowing ``re.error`` to a non-match). The only addition is
optional dotted field paths (``input.command``) which resolve nested dicts;
a top-level key that literally contains a dot still wins, so trigger behavior is
unchanged for the flat events triggers actually carry.
"""

from __future__ import annotations

import re
from typing import Iterable, Literal

from pydantic import BaseModel, Field

# Sentinel distinguishing "key absent" (renders as "") from "key present but
# None" (renders as "None"), matching the trigger evaluator's
# ``str(event.get(field, ""))`` behavior exactly.
_MISSING = object()

ConditionOperator = Literal[
    "equals", "not_equals", "contains", "starts_with", "matches_regex"
]


class HookCondition(BaseModel):
    """A filter condition evaluated against a data dict before an action runs.

    Field/operator/value/case_sensitive are the trigger-condition fields; the
    trigger stack re-exports this class as ``TriggerCondition`` so both stacks
    serialize identically.
    """

    field: str = Field(..., description="Field name to check (supports dotted paths)")
    operator: ConditionOperator = Field(default="contains")  # type: ignore[bad-assignment]
    value: str = Field(default="")
    case_sensitive: bool = Field(default=False)


def _resolve_field(data: dict, field: str):
    """Resolve ``field`` in ``data``.

    A literal top-level key wins first (so a key containing a dot keeps working
    exactly as the trigger evaluator did). Otherwise a dotted path walks nested
    dicts. Returns ``_MISSING`` when the field is absent.
    """
    if field in data:
        return data[field]
    if "." in field:
        current = data
        for part in field.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return _MISSING
        return current
    return _MISSING


def evaluate_conditions(data: dict, conditions: Iterable) -> bool:
    """Return True if ALL conditions pass (AND logic); empty = always True.

    ``conditions`` is any iterable of objects exposing ``field`` / ``operator``
    / ``value`` / ``case_sensitive`` (``HookCondition`` or the trigger alias).
    ``data`` is the dict to match against (e.g. ``ctx.tool_args``).
    """
    for cond in conditions:
        raw = _resolve_field(data, cond.field)
        event_value = str(raw) if raw is not _MISSING else ""
        compare_value = cond.value
        if not cond.case_sensitive:
            event_value = event_value.lower()
            compare_value = compare_value.lower()

        op = cond.operator
        if op == "equals" and event_value != compare_value:
            return False
        elif op == "not_equals" and event_value == compare_value:
            return False
        elif op == "contains" and compare_value not in event_value:
            return False
        elif op == "starts_with" and not event_value.startswith(compare_value):
            return False
        elif op == "matches_regex":
            try:
                flags = 0 if cond.case_sensitive else re.IGNORECASE
                raw_text = str(raw) if raw is not _MISSING else ""
                if not re.search(cond.value, raw_text, flags):
                    return False
            except re.error:
                return False
    return True
