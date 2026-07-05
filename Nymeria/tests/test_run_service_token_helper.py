"""Regression tests for run.py startup helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nymeria.core.service_bootstrap import SLIM_SERVICE_TOKEN_FILENAME
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


def test_require_service_token_falls_back_to_minted_file(tmp_path):
    """When the env token is empty, the api-minted file on the shared volume
    supplies it (the full Docker stack worker/mcp path)."""
    (tmp_path / SLIM_SERVICE_TOKEN_FILENAME).write_text("nym_from_file\n", encoding="utf-8")
    settings = SimpleNamespace(nymeria_service_token="", data_dir=tmp_path)

    assert _require_service_token(settings, "the test role") == "nym_from_file"


def test_require_service_token_handles_missing_data_dir(capsys):
    """A settings stub without ``data_dir`` must still exit cleanly (getattr
    guard), not raise AttributeError, when no token is available."""
    settings = SimpleNamespace(nymeria_service_token="")

    with pytest.raises(SystemExit) as exc_info:
        _require_service_token(settings, "the test role")

    assert exc_info.value.code == 1


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
        ("discord-bot", None, "the Discord bot"),
        ("telegram-bot", None, "the Telegram bot"),
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
        # The worker is intentionally NOT early-gated: it depends only on
        # postgres/redis (not api-healthy) and can start before the api
        # self-mints the service token onto the shared volume, so it resolves
        # the token after its own API health wait instead.
        ("worker", None),
        # The service-manager actions never call the API as a privileged
        # client; only `service run` (the foreground gateway) needs the token.
        ("service", "install"),
        ("service", "uninstall"),
        ("service", "status"),
        ("service", "restart"),
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
