"""Regression tests for run.py startup helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from run import (
    _require_launch_mode_service_token,
    _require_service_token,
    _service_token_requirement,
)


def test_require_service_token_returns_configured_token():
    settings = SimpleNamespace(nymeria_service_token="nym_test")

    assert _require_service_token(settings, "the test role") == "nym_test"


def test_require_service_token_strips_whitespace():
    settings = SimpleNamespace(nymeria_service_token="  nym_test  ")

    assert _require_service_token(settings, "the test role") == "nym_test"


def test_require_service_token_exits_with_provisioning_guidance(capsys):
    settings = SimpleNamespace(nymeria_service_token="")

    with pytest.raises(SystemExit) as exc_info:
        _require_service_token(settings, "the test role")

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "NYMERIA_SERVICE_TOKEN is required for the test role" in output
    assert "users add bot-service@localhost" in output
    assert "--role admin --id bot-service" in output


@pytest.mark.parametrize(
    ("command", "action", "role"),
    [
        ("worker", None, "the worker (ticker)"),
        ("discord-bot", None, "the Discord bot"),
        ("telegram-bot", None, "the Telegram bot"),
        ("watchdog", None, "the watchdog worker"),
        ("mcp", None, "the MCP thin client"),
        ("service", None, "the foreground gateway service"),
    ],
)
def test_service_token_requirement_covers_internal_launch_modes(command, action, role):
    args = SimpleNamespace(command=command, action=action)

    assert _service_token_requirement(args) == role


@pytest.mark.parametrize(
    ("command", "action"),
    [
        ("api", None),
        ("cli", None),
        ("users", None),
    ],
)
def test_service_token_requirement_skips_modes_without_internal_api_calls(command, action):
    args = SimpleNamespace(command=command, action=action)

    assert _service_token_requirement(args) is None


def test_launch_mode_service_token_validation_is_noop_for_api_mode():
    args = SimpleNamespace(command="api", action=None)
    settings = SimpleNamespace(nymeria_service_token="")

    _require_launch_mode_service_token(args, settings)


def test_launch_mode_service_token_validation_exits_for_mcp_on_stderr(capsys):
    args = SimpleNamespace(command="mcp", action=None)
    settings = SimpleNamespace(nymeria_service_token="")

    with pytest.raises(SystemExit) as exc_info:
        _require_launch_mode_service_token(args, settings)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "NYMERIA_SERVICE_TOKEN is required for the MCP thin client" in captured.err
