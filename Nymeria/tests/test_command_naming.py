"""The #131 naming-canon validator (core/command_naming.py).

Pins the machine-checkable half of docs/private/command-style-guide.md:
leaf vocabulary, the shrink-only grandfather ratchet, the rootless-family
rule, the scope-grammar rules, and the AGENT_BLOCKED registration check.
The built-in catalog passing is exercised by every CommandService()
construction; here it is explicit, plus each rule proved able to fail
against synthetic catalogs.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nymeria.core.command_naming import (
    AGENT_BLOCKED_UNREGISTERED,
    SANCTIONED_LEAF_TOKENS,
    SCOPE_OPTION_EXCEPTIONS,
    validate_command_naming,
)
from nymeria.core.command_service import AGENT_BLOCKED, CommandService

_NONE = frozenset()


def _cmd(path: tuple[str, ...], params: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(path=path, params=params)


def _param(kind: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, name=name)


def test_the_built_in_catalog_passes_the_canon() -> None:
    # validate_registry runs in __init__ and includes the naming check.
    CommandService()


def test_off_canon_leaf_raises() -> None:
    commands = {
        "zz": _cmd(("zz",)),
        "zz.frobnicate": _cmd(("zz", "frobnicate")),
    }
    with pytest.raises(ValueError, match="frobnicate"):
        validate_command_naming(
            commands, _NONE, grandfathered=_NONE, rootless_grandfathered=_NONE
        )


def test_canon_verb_and_sanctioned_word_leaves_pass() -> None:
    assert "switch" in SANCTIONED_LEAF_TOKENS
    commands = {
        "zz": _cmd(("zz",)),
        "zz.list": _cmd(("zz", "list")),
        "zz.switch": _cmd(("zz", "switch")),
    }
    validate_command_naming(
        commands, _NONE, grandfathered=_NONE, rootless_grandfathered=_NONE
    )


def test_stale_grandfather_entry_raises() -> None:
    # The shrink ratchet: a rename must retire its grandfather row.
    commands = {"zz": _cmd(("zz",))}
    with pytest.raises(ValueError, match="no longer registered"):
        validate_command_naming(
            commands,
            _NONE,
            grandfathered=frozenset({"zz.gone"}),
            rootless_grandfathered=_NONE,
        )


def test_family_without_an_overview_root_raises() -> None:
    commands = {"zz.list": _cmd(("zz", "list"))}
    with pytest.raises(ValueError, match="overview root"):
        validate_command_naming(
            commands, _NONE, grandfathered=_NONE, rootless_grandfathered=_NONE
        )


def test_global_flag_outside_the_grandfather_raises() -> None:
    commands = {
        "zz": _cmd(("zz",)),
        "zz.list": _cmd(("zz", "list"), params=(_param("flag", "global"),)),
    }
    with pytest.raises(ValueError, match="--global flags are retired"):
        validate_command_naming(
            commands, _NONE, grandfathered=_NONE, rootless_grandfathered=_NONE
        )


def test_stale_scope_flag_grandfather_entry_raises() -> None:
    """The scope-flag set shrinks on the same ratchet as the leaf set.

    Wave B emptied it, and nothing forced that before: the set only ever
    EXCUSED a flag, so a migrated command could leave its row behind and the
    next `--global` to appear under that id would pass unnoticed.
    """
    commands = {
        "zz": _cmd(("zz",)),
        "zz.list": _cmd(("zz", "list")),
    }
    with pytest.raises(ValueError, match="no longer declared"):
        validate_command_naming(
            commands,
            _NONE,
            grandfathered=_NONE,
            rootless_grandfathered=_NONE,
            scope_flag_grandfathered=frozenset({"zz.list"}),
        )

    # Still declaring the flag keeps the row honest.
    still_flagged = {
        "zz": _cmd(("zz",)),
        "zz.list": _cmd(("zz", "list"), params=(_param("flag", "global"),)),
    }
    validate_command_naming(
        still_flagged,
        _NONE,
        grandfathered=_NONE,
        rootless_grandfathered=_NONE,
        scope_flag_grandfathered=frozenset({"zz.list"}),
    )


def test_the_shipped_catalog_declares_no_global_flag() -> None:
    """Wave B's outcome, asserted directly rather than via an empty set."""
    from nymeria.core.command_naming import _SCOPE_FLAG_GRANDFATHERED

    assert _SCOPE_FLAG_GRANDFATHERED == frozenset()
    flagged = [
        command_id
        for command_id, cmd in CommandService()._commands.items()
        for param in cmd.params or ()
        if param.kind == "flag" and param.name == "global"
    ]
    assert flagged == []


def test_scope_option_outside_the_exceptions_raises() -> None:
    assert "hook.create" in SCOPE_OPTION_EXCEPTIONS
    commands = {
        "zz": _cmd(("zz",)),
        "zz.create": _cmd(("zz", "create"), params=(_param("option", "scope"),)),
    }
    with pytest.raises(ValueError, match="--scope options are retired"):
        validate_command_naming(
            commands, _NONE, grandfathered=_NONE, rootless_grandfathered=_NONE
        )


def test_unregistered_agent_blocked_entry_raises() -> None:
    commands = {"zz": _cmd(("zz",))}
    with pytest.raises(ValueError, match="AGENT_BLOCKED"):
        validate_command_naming(
            commands,
            {"phantom"},
            grandfathered=_NONE,
            rootless_grandfathered=_NONE,
        )


def test_recorded_unregistered_agent_blocked_entries_pass() -> None:
    # ask and start are bot-local chat entries, pre-blocked on purpose.
    assert AGENT_BLOCKED_UNREGISTERED == {"ask", "start"}
    assert AGENT_BLOCKED_UNREGISTERED <= AGENT_BLOCKED
    commands = {"zz": _cmd(("zz",))}
    validate_command_naming(
        commands,
        AGENT_BLOCKED_UNREGISTERED,
        grandfathered=_NONE,
        rootless_grandfathered=_NONE,
    )
