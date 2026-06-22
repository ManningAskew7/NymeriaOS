"""Tests for the shared tool helpers consolidated in ``nymeria.tools.utils``."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from nymeria.core.agent import get_current_agent
from nymeria.tools.utils import current_agent, json_result, versioned_json_result


def test_json_result_pretty_serializes_payload_without_version():
    out = json_result(ok=True, action="status", count=2)
    parsed = json.loads(out)

    assert parsed == {"ok": True, "action": "status", "count": 2}
    assert "tool_version" not in parsed
    # Pretty-printed with indent=2.
    assert "\n  " in out


def test_json_result_default_str_serializes_non_json_types():
    moment = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)

    out = json_result(when=moment)

    # ``default=str`` keeps datetimes from raising; they round-trip as strings.
    assert json.loads(out)["when"] == str(moment)


def test_versioned_json_result_stamps_leading_tool_version():
    moment = datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc)

    out = versioned_json_result("2026-05-01.1", ok=True, action="write", when=moment)
    parsed = json.loads(out)

    assert parsed == {
        "tool_version": "2026-05-01.1",
        "ok": True,
        "action": "write",
        "when": str(moment),  # default=str also applies to the versioned variant
    }
    # ``tool_version`` is the first key (insertion order preserved by json).
    assert list(parsed.keys())[0] == "tool_version"


def test_current_agent_matches_get_current_agent():
    # current_agent is a thin lazy wrapper; it must return whatever the canonical
    # accessor returns (None when no agent is running in this test process).
    assert current_agent() is get_current_agent()
