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


def test_service_construction_actually_runs_the_naming_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wiring itself, not the rules: if validate_registry ever stops
    calling validate_command_naming, every rule test above keeps passing
    against a validator nothing runs (#131 review F4)."""
    from nymeria.core import command_service as command_service_module

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise ValueError("naming check wired")

    monkeypatch.setattr(
        command_service_module, "validate_command_naming", _boom
    )
    with pytest.raises(ValueError, match="naming check wired"):
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


def test_cliproxy_target_spellings_never_shadow_a_registry_provider() -> None:
    """`/provider <target>` may only claim names the registry does not own.

    Three of the six CLIProxy targets ARE registry ids or aliases
    (claude -> anthropic, kimi -> moonshotai, grok -> xai), whose
    ungated, read-only provider card has to keep winning; the card
    already offers CLIProxy as a tab for its matching target. A new
    catalog entry that collided would otherwise silently convert an
    ordinary browse into an admin-gated OAuth step.
    """
    from nymeria.cliproxy.catalog import list_cliproxy_providers
    from nymeria.config.llm_providers import get_llm_provider_spec

    service = CommandService()
    # Alias paths are stored normalized, so a catalog id folds its
    # dashes to underscores on the way in (`gemini-cli` -> `gemini_cli`).
    catalog = {spec.id.replace("-", "_"): spec.id for spec in list_cliproxy_providers()}
    dispatched = {
        catalog[path[1]]
        for path, target in service._aliases.items()
        if len(path) == 2
        and path[0] == "provider"
        and target == "provider.cliproxy"
        and path[1] in catalog
    }
    # The three free ids dispatch; the colliding three deliberately do not.
    assert dispatched == {"codex", "gemini-cli", "antigravity"}

    for spelling in dispatched:
        assert get_llm_provider_spec(spelling) is None, (
            f"/provider {spelling} would shadow registry provider "
            f"{spelling!r}; drop it from injected_aliases"
        )

    withheld = set(catalog.values()) - dispatched
    assert withheld == {"claude", "kimi", "grok"}
    for spelling in withheld:
        assert get_llm_provider_spec(spelling) is not None, (
            f"CLIProxy target {spelling!r} no longer collides with the "
            "registry; it can dispatch straight through now"
        )


def _leading_single_token_aliases() -> dict[str, str]:
    """command id -> its FIRST single-token alias, in declaration order.

    This mirrors ``_telegram_command_name``'s preference rule: the leading
    single-token alias IS the Telegram menu name for that command, so alias
    tuple ORDER in the registry is a live external contract.
    """
    result: dict[str, str] = {}
    for command_id, cmd in CommandService()._commands.items():
        for alias in cmd.aliases:
            if len(alias) == 1:
                result[command_id] = alias[0]
                break
    return result


def test_leading_single_token_aliases_snapshot() -> None:
    """The Telegram menu-name contract, pinned (#131 architect review).

    Wave A preserved every menu name by keeping alias order stable and
    guarded it with registry comments; this snapshot gives the contract
    teeth. A diff here means a Telegram menu entry is about to rename:
    either restore the order or update BOTH this snapshot and the
    telegram-bot.md tables deliberately.
    """
    assert _leading_single_token_aliases() == {
        "account": "acct",
        "account.platforms": "account_platforms",
        "alias": "aliases",
        "alias.create": "alias_create",
        "alias.delete": "alias_delete",
        "alias.list": "alias_list",
        "browser.login": "browser_login",
        "account.show": "account_current",
        "account.tokens": "account_tokens",
        "account.tokens.issue": "account_tokens_issue",
        "account.tokens.revoke": "account_tokens_revoke",
        "activity.list": "activity_list",
        "activity.notifications": "activity_notifications",
        "artifacts.list": "artifacts_recent",
        "doctor.auth": "doctor_auth",
        "doctor.model": "doctor_model",
        "env.get": "env_get",
        "env.set": "env_set",
        "env.show": "env_show",
        "help": "h",
        "hook": "hooks",
        "hook.approvals": "hook_approvals",
        "hook.approve": "hook_approve",
        "hook.create": "hook_create",
        "hook.delete": "hook_delete",
        "hook.deny": "hook_deny",
        "hook.disable": "hook_disable",
        "hook.edit": "hook_edit",
        "hook.enable": "hook_enable",
        "hook.history": "hook_log",
        "hook.install": "hook_install",
        "hook.list": "hook_list",
        "hook.show": "hook_show",
        "hook.templates": "hook_templates",
        "hook.test": "hook_test",
        "mcp.delete": "mcp_remove",
        "mcp.discover": "mcp_discover",
        "mcp.list": "mcp_list",
        "mcp.logs": "mcp_logs",
        "mcp.retry": "mcp_retry",
        "mcp.status": "mcp_status",
        "mcp.test": "mcp_test",
        "memory.delete": "memory_forget",
        "memory.limit": "memory_limit",
        "memory.list": "memory_list",
        "memory.save": "memory_save",
        "memory.search": "memory_search",
        "model.list": "models",
        "notepad.clear": "notepad_clear",
        "notepad.read": "notepad_read",
        "notepad.write": "notepad_write",
        "orchestrate.clear": "orchestrate_clear",
        "orchestrate.status": "orchestrate_status",
        "provider.cliproxy": "provider_cliproxy",
        "provider.list": "provider_list",
        "provider.reasoning_passback": "provider_reasoning_passback",
        "provider.set": "provider_set",
        "provider.setup": "provider_setup",
        "provider.switch": "provider_switch",
        "provider.test": "provider_test",
        # Deliberate 2026-08-10 addition, not a Wave A preservation: the flat
        # spelling was never registered, so /restart_api dead-ended in a
        # did-you-mean pointing at bare /restart.
        "restart.api": "restart_api",
        "settings": "settings_show",
        "settings.get": "settings_get",
        "settings.set": "settings_set",
        "skills.disable": "skills_disable",
        "skills.enable": "skills_enable",
        "skills.install": "skills_install",
        "skills.list": "skills_list",
        "skills.search": "skills_search",
        "skills.show": "skills_show",
        "team": "teams",
        "team.list": "team_list",
        "team.show": "team_show",
        "think": "reasoning",
        "thread": "threads",
        "thread.branch": "thread_branch",
        "thread.compact": "thread_compact",
        "thread.config": "thread_config",
        "thread.create": "thread_new",
        "thread.delete": "thread_delete",
        "thread.list": "thread_list",
        "thread.pin": "thread_pin",
        "thread.rename": "thread_rename",
        "thread.show": "thread_info",
        "thread.switch": "thread_switch",
        "todos": "todo",
        "todos.add": "todos_add",
        "todos.complete": "todos_complete",
        "todos.delete": "todos_delete",
        "todos.edit": "todos_edit",
        "todos.list": "todos_list",
        "todos.repeat": "todos_repeat",
        "todos.schedule": "todos_schedule",
        "tools.disable": "tools_disable",
        "tools.enable": "tools_enable",
        "tools.list": "tools_list",
        "triggers.delete": "triggers_delete",
        "triggers.disable": "triggers_disable",
        "triggers.enable": "triggers_enable",
        "triggers.history": "triggers_history",
        "triggers.list": "triggers_list",
        "usage": "tokens",
        "usage.session": "usage_session",
    }


def test_sanctioned_tokens_appear_in_the_style_guide() -> None:
    """SANCTIONED_LEAF_TOKENS and the style guide's human copy are
    hand-synced by rule; this closes the gap (#131 architect review).
    A hyphen-rendered spelling in the guide vouches for its underscore
    token."""
    from pathlib import Path

    guide = (
        Path(__file__).resolve().parents[1]
        / "docs"
        / "private"
        / "command-style-guide.md"
    ).read_text(encoding="utf-8")
    missing = [
        token
        for token in SANCTIONED_LEAF_TOKENS
        if f"`{token}`" not in guide
        and f"`{token.replace('_', '-')}`" not in guide
    ]
    assert not missing, (
        "Sanctioned tokens absent from docs/private/command-style-guide.md "
        f"(add them to the Recorded exceptions section): {missing}"
    )
