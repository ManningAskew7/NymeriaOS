"""Tests for the shared tool helpers consolidated in ``nymeria.tools.utils``."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from nymeria.core.agent import get_current_agent
from nymeria.tools.utils import (
    _escape_md_table_cell,
    caller_role,
    current_agent,
    is_admin,
    json_result,
    rows_to_markdown_table,
    versioned_json_result,
)


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


# --- rows_to_markdown_table (slice 16 F8) ----------------------------------


def test_rows_to_markdown_table_basic_shape():
    out = rows_to_markdown_table([["Name", "Role"], ["Alice", "Dev"]])

    assert out == "| Name | Role |\n| --- | --- |\n| Alice | Dev |"
    # No trailing newline; the separator width follows the header.
    assert not out.endswith("\n")


def test_rows_to_markdown_table_separator_width_follows_header():
    out = rows_to_markdown_table([["a", "b", "c"], ["1", "2", "3"]])

    assert out.splitlines()[1] == "| --- | --- | --- |"


def test_rows_to_markdown_table_escapes_pipe_in_cells():
    # A literal pipe would otherwise end the cell and corrupt the row.
    out = rows_to_markdown_table([["h"], ["a|b"]])

    assert out == "| h |\n| --- |\n| a\\|b |"


def test_rows_to_markdown_table_collapses_newlines_in_cells():
    # CR/LF would split a cell across table rows; they collapse to a space.
    out = rows_to_markdown_table([["h"], ["line1\nline2"], ["x\r\ny"]])

    assert "| line1 line2 |" in out
    assert "| x y |" in out
    # The body stays three logical rows (header + separator + 2 data rows).
    assert len(out.splitlines()) == 4


def test_rows_to_markdown_table_empty_input_returns_empty_string():
    assert rows_to_markdown_table([]) == ""


def test_rows_to_markdown_table_header_only():
    assert rows_to_markdown_table([["only", "header"]]) == (
        "| only | header |\n| --- | --- |"
    )


def test_rows_to_markdown_table_preserves_ragged_rows_unpadded():
    # The helper does not pad; each row renders at its own width so callers keep
    # their own row geometry (google_docs unpadded, outlook pre-padded).
    out = rows_to_markdown_table([["a", "b"], ["1"], ["2", "3", "4"]])

    assert out.splitlines() == [
        "| a | b |",
        "| --- | --- |",
        "| 1 |",
        "| 2 | 3 | 4 |",
    ]


def test_escape_md_table_cell_coerces_non_str():
    # Defensive str() so a shared/public helper never blows up on a stray int.
    assert _escape_md_table_cell(42) == "42"
    assert _escape_md_table_cell("plain") == "plain"


# --- caller_role / is_admin (slice 12 F11) ---------------------------------


def _fake_agent_with_role(role: str):
    """A minimal agent whose accounts_repo resolves "u1" to ``role``."""
    user = SimpleNamespace(id="u1", role=role, disabled=False)
    repo = SimpleNamespace(get_user_by_id=lambda uid: user if uid == "u1" else None)
    return SimpleNamespace(accounts_repo=repo)


def _raising_agent():
    def _boom(_uid):
        raise RuntimeError("repo down")

    return SimpleNamespace(accounts_repo=SimpleNamespace(get_user_by_id=_boom))


def test_caller_role_resolves_via_lazy_agent_seam():
    # The lazy default path resolves through current_agent(), whose function-local
    # import binds nymeria.core.agent.get_current_agent at call time. Patching that
    # canonical seam must still work; this is the single most likely silent
    # breakage if the import is ever hoisted to module level.
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("admin")):
        assert caller_role("u1") == "admin"
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("user")):
        assert caller_role("u1") == "user"


def test_caller_role_fails_closed_to_user():
    # No agent, unknown account, falsy user_id, and repo errors all deny.
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        assert caller_role("u1") == "user"
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("admin")):
        assert caller_role("ghost") == "user"
        assert caller_role("") == "user"
        assert caller_role(None) == "user"
    with patch("nymeria.core.agent.get_current_agent", return_value=_raising_agent()):
        assert caller_role("u1") == "user"


def test_caller_role_preserves_role_string_byte_for_byte():
    # Locks equivalence with the retired _get_user_role: the resolved role string
    # is returned verbatim (only a missing user collapses to "user"). An empty
    # role string is preserved as-is (it is simply not "admin"), matching the old
    # `user.role if user else "user"` shape rather than coercing falsy to "user".
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("developer")):
        assert caller_role("u1") == "developer"
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("")):
        assert caller_role("u1") == ""
        assert is_admin("u1") is False


def test_caller_role_explicit_agent_skips_lazy_lookup():
    # When an agent is passed in (e.g. a tool that already holds it), current_agent
    # is never consulted, avoiding a redundant contextvar lookup and a TOCTOU.
    def _should_not_be_called():
        raise AssertionError("current_agent() must not be called when agent= is provided")

    with patch("nymeria.core.agent.get_current_agent", side_effect=_should_not_be_called):
        assert caller_role("u1", agent=_fake_agent_with_role("admin")) == "admin"
        assert caller_role("ghost", agent=_fake_agent_with_role("admin")) == "user"


def test_is_admin_true_only_for_admin_role():
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("admin")):
        assert is_admin("u1") is True
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("user")):
        assert is_admin("u1") is False
    # Fail-closed: no agent, unknown account, and repo errors all deny.
    with patch("nymeria.core.agent.get_current_agent", return_value=None):
        assert is_admin("u1") is False
    with patch("nymeria.core.agent.get_current_agent", return_value=_fake_agent_with_role("admin")):
        assert is_admin("ghost") is False
    with patch("nymeria.core.agent.get_current_agent", return_value=_raising_agent()):
        assert is_admin("u1") is False


def test_is_admin_honors_explicit_agent():
    assert is_admin("u1", agent=_fake_agent_with_role("admin")) is True
    assert is_admin("u1", agent=_fake_agent_with_role("user")) is False
