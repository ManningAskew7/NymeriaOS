"""Tests for MCP stdio process diagnostics."""

from __future__ import annotations

import sys

import pytest

from nymeria.core import mcp_servers
from nymeria.core.custom_tools import CustomToolLoader
from nymeria.core.mcp_manager import MCPServerManager, _validate_stdio_launch
from nymeria.core.mcp_manager import get_mcp_manager, shutdown_mcp_manager
from nymeria.tools.definitions.mcp_schema import MCPToolConfig


def test_stdio_launch_enforces_command_allowlist():
    with pytest.raises(RuntimeError, match="not allowed"):
        _validate_stdio_launch("/bin/sh", ["-c", "echo unsafe"])


def test_stdio_launch_blocks_interpreter_eval_flags():
    with pytest.raises(RuntimeError, match="unsafe argument"):
        _validate_stdio_launch("python3", ["-c", "print('unsafe')"])


def test_stdio_launch_accepts_versioned_python_interpreter():
    _validate_stdio_launch(sys.executable, ["server.py"])


def test_stdio_startup_failure_includes_recent_stderr(tmp_path):
    manager = MCPServerManager()
    missing_script = tmp_path / "missing_mcp_server.py"
    config = MCPToolConfig(
        server_command=sys.executable,
        server_args=[str(missing_script)],
        tool_name="__discovery__",
        startup_timeout_seconds=5,
    )

    try:
        with pytest.raises(RuntimeError) as exc_info:
            manager._get_or_create_connection(config)
    finally:
        manager.shutdown_all()

    message = str(exc_info.value)
    assert "MCP server initialization failed" in message
    assert "stderr:" in message
    assert str(missing_script) in message
    assert "No such file" in message or "can't open file" in message


class _FakeConn:
    def __init__(self):
        self._id = 0

    def next_request_id(self) -> int:
        self._id += 1
        return self._id


def test_list_tools_detailed_returns_raw_tool_records(monkeypatch):
    manager = MCPServerManager()
    config = MCPToolConfig(
        server_command=sys.executable,
        server_args=["server.py"],
        tool_name="__discovery__",
        startup_timeout_seconds=7,
    )
    captured = {}

    def fake_send_request(conn, request, timeout):
        captured["method"] = request["method"]
        captured["timeout"] = timeout
        return {
            "result": {
                "tools": [
                    {"name": "alpha", "description": "A", "inputSchema": {"type": "object"}},
                    {"name": "beta"},
                ]
            }
        }

    monkeypatch.setattr(manager, "_get_or_create_connection", lambda cfg: _FakeConn())
    monkeypatch.setattr(manager, "_send_request", fake_send_request)

    tools = manager.list_tools_detailed(config)

    assert captured["method"] == "tools/list"
    assert captured["timeout"] == 7  # honors config.startup_timeout_seconds
    assert [t.get("name") for t in tools] == ["alpha", "beta"]
    assert tools[0]["inputSchema"] == {"type": "object"}


def test_list_tools_detailed_returns_empty_without_result(monkeypatch):
    manager = MCPServerManager()
    config = MCPToolConfig(
        server_command=sys.executable,
        server_args=["server.py"],
        tool_name="__discovery__",
    )
    monkeypatch.setattr(manager, "_get_or_create_connection", lambda cfg: _FakeConn())
    monkeypatch.setattr(
        manager, "_send_request", lambda conn, request, timeout: {"error": {"message": "boom"}}
    )

    assert manager.list_tools_detailed(config) == []


def test_shared_mcp_manager_used_by_managed_and_custom_tools(tmp_path):
    shutdown_mcp_manager()
    try:
        shared = get_mcp_manager()
        loader = CustomToolLoader(tmp_path / "custom_tools")

        assert loader.mcp_manager is shared
        assert mcp_servers.get_mcp_manager() is shared
    finally:
        shutdown_mcp_manager()


def test_shutdown_mcp_manager_resets_singleton():
    first = get_mcp_manager()
    shutdown_mcp_manager()

    try:
        second = get_mcp_manager()
        assert second is not first
    finally:
        shutdown_mcp_manager()
