"""Tests for MCP stdio process diagnostics."""

from __future__ import annotations

import sys

import pytest

from nymeria.core import mcp_servers
from nymeria.core.custom_tools import CustomToolLoader
from nymeria.core.mcp_manager import MCPServerManager
from nymeria.core.mcp_manager import get_mcp_manager, shutdown_mcp_manager
from nymeria.tools.definitions.schema import MCPToolConfig


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
