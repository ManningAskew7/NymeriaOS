"""Tests for MCP stdio process diagnostics."""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from types import SimpleNamespace
from typing import cast

import pytest

from nymeria.core import credential_vault, mcp_manager, mcp_servers
from nymeria.core.custom_tools import CustomToolLoader
from nymeria.core.mcp_manager import MCPConnection, MCPServerManager, _validate_stdio_launch
from nymeria.core.mcp_manager import (
    _get_mcp_call_executor,
    get_mcp_manager,
    shutdown_mcp_manager,
)
from nymeria.tools.definitions.mcp_schema import MCPServerDefinition, MCPToolConfig


@pytest.fixture
def mcp_manager_instance():
    """A fresh manager whose background cleanup thread is torn down after use."""
    manager = MCPServerManager()
    try:
        yield manager
    finally:
        manager.shutdown_all()


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


# --------------------------------------------------------------------------
# P5 (T6) execution lifecycle
# --------------------------------------------------------------------------


class _FakeIdleConn:
    """Minimal connection stand-in for the idle-sweep logic."""

    def __init__(self, *, alive=True, idle=True, locked=False, inflight=False,
                 transport="stdio"):
        self._alive = alive
        self._idle = idle
        self._inflight = inflight
        self.server_id = "fake"
        self._io_lock = threading.Lock()
        if locked:
            self._io_lock.acquire()
        self.config = SimpleNamespace(idle_timeout_seconds=300, transport=transport)

    def is_alive(self):
        return self._alive

    def is_idle(self, timeout_seconds):
        return self._idle

    def has_active_request(self):
        return self._inflight


def test_idle_sweep_skips_connection_with_call_in_flight(mcp_manager_instance, monkeypatch):
    manager = mcp_manager_instance
    shut_down = []
    monkeypatch.setattr(manager, "_shutdown_connection", lambda conn: shut_down.append(conn))

    # Idle by the clock, but a call holds the io lock: must NOT be reaped.
    conn = _FakeIdleConn(alive=True, idle=True, locked=True)
    manager._connections["k"] = cast(MCPConnection, conn)
    manager._cleanup_idle_connections()
    assert "k" in manager._connections
    assert shut_down == []

    # Release the lock; now the same idle connection is reaped.
    conn._io_lock.release()
    manager._cleanup_idle_connections()
    assert "k" not in manager._connections
    assert shut_down == [conn]


def test_idle_sweep_skips_http_connection_with_inflight_request(
    mcp_manager_instance, monkeypatch
):
    """An http call in flight never holds the io lock, so the in-flight counter
    is what keeps the idle reaper from closing the client under the active
    request (a call can legitimately outlast the idle window)."""
    manager = mcp_manager_instance
    shut_down = []
    monkeypatch.setattr(
        manager, "_shutdown_connection", lambda conn: shut_down.append(conn)
    )

    conn = MCPConnection(
        config=MCPToolConfig(transport="http", url="http://x", tool_name="t")
    )
    conn.http_client = SimpleNamespace(close=lambda: None)  # is_alive() -> True
    conn.last_used = time.time() - 10_000  # idle by the clock
    manager._connections["k"] = conn

    # A call is in flight: must NOT be reaped even though it looks idle.
    conn.begin_request()
    manager._cleanup_idle_connections()
    assert "k" in manager._connections
    assert shut_down == []

    # Call finished (end_request restarts the idle window from completion, so
    # re-age the connection to simulate the idle timeout then elapsing).
    conn.end_request()
    conn.last_used = time.time() - 10_000
    manager._cleanup_idle_connections()
    assert "k" not in manager._connections
    assert shut_down == [conn]


def test_send_request_reports_inflight_during_http_call(
    mcp_manager_instance, monkeypatch
):
    """_send_request marks the connection in flight for the whole dispatch, so a
    concurrent idle sweep sees has_active_request() and skips it."""
    manager = mcp_manager_instance
    conn = MCPConnection(
        config=MCPToolConfig(transport="http", url="http://x", tool_name="t")
    )
    seen = {}

    def _fake_http(c, r, t):
        seen["inflight"] = c.has_active_request()
        return {"result": {}}

    monkeypatch.setattr(manager, "_http_send_request", _fake_http)
    manager._send_request(conn, {"method": "tools/call"}, timeout=5)

    assert seen["inflight"] is True
    # The counter is released once the call completes.
    assert conn.has_active_request() is False


def test_begin_end_request_counter(mcp_manager_instance):
    """The in-flight counter supports concurrent http calls and never underflows."""
    conn = MCPConnection(
        config=MCPToolConfig(transport="http", url="http://x", tool_name="t")
    )
    assert conn.has_active_request() is False
    conn.begin_request()
    conn.begin_request()  # two concurrent calls to the same server
    assert conn.has_active_request() is True
    conn.end_request()
    assert conn.has_active_request() is True
    conn.end_request()
    assert conn.has_active_request() is False
    conn.end_request()  # extra end must not go negative
    assert conn.has_active_request() is False


def test_shutdown_server_skips_http_connection_with_inflight_request(
    mcp_manager_instance,
):
    """Credential-rotation shutdown must also skip an http connection with a
    call in flight: return skipped_in_use and leave the live client open."""
    manager = mcp_manager_instance
    closed = []
    config = MCPToolConfig(
        transport="http", url="http://x", tool_name="t", server_id="srv-http"
    )
    conn = MCPConnection(config=config)
    conn.http_client = SimpleNamespace(close=lambda: closed.append(True))
    key = manager._get_config_key(config)
    manager._connections[key] = conn

    conn.begin_request()
    assert manager.shutdown_server("srv-http") == "skipped_in_use"
    assert key in manager._connections
    assert closed == []

    conn.end_request()
    assert manager.shutdown_server("srv-http") == "shutdown"
    assert key not in manager._connections


def test_send_request_touches_at_both_ends(mcp_manager_instance, monkeypatch):
    manager = mcp_manager_instance
    conn = MCPConnection(
        config=MCPToolConfig(transport="http", url="http://x", tool_name="t")
    )
    conn.last_used = time.time() - 1000
    monkeypatch.setattr(manager, "_http_send_request", lambda c, r, t: {"result": {}})

    manager._send_request(conn, {"method": "tools/call"}, timeout=5)

    # last_used advanced to (about) now, so a subsequent sweep restarts the
    # idle window from call completion rather than acquisition.
    assert time.time() - conn.last_used < 5


def test_effective_call_timeout_uses_config_value(mcp_manager_instance, monkeypatch):
    import nymeria.config as ncfg

    monkeypatch.setattr(ncfg, "get_settings", lambda: SimpleNamespace(tool_timeout=300))
    manager = mcp_manager_instance
    config = MCPToolConfig(
        server_command=sys.executable, server_args=["s.py"], tool_name="t",
        call_timeout_seconds=45,
    )
    assert manager._effective_call_timeout(config) == 45


def test_effective_call_timeout_clamps_to_tool_timeout(mcp_manager_instance, monkeypatch):
    import nymeria.config as ncfg

    monkeypatch.setattr(ncfg, "get_settings", lambda: SimpleNamespace(tool_timeout=90))
    manager = mcp_manager_instance
    config = MCPToolConfig(
        server_command=sys.executable, server_args=["s.py"], tool_name="t",
        call_timeout_seconds=600,
    )
    # A server can't out-wait the turn-level tool_timeout ceiling.
    assert manager._effective_call_timeout(config) == 90


def test_call_tool_sync_passes_effective_timeout(mcp_manager_instance, monkeypatch):
    import nymeria.config as ncfg

    monkeypatch.setattr(ncfg, "get_settings", lambda: SimpleNamespace(tool_timeout=300))
    manager = mcp_manager_instance
    captured = {}

    conn = SimpleNamespace(available_tools=["t"], next_request_id=lambda: 1)
    monkeypatch.setattr(manager, "_get_or_create_connection", lambda cfg: conn)

    def fake_send(c, request, timeout):
        captured["timeout"] = timeout
        return {"result": {"content": [{"type": "text", "text": "ok"}]}}

    monkeypatch.setattr(manager, "_send_request", fake_send)
    config = MCPToolConfig(
        server_command=sys.executable, server_args=["s.py"], tool_name="t",
        call_timeout_seconds=50,
    )
    assert manager.call_tool_sync(config, {}) == "ok"
    assert captured["timeout"] == 50


def test_abort_in_flight_tears_down_only_when_call_in_flight(mcp_manager_instance, monkeypatch):
    manager = mcp_manager_instance
    config = MCPToolConfig(server_command="npx", server_args=["-y", "x"], tool_name="t")
    key = manager._get_config_key(config)
    shut_down = []
    monkeypatch.setattr(manager, "_shutdown_connection", lambda conn: shut_down.append(conn))

    # No call in flight (io lock free): warm connection is left alone, no reaper.
    idle_conn = _FakeIdleConn(locked=False)
    manager._connections[key] = cast(MCPConnection, idle_conn)
    assert manager._abort_in_flight(config) is None
    assert key in manager._connections
    assert shut_down == []

    # A call in flight (io lock held): detached synchronously, reaped off-loop.
    busy_conn = _FakeIdleConn(locked=True)
    manager._connections[key] = cast(MCPConnection, busy_conn)
    reaper = manager._abort_in_flight(config)
    # Detach is synchronous so no later call reuses the dying connection.
    assert key not in manager._connections
    # The subprocess reap runs on a daemon thread, not the caller's (so it never
    # blocks the agent event loop); join it before asserting it ran.
    assert reaper is not None and reaper is not threading.current_thread()
    reaper.join(timeout=5)
    assert shut_down == [busy_conn]


@pytest.mark.asyncio
async def test_call_tool_cancel_triggers_abort(mcp_manager_instance, monkeypatch):
    """Cancelling call_tool aborts the in-flight subprocess and re-raises."""
    manager = mcp_manager_instance
    config = MCPToolConfig(server_command="npx", server_args=["-y", "x"], tool_name="t")
    started = threading.Event()
    release = threading.Event()
    aborted = []
    monkeypatch.setattr(manager, "_abort_in_flight", lambda cfg: aborted.append(cfg))

    def blocking_call(cfg, params):
        # Runs on the dedicated MCP call pool. Cancelling the task cannot stop
        # this thread; released explicitly so it does not linger post-test.
        started.set()
        release.wait(30)
        return "unreachable"

    monkeypatch.setattr(manager, "call_tool_sync", blocking_call)

    try:
        task = asyncio.ensure_future(manager.call_tool(config, {}))
        await asyncio.get_event_loop().run_in_executor(None, started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert aborted == [config]
    finally:
        release.set()
        # Reset the module-level call pool so the released worker thread is
        # joined here rather than lingering into other tests / atexit.
        shutdown_mcp_manager()


def test_shutdown_mcp_manager_recreates_call_executor():
    """The dedicated MCP call pool is torn down and lazily rebuilt on shutdown."""
    shutdown_mcp_manager()
    try:
        first = _get_mcp_call_executor()
        assert _get_mcp_call_executor() is first  # cached singleton
        shutdown_mcp_manager()
        second = _get_mcp_call_executor()
        assert second is not first  # recreated, no use-after-shutdown
    finally:
        shutdown_mcp_manager()


def test_definition_clamps_out_of_range_timeouts():
    """Raw-edited out-of-range timeouts normalize instead of dropping tools.

    MCPToolConfig enforces bounds (ge/le); the definition is the raw-editable
    surface. An out-of-range value must clamp here so wrapping into an
    MCPToolConfig downstream does not raise and silently drop every tool.
    """
    defn = MCPServerDefinition(
        id="s1",
        name="s1",
        server_command="npx",
        call_timeout_seconds=5000,
        idle_timeout_seconds=1,
        startup_timeout_seconds=999,
    )
    assert defn.call_timeout_seconds == 900
    assert defn.idle_timeout_seconds == 30
    assert defn.startup_timeout_seconds == 120

    # The clamped values now construct a valid MCPToolConfig (no ValidationError).
    MCPToolConfig(
        server_command="npx",
        tool_name="t",
        call_timeout_seconds=defn.call_timeout_seconds,
        idle_timeout_seconds=defn.idle_timeout_seconds,
        startup_timeout_seconds=defn.startup_timeout_seconds,
    )


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
        actor,
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


# ---- exception chaining + read-loop catch narrowing (slice 05 F14) ----


def test_start_server_failure_chains_original_cause(monkeypatch):
    """The ``Failed to start MCP server`` re-wrap preserves ``__cause__``."""
    boom = OSError("popen exploded")

    def fake_popen(cmd, **kwargs):
        raise boom

    monkeypatch.setattr(mcp_manager.subprocess, "Popen", fake_popen)

    config = MCPToolConfig(
        server_command=sys.executable,
        server_args=["server.py"],
        tool_name="__discovery__",
    )
    manager = MCPServerManager()
    with pytest.raises(RuntimeError, match="Failed to start MCP server") as exc_info:
        manager._start_server(config)

    # Without ``from e`` the original OSError would be lost (only on __context__).
    assert exc_info.value.__cause__ is boom


def test_http_init_failure_chains_original_cause(monkeypatch):
    """The ``MCP HTTP server initialization failed`` re-wrap preserves ``__cause__``."""
    import httpx

    monkeypatch.setattr(
        mcp_manager, "validate_http_egress_url", lambda url, label=None: None
    )

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    boom = RuntimeError("init handshake failed")

    def fake_init(conn, init_timeout):
        raise boom

    manager = MCPServerManager()
    monkeypatch.setattr(manager, "_initialize_server", fake_init)

    config = MCPToolConfig(
        url="https://mcp.example.com/rpc",
        transport="http",
        tool_name="__discovery__",
        server_id="srv-http",
    )
    with pytest.raises(
        RuntimeError, match="MCP HTTP server initialization failed"
    ) as exc_info:
        manager._start_http_connection(config)

    assert exc_info.value.__cause__ is boom


class _FakeStdin:
    def write(self, data):
        return len(data)

    def flush(self):
        pass


class _ScriptedStdout:
    """``readline()`` replays a script: raise an Exception instance, else return bytes."""

    def __init__(self, script):
        self._script = list(script)

    def flush(self):
        pass

    def readline(self):
        if not self._script:
            raise AssertionError("readline() called more times than scripted")
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeStdioProcess:
    def __init__(self, stdout):
        self.stdin = _FakeStdin()
        self.stdout = stdout

    def poll(self):
        return None  # alive


class _FakeStdioConn:
    server_id = "srv-readloop"
    _stderr_thread = None

    def __init__(self, stdout):
        self.process = _FakeStdioProcess(stdout)

    def is_alive(self):
        return True

    def stderr_tail(self):
        # Matches MCPConnection: the timeout branch reads this for diagnostics.
        return []


def test_stdio_read_loop_retries_on_transient_io_errors(monkeypatch):
    """OSError/ValueError mid-read are transient pipe hiccups and get retried."""
    # Force the win32 read branch (plain flush + readline, no real ``select``).
    monkeypatch.setattr(mcp_manager.sys, "platform", "win32")
    response = b'{"jsonrpc": "2.0", "id": 7, "result": {"ok": true}}\n'
    stdout = _ScriptedStdout(
        [
            OSError("resource temporarily unavailable"),
            ValueError("I/O operation on closed file"),
            response,
        ]
    )
    conn = _FakeStdioConn(stdout)
    manager = MCPServerManager()

    msg = manager._stdio_send_request_locked(
        cast(mcp_manager.MCPConnection, conn),
        {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        timeout=5,
    )

    assert msg == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}


def test_stdio_read_loop_propagates_unexpected_errors(monkeypatch):
    """A non-IO error in the read path surfaces instead of being masked as transient."""
    monkeypatch.setattr(mcp_manager.sys, "platform", "win32")
    boom = RuntimeError("logic bug in read path")
    conn = _FakeStdioConn(_ScriptedStdout([boom]))
    manager = MCPServerManager()

    # Before narrowing, this RuntimeError was swallowed and the loop spun until the
    # deadline raised a misleading "Timeout" error; now the real bug propagates.
    with pytest.raises(RuntimeError, match="logic bug in read path"):
        manager._stdio_send_request_locked(
            cast(mcp_manager.MCPConnection, conn),
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            timeout=5,
        )


# ---- stdio server confinement (C1-02) ----------------------------------------


def test_the_stdio_server_launch_is_confined(mcp_manager_instance, monkeypatch):
    """The server process is the WRAPPED argv, not the raw command.

    An MCP stdio server is an arbitrary operator-configured program that the API
    process supervises for the life of the deployment. Confined it loses /proc,
    which is the route to the parent's environment (the vault master key, the
    service token, every provider key), and on a deployment whose data dir sits
    outside the project tree it loses the credential stores too.

    The failure this guards is the two-line shape reading as confined while
    running unconfined: build a launch, then spawn the original command.
    """
    captured: dict = {}

    def fake_launch(argv, kwargs, *, creation_roots=None):
        captured["argv"] = list(argv)
        captured["creation_roots"] = creation_roots
        captured["env"] = kwargs.get("env")
        return ["/shim", *argv]

    monkeypatch.setattr(mcp_manager, "sandbox_argv_launch", fake_launch)

    def fake_popen(cmd, **kwargs):
        captured["spawned"] = cmd
        raise RuntimeError("stop-after-spawn")

    monkeypatch.setattr(mcp_manager.subprocess, "Popen", fake_popen)

    config = MCPToolConfig(
        server_command=sys.executable,
        server_args=["server.py"],
        tool_name="__discovery__",
        server_id="srv-stdio",
    )
    with pytest.raises(RuntimeError, match="Failed to start MCP server"):
        mcp_manager_instance._start_server(config)

    assert captured["argv"] == [sys.executable, "server.py"]
    assert captured["spawned"] == ["/shim", sys.executable, "server.py"]
    # DEFAULT creation roots, deliberately, unlike the install runner next door.
    # A long-lived server's write area is whatever it was configured to manage,
    # and narrowing the roots would carve every ancestor of the data dir, making
    # anything the server creates after startup unreadable to it. Passing a root
    # here would be a behaviour change, not a tightening, so it is asserted.
    assert captured["creation_roots"] is None
    # The wrapper refuses a launch with no explicit env; the scrubbed stdio env
    # is built above it.
    assert captured["env"] is not None


def test_a_dead_server_reports_why_it_died_through_the_shim(
    mcp_manager_instance, monkeypatch
):
    """A missing `server_command` still reaches the caller with the reason.

    Wrapping changes the shape of this failure and it is worth pinning, because
    the obvious reading is that it gets lost. Unsandboxed, `Popen` raises
    `FileNotFoundError` and `_start_server` names the command. Under the sandbox
    the child is the shim, an interpreter that always exists, so `Popen`
    succeeds and the miss happens after the fork.

    Nothing was lost, and the existing machinery is what carries it: the shim
    writes `exec '<cmd>' failed` to stderr, `_drain_stderr` records it, the read
    loop notices the process is gone rather than waiting out the timeout, and
    `_stdio_error_detail` attaches the exit code and the stderr tail. This test
    exists so that chain cannot be broken silently, and so nobody re-adds a
    pre-spawn resolve check to "fix" a loss that is not there. One was tried:
    it also refused valid relative commands, because a resolve check here runs
    against THIS process's cwd while the child resolves after chdir into
    `working_directory`.
    """
    monkeypatch.setattr(
        mcp_manager, "_should_enforce_stdio_launch_allowlist", lambda: False
    )
    config = MCPToolConfig(
        server_command="definitely-not-a-real-mcp-binary",
        server_args=[],
        tool_name="__discovery__",
        server_id="srv-missing",
        startup_timeout_seconds=10,
    )
    with pytest.raises(RuntimeError) as excinfo:
        mcp_manager_instance._start_server(config)

    message = str(excinfo.value)
    assert "definitely-not-a-real-mcp-binary" in message, (
        "the failing command is not named anywhere in the error, so an operator "
        f"cannot tell what went wrong: {message!r}"
    )
    # Sandbox on, this is the shim's stderr arriving via _stdio_error_detail.
    # Sandbox off, it is the FileNotFoundError arm. Both name the command, which
    # is the property; asserting the exact wording would pin the deployment
    # rather than the behaviour.
    assert "not found" in message or "No such file" in message, message
