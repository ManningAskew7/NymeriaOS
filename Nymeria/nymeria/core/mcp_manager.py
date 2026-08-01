"""MCP Server Manager for Model Context Protocol servers.

Manages the lifecycle of MCP servers across two transports:
- stdio: local subprocesses with JSON-RPC over stdin/stdout
- http:  remote or Docker-MCP-Gateway endpoints over streamable HTTP

Shared behavior:
- Starts / connects on demand when a tool is called
- Maintains a connection pool with idle timeout
- Cleans up on idle, on process death, or on shutdown

Lifecycle hardening (2026):
- Subprocesses spawn in their own process group; shutdown kills the group
  so grandchildren (npx -> node -> mcp-server) do not leak.
- stderr is drained on a dedicated thread per connection to avoid pipe
  deadlocks on chatty servers (especially on Windows).
- Per-phase timeouts: init / tools/list / tools/call.
"""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..oom import with_tool_oom_score
from ..subprocess_env import NETWORK_RUNTIME_PASSTHROUGH, scrubbed_subprocess_env
from ..tools.definitions.mcp_schema import MCPToolConfig
from .http_policy import (
    HTTPPolicyRedirectLimit,
    HTTPPolicyViolation,
    httpx_request_with_policy,
    validate_http_egress_url,
)
from .credential_vault import SYSTEM_ACTOR
from .exec_policy import sandbox_argv_launch
from .secret_interpolation import resolve_env_and_credential_refs

logger = logging.getLogger(__name__)

_UNSAFE_EVAL_FLAGS = {
    "python": {"-c"},
    "python3": {"-c"},
    "node": {"-e", "--eval"},
    "deno": {"eval"},
    "bun": {"-e", "--eval"},
    "npm": {"exec", "x"},
    "npx": {"-c", "--call"},
}


def _extra_stdio_commands() -> set[str]:
    """Admin-configured launcher basenames added to the stdio allowlist."""
    from ..config import get_settings

    try:
        raw = getattr(get_settings(), "nymeria_mcp_extra_stdio_commands", "") or ""
    except Exception:
        logger.debug("Failed to read extra MCP stdio commands", exc_info=True)
        return set()
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _validate_stdio_launch(command: str, args: List[str]) -> None:
    from .mcp_sources import SAFE_STDIO_COMMANDS

    allowlist = SAFE_STDIO_COMMANDS | _extra_stdio_commands()
    basename = Path(command).name.lower()
    if re.fullmatch(r"python3(?:\.\d+)?", basename):
        basename = "python3"
    if basename not in allowlist:
        allowed = ", ".join(sorted(allowlist))
        raise RuntimeError(
            f"MCP stdio command '{command}' is not allowed. Allowed launchers: {allowed}"
        )

    blocked_flags = _UNSAFE_EVAL_FLAGS.get(basename, set())
    for arg in args:
        normalized = str(arg).strip().lower()
        if normalized in blocked_flags:
            raise RuntimeError(
                f"MCP stdio launcher '{basename}' cannot use unsafe argument '{arg}'"
            )


def _should_enforce_stdio_launch_allowlist() -> bool:
    from ..config import get_settings

    try:
        return bool(getattr(get_settings(), "nymeria_enforce_mcp_stdio_allowlist", False))
    except Exception:
        logger.debug("Failed to read MCP stdio allowlist setting", exc_info=True)
        return False


def _build_stdio_env(config: MCPToolConfig) -> Dict[str, str]:
    """Environment for an MCP stdio server subprocess.

    A scrubbed, deny-by-default base (shared allowlist) plus the server's own
    declared, credential-resolved ``env_vars``/``encrypted_env_vars``. The API
    process's secrets (master key, DB/Redis creds, service token, provider
    keys) are NOT inherited: a server that needs a value must declare it in its
    ``env_vars`` (``${credential:...}``/``${env:...}`` references still resolve
    against the parent environment; only the child's inherited base changes).
    Non-secret network/CA/runtime vars are opted back in so npx/uvx/node servers
    behind a proxy or custom CA keep working.
    """
    env = scrubbed_subprocess_env(NETWORK_RUNTIME_PASSTHROUGH)
    used_credentials: set[str] = set()
    for key, value in config.env_vars.items():
        env[key] = resolve_env_and_credential_refs(
            value,
            # SYSTEM_ACTOR: MCPToolConfig carries no owner, and this runs on the
            # server-startup path rather than in a user's turn. The read is
            # still bounded by allowed_targets containing mcp_server:<id>.
            actor=SYSTEM_ACTOR,
            target_type="mcp_server",
            target_id=config.server_id or config.server_command,
            used_credentials=used_credentials,
            missing_env="empty",
        )
    if config.encrypted_env_vars:
        from . import secrets as nymeria_secrets

        for key, value in config.encrypted_env_vars.items():
            env[key] = nymeria_secrets.decrypt(value)
    if used_credentials:
        logger.info(
            "Resolved %d credential reference(s) for MCP server %s",
            len(used_credentials),
            config.server_id or config.server_command,
        )
    return env


# Per-phase timeouts (seconds). MCPToolConfig.startup_timeout_seconds overrides INIT.
INIT_TIMEOUT_DEFAULT = 10
LIST_TIMEOUT_DEFAULT = 30
CALL_TIMEOUT_DEFAULT = 60

# MCP tool calls run on this dedicated, bounded pool rather than the asyncio
# default executor, so a burst of slow/hung MCP calls cannot starve every other
# ``run_in_executor(None, ...)``/``to_thread`` user on the loop. Blocking-I/O
# bound (each thread waits on a subprocess/HTTP round trip), so a modest cap is
# plenty; overflow queues.
_MCP_CALL_EXECUTOR_MAX_WORKERS = 16
_mcp_call_executor: Optional[ThreadPoolExecutor] = None
_mcp_call_executor_lock = threading.Lock()


def _get_mcp_call_executor() -> ThreadPoolExecutor:
    """Return the process-wide bounded executor for MCP tool calls."""
    global _mcp_call_executor
    with _mcp_call_executor_lock:
        if _mcp_call_executor is None:
            _mcp_call_executor = ThreadPoolExecutor(
                max_workers=_MCP_CALL_EXECUTOR_MAX_WORKERS,
                thread_name_prefix="mcp-call",
            )
        return _mcp_call_executor


@dataclass
class MCPConnection:
    """An active connection to an MCP server (stdio subprocess OR http endpoint)."""

    config: MCPToolConfig
    # stdio-only: the subprocess. None for http transport.
    process: Optional[subprocess.Popen] = None
    # http-only: the reusable client. None for stdio transport.
    http_client: Optional[Any] = None  # httpx.Client, lazily imported
    server_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    last_used: float = field(default_factory=time.time)
    initialized: bool = False
    available_tools: List[str] = field(default_factory=list)
    # HTTP: optional server-issued session id per the streamable-HTTP spec.
    session_id: Optional[str] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Serializes full request+response cycles on the stdio pipes. MCP stdio is
    # inherently a synchronous channel (one request in / one response out per
    # connection), so parallel tool_calls from the agent must queue here
    # rather than race on stdin/stdout — concurrent reads and writes tear
    # JSON-RPC frames and manifest as "Invalid JSON response" or hangs past
    # the call timeout.
    _io_lock: threading.Lock = field(default_factory=threading.Lock)
    # Count of requests currently in flight on this connection. stdio is already
    # serialized by ``_io_lock`` (held for the whole write+read cycle), but http
    # calls never take it, so this counter is what keeps the idle reaper and the
    # credential-rotation shutdown from tearing an http connection down mid-call.
    # A counter (not a lock) so concurrent http calls to one server are not
    # serialized.
    _inflight: int = 0
    _request_id: int = 0
    _stderr_thread: Optional[threading.Thread] = None
    _stderr_tail: List[str] = field(default_factory=list)
    _stderr_lock: threading.Lock = field(default_factory=threading.Lock)
    _stderr_tail_limit: int = 20

    def next_request_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def touch(self) -> None:
        self.last_used = time.time()

    def begin_request(self) -> None:
        """Mark a request in flight and stamp activity.

        The idle reaper and ``shutdown_server`` skip a connection with an
        in-flight request. The start stamp covers a call that queues on the io
        lock; ``end_request`` restarts the idle window from completion.
        """
        with self._lock:
            self._inflight += 1
        self.last_used = time.time()

    def end_request(self) -> None:
        """Clear one in-flight request and restart the idle window from now."""
        with self._lock:
            if self._inflight > 0:
                self._inflight -= 1
        self.last_used = time.time()

    def has_active_request(self) -> bool:
        with self._lock:
            return self._inflight > 0

    def is_alive(self) -> bool:
        if self.config.transport == "http":
            return self.http_client is not None
        return self.process is not None and self.process.poll() is None

    def is_idle(self, timeout_seconds: int) -> bool:
        return (time.time() - self.last_used) > timeout_seconds

    def record_stderr(self, line: str) -> None:
        with self._stderr_lock:
            self._stderr_tail.append(line)
            if len(self._stderr_tail) > self._stderr_tail_limit:
                del self._stderr_tail[: len(self._stderr_tail) - self._stderr_tail_limit]

    def stderr_tail(self) -> List[str]:
        with self._stderr_lock:
            return list(self._stderr_tail)


class MCPServerManager:
    """Manages MCP server connections (stdio subprocess or http endpoint).

    Connections start on demand and stay alive until their idle timeout expires.
    """

    def __init__(self):
        self._connections: Dict[str, MCPConnection] = {}
        self._lock = threading.Lock()

        # Background cleanup thread
        self._cleanup_interval = 30
        self._cleanup_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()

        self._start_cleanup_thread()

    def _start_cleanup_thread(self) -> None:
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="MCPCleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                self._cleanup_idle_connections()
            except Exception as e:
                logger.error(f"Error in MCP cleanup loop: {e}")
            self._shutdown_event.wait(self._cleanup_interval)

    def _cleanup_idle_connections(self) -> None:
        with self._lock:
            to_remove = []
            for key, conn in self._connections.items():
                if not conn.is_alive():
                    logger.info(f"MCP server {conn.server_id} died, removing connection")
                    self._shutdown_connection(conn)
                    to_remove.append(key)
                    continue
                # A tool call in flight must never be reaped even if last_used
                # looks stale (a single call can outlast the idle window): stdio
                # holds the io lock for the whole cycle, http tracks an in-flight
                # counter. Mirrors shutdown_server's skip_if_active guard.
                if conn._io_lock.locked() or conn.has_active_request():
                    continue
                if conn.is_idle(conn.config.idle_timeout_seconds):
                    logger.info(
                        f"MCP server {conn.server_id} idle for "
                        f">{conn.config.idle_timeout_seconds}s, shutting down"
                    )
                    self._shutdown_connection(conn)
                    to_remove.append(key)
            for key in to_remove:
                del self._connections[key]

    def _get_config_key(self, config: MCPToolConfig) -> str:
        """Unique key per server, NOT per tool, so tools sharing a server share one connection.

        Key includes transport so a stdio and http server with the same "identity"
        don't collide.
        """
        if config.transport == "http":
            return f"http|{config.url}"
        return f"stdio|{config.server_command}|{':'.join(config.server_args)}"

    def _get_or_create_connection(self, config: MCPToolConfig) -> MCPConnection:
        key = self._get_config_key(config)
        with self._lock:
            if key in self._connections:
                conn = self._connections[key]
                if conn.is_alive():
                    conn.touch()
                    return conn
                del self._connections[key]

            if config.transport == "http":
                conn = self._start_http_connection(config)
            else:
                conn = self._start_server(config)
            self._connections[key] = conn
            return conn

    # ---- stdio transport ----

    def _start_server(self, config: MCPToolConfig) -> MCPConnection:
        logger.info(
            f"Starting MCP server (stdio): {config.server_command} {' '.join(config.server_args)}"
        )

        env = _build_stdio_env(config)

        if _should_enforce_stdio_launch_allowlist():
            _validate_stdio_launch(config.server_command, config.server_args)
        cmd = [config.server_command] + config.server_args

        cwd = config.working_directory
        if cwd:
            cwd = os.path.expanduser(cwd)
            cwd = os.path.expandvars(cwd)

        # Spawn in a new process group so we can kill the whole tree on shutdown.
        popen_kwargs: Dict[str, Any] = dict(
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            text=False,
            bufsize=0,
        )
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True
        # An MCP server tree should be the kernel's OOM target under memory
        # pressure, not the API server that supervises it.
        with_tool_oom_score(popen_kwargs)

        # DEFAULT creation roots, unlike the install runner next door, which
        # names its own. The difference is how long the child lives, not what it
        # can be trusted with: an install finishes in seconds, a server runs for
        # days. Narrowing would keep the store denials on a source checkout, and
        # keeping a denial carves every ancestor of the data dir, which on that
        # layout includes the project root, a plausible place for a server to
        # work. A carve is a SNAPSHOT, so anything created in a carved directory
        # afterwards is writable and then unreadable, and over a session of days
        # that is a bad trade for a program whose write area we do not know.
        #
        # Note what this does NOT claim: the container layout carves the data
        # dir either way, so a server creating a new file directly there and
        # reading it back fails today (measured). The default roots make the
        # hazard SMALLER, not absent. Two things bound it, both measured, and
        # they are why it is a hazard rather than a break: only DIRECT children
        # of a carved container are affected, because anything deeper sits under
        # a directory that existed at policy-build time and got its own rule, so
        # a managed server can always read its own installed code; and the window
        # is the CONNECTION, not the process, because the policy is rebuilt per
        # spawn, so a restart or the idle reaper makes the file readable again.
        # Filed as a follow-up: managed servers get a working directory Nymeria
        # itself sets under the runtime tree, so for that population the write
        # area IS known and could be named, which would restore the store
        # denials on the slim shape.
        launch = sandbox_argv_launch(cmd, popen_kwargs)

        try:
            process = subprocess.Popen(launch, **popen_kwargs)
        except FileNotFoundError as e:
            # Sandbox off only. Under the sandbox the child is the shim, an
            # interpreter that always exists, so a missing server_command
            # instead reaches the caller through _stdio_error_detail: the read
            # loop notices the dead process, and the shim's own
            # "exec '<cmd>' failed: [Errno 2]" line is in the captured stderr
            # tail alongside the exit code. Verified, and the reason a resolve
            # check here would be wrong twice over: it would buy back an error
            # that is not lost, and both `which` and a path test resolve against
            # THIS process's cwd while the child resolves after chdir into
            # `working_directory`, so a valid relative command would be refused.
            raise RuntimeError(
                f"MCP server command not found: {config.server_command}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Failed to start MCP server: {e}") from e

        conn = MCPConnection(config=config, process=process)

        # Drain stderr on a background thread so the pipe never fills up.
        conn._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(conn,),
            name=f"MCPStderr-{conn.server_id}",
            daemon=True,
        )
        conn._stderr_thread.start()

        init_timeout = config.startup_timeout_seconds or INIT_TIMEOUT_DEFAULT
        try:
            self._initialize_server(conn, init_timeout=init_timeout)
        except Exception as e:
            self._shutdown_connection(conn)
            raise RuntimeError(f"MCP server initialization failed: {e}") from e

        logger.info(f"MCP server {conn.server_id} started and initialized")
        return conn

    def _drain_stderr(self, conn: MCPConnection) -> None:
        """Continuously read stderr so the pipe never fills up."""
        if not conn.process or not conn.process.stderr:
            return
        try:
            for line in iter(conn.process.stderr.readline, b""):
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                if decoded:
                    conn.record_stderr(decoded)
                    logger.debug(f"[mcp:{conn.server_id}] {decoded}")
        except Exception as e:
            logger.debug(f"stderr drain ended for {conn.server_id}: {e}")

    def _stdio_error_detail(self, conn: MCPConnection, message: str) -> str:
        """Build an actionable stdio process error without dumping unbounded logs."""
        if conn._stderr_thread and conn._stderr_thread.is_alive():
            conn._stderr_thread.join(timeout=0.2)

        exit_code = conn.process.poll() if conn.process else None
        detail = message
        if exit_code is not None:
            detail = f"{detail} (exit code {exit_code})"

        stderr_tail = conn.stderr_tail()
        if stderr_tail:
            stderr = "\n".join(stderr_tail[-8:])
            if len(stderr) > 2000:
                stderr = "..." + stderr[-2000:]
            detail = f"{detail}; stderr:\n{stderr}"
        return detail

    # ---- http transport ----

    def _start_http_connection(self, config: MCPToolConfig) -> MCPConnection:
        try:
            import httpx
        except ImportError as e:
            raise RuntimeError(
                "httpx is required for HTTP-transport MCP servers"
            ) from e

        logger.info(f"Connecting to MCP server (http): {config.url}")
        try:
            validate_http_egress_url(config.url, label="MCP HTTP server URL")
        except ValueError as e:
            raise RuntimeError(str(e)) from e

        # Interpolate ${env:VAR} and ${credential:ID.FIELD} in headers.
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        used_credentials: set[str] = set()
        for k, v in config.headers.items():
            headers[k] = resolve_env_and_credential_refs(
                v,
                # SYSTEM_ACTOR: see _build_stdio_env. Same bound applies here,
                # the read is scoped to mcp_server:<id> allowed targets.
                actor=SYSTEM_ACTOR,
                target_type="mcp_server",
                target_id=config.server_id or config.url,
                used_credentials=used_credentials,
                missing_env="empty",
            )
        if used_credentials:
            logger.info(
                "Resolved %d credential reference(s) for MCP HTTP server %s",
                len(used_credentials),
                config.server_id or config.url,
            )

        # Not using base_url: httpx appends a trailing slash on empty paths which
        # some MCP servers reject. We POST directly to config.url each request.
        client = httpx.Client(
            headers=headers,
            timeout=None,
            limits=httpx.Limits(max_keepalive_connections=0),
            trust_env=False,
        )
        conn = MCPConnection(config=config, http_client=client)

        init_timeout = config.startup_timeout_seconds or INIT_TIMEOUT_DEFAULT
        try:
            self._initialize_server(conn, init_timeout=init_timeout)
        except Exception as e:
            self._shutdown_connection(conn)
            raise RuntimeError(f"MCP HTTP server initialization failed: {e}") from e

        logger.info(f"MCP server {conn.server_id} connected over http")
        return conn

    # ---- shared handshake ----

    def _initialize_server(self, conn: MCPConnection, init_timeout: int) -> None:
        init_request = {
            "jsonrpc": "2.0",
            "id": conn.next_request_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "nymeria", "version": "1.0.0"},
            },
        }

        response = self._send_request(conn, init_request, timeout=init_timeout)
        if "error" in response:
            raise RuntimeError(f"Initialize failed: {response['error']}")

        init_notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        self._send_notification(conn, init_notification)

        tools_request = {
            "jsonrpc": "2.0",
            "id": conn.next_request_id(),
            "method": "tools/list",
        }
        tools_response = self._send_request(conn, tools_request, timeout=LIST_TIMEOUT_DEFAULT)
        if "result" in tools_response:
            tools = tools_response["result"].get("tools", [])
            conn.available_tools = [t.get("name", "") for t in tools]
            logger.debug(f"MCP server {conn.server_id} tools: {conn.available_tools}")

        conn.initialized = True

    def _send_request(
        self,
        conn: MCPConnection,
        request: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        # Mark the call in flight (and stamp activity) at both ends so neither
        # the idle reaper nor a credential-rotation shutdown tears the
        # connection down mid-call. stdio is protected by _io_lock (held for the
        # whole write+read cycle); http never takes it, so a call outlasting the
        # idle window would otherwise be reaped and its client closed under the
        # active request. begin_request also stamps last_used (covering a call
        # queued on the io lock) and end_request restarts the idle window from
        # completion so a long call is not immediately reaped by the next sweep.
        conn.begin_request()
        try:
            if conn.config.transport == "http":
                return self._http_send_request(conn, request, timeout)
            return self._stdio_send_request(conn, request, timeout)
        finally:
            conn.end_request()

    def _send_notification(self, conn: MCPConnection, notification: Dict[str, Any]) -> None:
        if conn.config.transport == "http":
            self._http_send_notification(conn, notification)
        else:
            self._stdio_send_notification(conn, notification)

    # ---- stdio JSON-RPC ----

    def _stdio_send_request(
        self, conn: MCPConnection, request: Dict[str, Any], timeout: int
    ) -> Dict[str, Any]:
        # Hold the per-connection IO lock for the full write+read cycle.
        # Parallel tool calls from the agent queue here rather than interleave
        # bytes on stdin/stdout.
        with conn._io_lock:
            return self._stdio_send_request_locked(conn, request, timeout)

    def _stdio_send_request_locked(
        self, conn: MCPConnection, request: Dict[str, Any], timeout: int
    ) -> Dict[str, Any]:
        if not conn.is_alive():
            raise RuntimeError(
                self._stdio_error_detail(conn, "MCP server process has died")
            )

        proc = conn.process
        if proc is None or proc.stdin is None:
            raise RuntimeError(
                self._stdio_error_detail(conn, "MCP server process or stdin not available")
            )

        request_bytes = (json.dumps(request) + "\n").encode("utf-8")

        try:
            proc.stdin.write(request_bytes)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            if not conn.is_alive():
                raise RuntimeError(
                    self._stdio_error_detail(conn, f"Failed to send request: {e}")
                ) from e
            raise RuntimeError(f"Failed to send request: {e}") from e

        # Read JSON-RPC lines until we see a response whose id matches this
        # request. Blank lines, notifications (no id), and unrelated responses
        # are skipped rather than mis-parsed as our response — which would
        # surface as "Invalid JSON response: Expecting value: line 2 column 1"
        # (from a lone "\n") or would return another call's payload.
        expected_id = request.get("id")
        deadline = time.time() + timeout

        stdout = proc.stdout
        if stdout is None:
            raise RuntimeError(
                self._stdio_error_detail(conn, "MCP server stdout not available")
            )

        while time.time() < deadline:
            if not conn.is_alive():
                raise RuntimeError(
                    self._stdio_error_detail(conn, "MCP server process died during request")
                )
            try:
                if sys.platform == "win32":
                    stdout.flush()
                    line = stdout.readline()
                    if not line:
                        time.sleep(0.01)
                        continue
                else:
                    import select
                    readable, _, _ = select.select([stdout], [], [], 0.1)
                    if not readable:
                        continue
                    line = stdout.readline()
                    if not line:
                        continue
            except (OSError, ValueError) as e:
                # Transient pipe/fd faults: OSError from select/readline on a
                # broken pipe, ValueError from readline on an already-closed
                # file. Retry those; anything else here is a logic bug and
                # should surface to the caller, not be masked as a read hiccup.
                logger.debug(f"Read error (may be temporary): {e}")
                time.sleep(0.1)
                continue

            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            try:
                msg = json.loads(text)
            except json.JSONDecodeError as e:
                logger.warning(
                    f"MCP server {conn.server_id} wrote non-JSON to stdout "
                    f"(skipping): {text[:200]!r} ({e})"
                )
                continue

            msg_id = msg.get("id") if isinstance(msg, dict) else None
            if msg_id != expected_id:
                logger.debug(
                    f"MCP server {conn.server_id} sent unrelated message "
                    f"(id={msg_id}, expected={expected_id}); skipping"
                )
                continue

            return msg

        detail = f"Timeout waiting for MCP response after {timeout}s"
        stderr_tail = conn.stderr_tail()
        if stderr_tail:
            stderr = "\n".join(stderr_tail[-8:])
            if len(stderr) > 2000:
                stderr = "..." + stderr[-2000:]
            detail = f"{detail}; recent stderr:\n{stderr}"
        raise RuntimeError(detail)

    def _stdio_send_notification(
        self, conn: MCPConnection, notification: Dict[str, Any]
    ) -> None:
        if not conn.is_alive():
            return
        proc = conn.process
        if proc is None or proc.stdin is None:
            return
        notification_bytes = (json.dumps(notification) + "\n").encode("utf-8")
        try:
            proc.stdin.write(notification_bytes)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # pipe may already be closed

    # ---- http JSON-RPC (streamable HTTP transport) ----

    def _http_send_request(
        self, conn: MCPConnection, request: Dict[str, Any], timeout: int
    ) -> Dict[str, Any]:
        client = conn.http_client
        if client is None:
            raise RuntimeError("MCP HTTP client is not initialized")

        headers = {}
        if conn.session_id:
            headers["Mcp-Session-Id"] = conn.session_id

        try:
            resp, _redirect_chain, _policy = httpx_request_with_policy(
                "POST",
                conn.config.url,
                client=client,
                json=request,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            )
        except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
            raise RuntimeError(f"HTTP request blocked by egress policy: {e}") from e
        except Exception as e:
            raise RuntimeError(f"HTTP request failed: {e}") from e

        if resp.status_code == 404 and conn.session_id:
            # Session expired; drop it and force a fresh connection next time.
            conn.session_id = None
            raise RuntimeError("MCP HTTP session expired")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        # Per spec, server MAY return a session id on initialize.
        new_session = resp.headers.get("Mcp-Session-Id")
        if new_session and not conn.session_id:
            conn.session_id = new_session

        content_type = resp.headers.get("Content-Type", "").lower()

        if content_type.startswith("application/json"):
            return resp.json()

        if content_type.startswith("text/event-stream"):
            # Find the first SSE "data:" frame that parses as a JSON-RPC response
            # matching our request id.
            target_id = request.get("id")
            for raw in resp.text.splitlines():
                if not raw.startswith("data:"):
                    continue
                payload = raw[5:].strip()
                if not payload:
                    continue
                try:
                    msg = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(msg, dict) and msg.get("id") == target_id:
                    return msg
            raise RuntimeError("No matching JSON-RPC response in SSE stream")

        # Unknown content type; try to parse as JSON anyway.
        try:
            return resp.json()
        except Exception as e:
            raise RuntimeError(f"Unexpected MCP HTTP response: {e}") from e

    def _http_send_notification(
        self, conn: MCPConnection, notification: Dict[str, Any]
    ) -> None:
        client = conn.http_client
        if client is None:
            return
        headers = {}
        if conn.session_id:
            headers["Mcp-Session-Id"] = conn.session_id
        try:
            httpx_request_with_policy(
                "POST",
                conn.config.url,
                client=client,
                json=notification,
                headers=headers,
                timeout=10,
                follow_redirects=False,
            )
        except (HTTPPolicyViolation, HTTPPolicyRedirectLimit) as e:
            logger.debug(f"Notification send blocked by egress policy (ignored): {e}")
        except Exception as e:
            logger.debug(f"Notification send failed (ignored): {e}")

    # ---- tool call ----

    async def call_tool(self, config: MCPToolConfig, params: Dict[str, Any]) -> str:
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(
                _get_mcp_call_executor(), self.call_tool_sync, config, params
            )
        except asyncio.CancelledError:
            # The turn was aborted. The executor thread can't be interrupted, so
            # tear down the in-flight subprocess to unblock its pipe read; the
            # orphaned thread then finishes fast and frees its pool slot. The
            # reap runs on its own daemon thread (see _abort_in_flight), so this
            # never blocks the event loop while the child is being killed.
            self._abort_in_flight(config)
            raise

    def _abort_in_flight(self, config: MCPToolConfig) -> Optional[threading.Thread]:
        """Kill the connection for *config* iff a call is in flight on it.

        Detaches the connection from the pool synchronously under ``self._lock``
        (the orphaned worker holds only ``_io_lock``, so no lock-ordering
        deadlock), then reaps the subprocess on a throwaway daemon thread. The
        reap must not run inline: this is called on the single agent event loop
        from ``call_tool``'s cancel handler, and ``_shutdown_connection`` blocks
        up to 5s in ``process.wait`` before escalating to SIGKILL. The SIGTERM
        that actually unblocks the orphaned worker's pipe read goes out at the
        top of that reap, so the loop is freed immediately; the wait/reap just
        finishes off-loop. The teardown never runs on the mcp-call pool (whose
        workers may be the very calls we are unblocking).

        Only fires when ``_io_lock`` is held, i.e. a call is genuinely
        mid-flight; a warm idle connection is left alone. This is a stdio-only
        teardown: http calls never take ``_io_lock`` (``.locked()`` is always
        False for them) and are bounded by ``_effective_call_timeout`` instead,
        so there is nothing to kill. MCP stdio serializes calls per connection,
        so at most one call is in flight. The connection is shared per server,
        not per turn, so under a cancellation-vs-completion race the reaped call
        may be a co-tenant turn's (a queued call, or briefly its freshly-started
        one) rather than the aborted turn's own; the co-tenant degrades to a
        clear "process died" ``[Error]`` string and retries, never hangs, which
        is the right outcome under a turn abort.

        Returns the reaper thread (or None when nothing was in flight) so
        callers/tests can join it; the production cancel path ignores it.
        """
        key = self._get_config_key(config)
        with self._lock:
            conn = self._connections.get(key)
            if conn is None or not conn._io_lock.locked():
                return None
            # Detach synchronously so no later call reuses this connection.
            self._connections.pop(key, None)
        logger.info("Aborting in-flight MCP call: tearing down %s", conn.server_id)
        reaper = threading.Thread(
            target=self._shutdown_connection,
            args=(conn,),
            name=f"mcp-abort-{conn.server_id}",
            daemon=True,
        )
        reaper.start()
        return reaper

    def _effective_call_timeout(self, config: MCPToolConfig) -> int:
        """Per-call timeout, clamped to the global tool_timeout ceiling.

        A server's ``call_timeout_seconds`` must never exceed the turn-level
        ``tool_timeout`` (the wider bracket SafeToolNode enforces), so a
        misconfigured server can't out-wait its own tool call.
        """
        call_timeout = getattr(config, "call_timeout_seconds", 0) or CALL_TIMEOUT_DEFAULT
        try:
            from ..config import get_settings

            ceiling = int(getattr(get_settings(), "tool_timeout", 0) or 0)
        except Exception:
            ceiling = 0
        return min(call_timeout, ceiling) if ceiling > 0 else call_timeout

    def call_tool_sync(self, config: MCPToolConfig, params: Dict[str, Any]) -> str:
        try:
            conn = self._get_or_create_connection(config)

            if config.tool_name not in conn.available_tools:
                available = ", ".join(conn.available_tools) if conn.available_tools else "none"
                return f"[Error]: Tool '{config.tool_name}' not found. Available: {available}"

            request = {
                "jsonrpc": "2.0",
                "id": conn.next_request_id(),
                "method": "tools/call",
                "params": {"name": config.tool_name, "arguments": params},
            }

            response = self._send_request(
                conn, request, timeout=self._effective_call_timeout(config)
            )

            if "error" in response:
                error = response["error"]
                return f"[Error]: {error.get('message', 'Unknown error')}"

            result = response.get("result", {})
            content = result.get("content", [])

            output_parts = []
            for block in content:
                block_type = block.get("type", "text")
                if block_type == "text":
                    text_value = block.get("text", "")
                    output_parts.append(str(text_value) if text_value is not None else "")
                elif block_type == "image":
                    output_parts.append(f"[Image: {block.get('mimeType', 'unknown')}]")
                elif block_type == "resource":
                    output_parts.append(f"[Resource: {block.get('uri', 'unknown')}]")
                else:
                    output_parts.append(json.dumps(block))

            return "\n".join(output_parts) if output_parts else "[No output]"

        except RuntimeError as e:
            return f"[Error]: {str(e)}"
        except Exception as e:
            logger.error(f"MCP tool call failed: {e}", exc_info=True)
            return f"[Error]: MCP tool call failed - {str(e)}"

    # ---- discovery ----

    def list_tools_detailed(self, config: MCPToolConfig) -> List[Dict[str, Any]]:
        """Return the full tool records advertised by a server.

        Encapsulates connection setup plus a ``tools/list`` request and returns the
        raw tool dicts (``name``, ``description``, ``inputSchema``) so callers (e.g.
        the server registry's ``discover_tools``) do not reach into connection
        internals or hand-build JSON-RPC. Returns an empty list if the server's
        response carries no ``result``.
        """
        conn = self._get_or_create_connection(config)
        tools_request = {
            "jsonrpc": "2.0",
            "id": conn.next_request_id(),
            "method": "tools/list",
        }
        timeout = config.startup_timeout_seconds or LIST_TIMEOUT_DEFAULT
        response = self._send_request(conn, tools_request, timeout=timeout)
        if "result" in response:
            return list(response["result"].get("tools", []))
        return []

    # ---- shutdown ----

    def _shutdown_connection(self, conn: MCPConnection) -> None:
        if conn.config.transport == "http":
            try:
                if conn.http_client is not None:
                    conn.http_client.close()
                conn.http_client = None
            except Exception as e:
                logger.warning(f"Error closing MCP http client {conn.server_id}: {e}")
            return

        try:
            if conn.process is not None and conn.process.poll() is None:
                # Kill the whole process group so grandchildren do not leak.
                try:
                    if sys.platform == "win32":
                        conn.process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        os.killpg(os.getpgid(conn.process.pid), signal.SIGTERM)
                except Exception as group_err:
                    logger.debug(f"Process-group signal failed: {group_err}")
                    conn.process.terminate()

                try:
                    conn.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        if sys.platform != "win32":
                            os.killpg(os.getpgid(conn.process.pid), signal.SIGKILL)
                        else:
                            conn.process.kill()
                    except Exception:
                        conn.process.kill()

            # Close pipes to unblock the stderr drain thread.
            if conn.process is None:
                return
            for stream in (conn.process.stdin, conn.process.stdout, conn.process.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    logger.debug("Error closing MCP process pipe during shutdown")

            logger.debug(f"Shutdown MCP server {conn.server_id}")
        except Exception as e:
            logger.warning(f"Error shutting down MCP server {conn.server_id}: {e}")

    def shutdown_all(self) -> None:
        logger.info("Shutting down all MCP servers...")
        self._shutdown_event.set()
        with self._lock:
            for conn in self._connections.values():
                self._shutdown_connection(conn)
            self._connections.clear()
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)
        logger.info("All MCP servers shutdown")

    def shutdown_server(self, server_id: str, *, skip_if_active: bool = True) -> str:
        """Force-shutdown a single MCP server connection by its config id.

        Used by the credential-resolution callback when a freshly-saved
        credential is bound to ``mcp_server:<server_id>``: killing the
        running connection ensures the next tool call respawns the server
        with the new vault value resolved into its env (env_var resolution
        runs at spawn time, not per tool call, see ``mcp_manager.py``
        around the ``${credential:...}`` block).

        Returns one of: ``"shutdown"`` (killed and removed),
        ``"not_running"`` (no live connection for that id),
        ``"skipped_in_use"`` (a tool call is in flight and
        ``skip_if_active=True``; idle sweep will eventually pick it up).

        Never raises: caller treats this as best-effort.
        """
        with self._lock:
            target_keys = [
                key for key, conn in self._connections.items()
                if (conn.config.server_id or conn.config.server_command) == server_id
            ]
            if not target_keys:
                return "not_running"

            results: List[str] = []
            for key in target_keys:
                conn = self._connections.get(key)
                if conn is None:
                    continue
                if skip_if_active and (
                    conn._io_lock.locked() or conn.has_active_request()
                ):
                    logger.info(
                        "shutdown_server skipping %s: call in flight",
                        server_id,
                    )
                    results.append("skipped_in_use")
                    continue
                try:
                    self._shutdown_connection(conn)
                finally:
                    self._connections.pop(key, None)
                results.append("shutdown")

        if "shutdown" in results:
            return "shutdown"
        if "skipped_in_use" in results:
            return "skipped_in_use"
        return "not_running"

    def get_active_servers(self) -> List[Dict[str, Any]]:
        with self._lock:
            out = []
            for conn in self._connections.values():
                entry: Dict[str, Any] = {
                    "server_id": conn.server_id,
                    "transport": conn.config.transport,
                    "tool_name": conn.config.tool_name,
                    "available_tools": conn.available_tools,
                    "last_used": conn.last_used,
                    "is_alive": conn.is_alive(),
                }
                if conn.config.transport == "http":
                    entry["url"] = conn.config.url
                else:
                    entry["command"] = conn.config.server_command
                out.append(entry)
            return out


_shared_mcp_manager: Optional[MCPServerManager] = None
_shared_mcp_manager_lock = threading.Lock()


def get_mcp_manager() -> MCPServerManager:
    """Return the process-wide MCP server manager."""
    global _shared_mcp_manager
    with _shared_mcp_manager_lock:
        if _shared_mcp_manager is None:
            _shared_mcp_manager = MCPServerManager()
        return _shared_mcp_manager


def shutdown_mcp_manager() -> None:
    """Shutdown and clear the process-wide MCP server manager, if it exists."""
    global _shared_mcp_manager, _mcp_call_executor
    with _shared_mcp_manager_lock:
        manager = _shared_mcp_manager
        _shared_mcp_manager = None
    if manager is not None:
        manager.shutdown_all()
    with _mcp_call_executor_lock:
        executor = _mcp_call_executor
        _mcp_call_executor = None
    if executor is not None:
        # Don't block shutdown on a wedged MCP call; the process is exiting.
        executor.shutdown(wait=False, cancel_futures=True)
