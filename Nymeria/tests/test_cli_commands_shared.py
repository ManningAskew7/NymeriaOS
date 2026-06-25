"""Tests for the shared CLI command leaf helpers (`commands/_shared.py`).

These cover the byte-identical `mapping_sequence`/`string_list` helpers that
were de-duplicated out of the individual command modules, plus a wiring check
that each command module now resolves its private alias to the shared copy
(so a future re-introduction of a local copy is caught).
"""

from __future__ import annotations

from types import ModuleType

import pytest

from nymeria.triggers.cli.commands import (
    _shared,
    account,
    mcp,
    skills,
    todos,
    tools,
    triggers,
)


class TestMappingSequence:
    def test_keeps_only_mappings_in_order(self) -> None:
        value = [{"a": 1}, "skip", 7, {"b": 2}]
        assert _shared.mapping_sequence(value) == [{"a": 1}, {"b": 2}]

    def test_accepts_tuples(self) -> None:
        assert _shared.mapping_sequence(({"a": 1},)) == [{"a": 1}]

    def test_rejects_str_and_bytes(self) -> None:
        assert _shared.mapping_sequence("abc") == []
        assert _shared.mapping_sequence(b"abc") == []

    def test_rejects_non_sequence(self) -> None:
        # A bare mapping is not a Sequence, so it yields nothing.
        assert _shared.mapping_sequence({"a": 1}) == []
        assert _shared.mapping_sequence(None) == []
        assert _shared.mapping_sequence(42) == []

    def test_empty_sequence(self) -> None:
        assert _shared.mapping_sequence([]) == []


class TestStringList:
    def test_stringifies_items(self) -> None:
        assert _shared.string_list(["x", "y"]) == ["x", "y"]
        assert _shared.string_list((1, 2)) == ["1", "2"]

    def test_filters_empty_string_renderings(self) -> None:
        # Only items whose str() is falsy (the empty string) are dropped.
        assert _shared.string_list(["x", "", "y"]) == ["x", "y"]

    def test_keeps_items_that_are_falsy_but_render_nonempty(self) -> None:
        # The filter is `if str(item)`, so 0 and False survive (as "0"/"False");
        # only an empty-string rendering is dropped.
        assert _shared.string_list([0, False, ""]) == ["0", "False"]

    def test_rejects_str_and_bytes(self) -> None:
        assert _shared.string_list("xy") == []
        assert _shared.string_list(b"xy") == []

    def test_rejects_non_sequence(self) -> None:
        assert _shared.string_list(None) == []
        assert _shared.string_list(123) == []


@pytest.mark.parametrize("module", [account, skills, mcp, triggers, todos, tools])
def test_modules_share_mapping_sequence(module: ModuleType) -> None:
    assert module._mapping_sequence is _shared.mapping_sequence


@pytest.mark.parametrize("module", [skills, mcp, tools])
def test_modules_share_string_list(module: ModuleType) -> None:
    assert module._string_list is _shared.string_list
