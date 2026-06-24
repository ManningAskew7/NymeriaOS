"""Unit tests for the thread-config API helpers (``thread_config_helpers``)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from nymeria.api import thread_config_helpers
from nymeria.api.thread_config_helpers import validate_callable_name
from nymeria.core.callable_names import CALLABLE_NAME_RE


def test_validate_callable_name_uses_canonical_regex():
    # The API-side validator must reuse the canonical grammar from
    # core/callable_names rather than a forked copy, so the branch, import, and
    # API-validator copies cannot silently drift. (The agent-threads schema
    # enforces the same shape via its own Pydantic pattern, not this constant.)
    assert thread_config_helpers.CALLABLE_NAME_RE is CALLABLE_NAME_RE


@pytest.mark.parametrize(
    "name",
    ["Agent", "agent_1", "a-b_c", "A1", "x", "x" * 64],
)
def test_validate_callable_name_accepts_valid(name):
    # No exception expected for names inside the LLM tool-binding grammar.
    validate_callable_name(name)


@pytest.mark.parametrize(
    "name",
    ["", "x" * 65, "has space", "dotted.name", "punct!", "tab\tname", "slash/name"],
)
def test_validate_callable_name_rejects_invalid(name):
    with pytest.raises(HTTPException) as exc_info:
        validate_callable_name(name)
    assert exc_info.value.status_code == 400
