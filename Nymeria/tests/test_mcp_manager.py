"""Tests for MCP stdio process diagnostics."""

from __future__ import annotations

import sys

import pytest

from nymeria.core import credential_vault, mcp_manager, mcp_servers
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


# ---- env / header secret interpolation at spawn time (slice 05 F4) ----


class _RecordingVault:
    """Resolves the known ``${credential:cred1.token}`` ref and records targets."""

    def __init__(self):
        self.targets: set[str] = set()

    def resolve_references(
        self,
        value,
        *,
        actor_user_id=None,
        target_type=None,
        target_id=None,
        used_credentials=None,
        redact_values=None,
    ):
        if target_id is not None:
            self.targets.add(target_id)
        if used_credentials is not None:
            used_credentials.add("cred1")
        return value.replace("${credential:cred1.token}", "SEKRET")


def test_start_server_resolves_env_and_credentials_in_env(monkeypatch):
    monkeypatch.setenv("MCP_TOKEN", "tok-123")
    monkeypatch.delenv("MCP_MISSING", raising=False)
    vault = _RecordingVault()
    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: vault)

    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        # Short-circuit before the stderr thread / init handshake. The env has
        # already been fully built and the credential set already accumulated.
        raise RuntimeError("stop-after-env")

    monkeypatch.setattr(mcp_manager.subprocess, "Popen", fake_popen)

    config = MCPToolConfig(
        server_command=sys.executable,
        server_args=["server.py"],
        tool_name="__discovery__",
        server_id="srv-stdio",
        env_vars={
            "WHOLE": "${env:MCP_TOKEN}",
            "EMBEDDED": "Bearer ${env:MCP_TOKEN}",  # NEW: embedded ref resolves
            "MISSING": "x=${env:MCP_MISSING}",  # missing -> empty (preserved)
            "CRED": "${credential:cred1.token}",
            "PLAIN": "literal",
        },
    )

    manager = MCPServerManager()
    with pytest.raises(RuntimeError, match="Failed to start MCP server"):
        manager._start_server(config)

    env = captured["env"]
    assert env["WHOLE"] == "tok-123"
    assert env["EMBEDDED"] == "Bearer tok-123"
    assert env["MISSING"] == "x="
    assert env["CRED"] == "SEKRET"
    assert env["PLAIN"] == "literal"
    # The credential set accumulates once across the whole env loop.
    assert vault.targets == {"srv-stdio"}


def test_start_http_connection_resolves_env_and_credentials_in_headers(monkeypatch):
    import httpx

    monkeypatch.setenv("MCP_TOKEN", "tok-123")
    vault = _RecordingVault()
    monkeypatch.setattr(credential_vault, "get_credential_vault_repo", lambda: vault)
    monkeypatch.setattr(
        mcp_manager, "validate_http_egress_url", lambda url, label=None: None
    )

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["headers"] = kwargs["headers"]

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    manager = MCPServerManager()
    monkeypatch.setattr(manager, "_initialize_server", lambda conn, init_timeout: None)

    config = MCPToolConfig(
        url="https://mcp.example.com/rpc",
        transport="http",
        tool_name="__discovery__",
        server_id="srv-http",
        headers={
            "Authorization": "Bearer ${env:MCP_TOKEN}",  # NEW: embedded resolves
            "X-Cred": "${credential:cred1.token}",
            "X-Plain": "static",
        },
    )

    manager._start_http_connection(config)

    headers = captured["headers"]
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["X-Cred"] == "SEKRET"
    assert headers["X-Plain"] == "static"
    # Default headers preserved.
    assert headers["Content-Type"] == "application/json"
    # Credentials resolve against one accumulated set keyed to the http target
    # (the F4 fix unified the prior per-header re-init/log).
    assert vault.targets == {"srv-http"}
