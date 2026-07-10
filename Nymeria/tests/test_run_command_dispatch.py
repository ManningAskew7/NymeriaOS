"""Regression tests for run.py command dispatch and bot-runner helpers.

Covers the COMMANDS registry (single source of truth for dispatch + config
validation) and the shared bot-runner helpers extracted from the eight
near-identical bot launchers.
"""

from __future__ import annotations

import argparse
import signal
import sys
from typing import Any, Callable

import run


# ---------------------------------------------------------------------------
# COMMANDS registry (F2)
# ---------------------------------------------------------------------------


def _subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def test_every_subparser_resolves_to_a_command():
    """No registered subcommand may exist without a COMMANDS entry, and vice
    versa, so dispatch and the parser can never drift apart."""
    names = _subcommand_names(run.build_parser())
    assert names, "build_parser exposed no subcommands"
    assert names == set(run.COMMANDS), names ^ set(run.COMMANDS)


def test_full_validation_set_matches_server_and_bot_commands():
    """The validate_config gate set is derived from the table; lock its members
    so a future edit cannot silently add or drop config validation."""
    assert run._FULL_VALIDATION_COMMANDS == frozenset(
        {
            "api",
            "slim",
            "mcp",
            "worker",
            "discord-bot",
            "telegram-bot",
            "slack-bot",
        }
    )
    # The conditionally-validated commands are deliberately NOT in the set.
    for name in ("cli", "service", "users", "snapshot"):
        assert name not in run._FULL_VALIDATION_COMMANDS


def test_exit_returning_commands():
    """Only the commands that return an exit code are marked exits=True."""
    exits = {name for name, command in run.COMMANDS.items() if command.exits}
    assert exits == {"init", "doctor", "reembed", "users", "snapshot"}


def test_dispatch_resolves_runner_through_module_namespace(monkeypatch):
    """main() must resolve the runner by name at call time, not call the
    reference captured in COMMANDS at import time, so monkeypatching a runner
    (as many tests and the old if/elif seam rely on) still intercepts dispatch.
    Regression guard: capturing the function object in COMMANDS silently
    bypasses the patch and runs the real runner."""
    called: dict[str, object] = {}
    monkeypatch.setattr(run, "run_api", lambda args: called.setdefault("api", args))
    monkeypatch.setattr(run, "validate_config", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["run.py", "api"])

    run.main()

    assert "api" in called, "dispatch did not call the monkeypatched runner"


def test_run_users_delegates_to_users_cli(monkeypatch):
    captured = {}

    def fake_dispatch(args):
        captured["args"] = args
        return 7

    import nymeria.cli.users as users_cli

    monkeypatch.setattr(users_cli, "dispatch", fake_dispatch)
    args = argparse.Namespace(action="list")
    assert run.run_users(args) == 7
    assert captured["args"] is args


# ---------------------------------------------------------------------------
# Bot-runner helpers (F1)
# ---------------------------------------------------------------------------


def test_resolve_api_url_prefers_explicit():
    assert (
        run._resolve_api_url(argparse.Namespace(api_url="http://localhost:9000"))
        == "http://localhost:9000"
    )


def test_resolve_api_url_defaults_when_missing_or_none():
    assert run._resolve_api_url(argparse.Namespace()) == "http://nymeria-api:8000"
    assert run._resolve_api_url(argparse.Namespace(api_url=None)) == "http://nymeria-api:8000"


def test_add_api_url_arg_default_help_and_parsing():
    parser = argparse.ArgumentParser()
    run._add_api_url_arg(parser)
    assert parser.parse_args([]).api_url is None
    assert parser.parse_args(["--api-url", "http://x:1"]).api_url == "http://x:1"


def test_install_exit_handlers_runs_on_stop_without_hard_exit(monkeypatch, capsys):
    registered: dict[int, Callable[[int, Any], None]] = {}
    monkeypatch.setattr(run.signal, "signal", lambda sig, handler: registered.__setitem__(sig, handler))

    calls: list[str] = []
    run._install_exit_handlers("byebye", on_stop=lambda: calls.append("stopped"), hard_exit=False)

    assert signal.SIGINT in registered and signal.SIGTERM in registered
    # Invoking the handler must print the message and run on_stop, but not exit.
    registered[signal.SIGINT](signal.SIGINT, None)
    assert "byebye" in capsys.readouterr().out
    assert calls == ["stopped"]


def test_install_exit_handlers_hard_exit(monkeypatch):
    registered: dict[int, Callable[[int, Any], None]] = {}
    monkeypatch.setattr(run.signal, "signal", lambda sig, handler: registered.__setitem__(sig, handler))
    exits: list[int] = []
    monkeypatch.setattr(run.os, "_exit", lambda code: exits.append(code))

    run._install_exit_handlers("bye", hard_exit=True)
    registered[signal.SIGTERM](signal.SIGTERM, None)
    assert exits == [0]
