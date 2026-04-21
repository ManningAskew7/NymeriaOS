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
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools.definitions.schema import MCPToolConfig

logger = logging.getLogger(__name__)

# Per-phase timeouts (seconds). MCPToolConfig.startup_timeout_seconds overrides INIT.
INIT_TIMEOUT_DEFAULT = 10
LIST_TIMEOUT_DEFAULT = 30
CALL_TIMEOUT_DEFAULT = 60


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
    _request_id: int = 0
    _stderr_thread: Optional[threading.Thread] = None

    def next_request_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def touch(self) -> None:
        self.last_used = time.time()

    def is_alive(self) -> bool:
        if self.config.transport == "http":
            return self.http_client is not None
        return self.process is not None and self.process.poll() is None

    def is_idle(self, timeout_seconds: int) -> bool:
        return (time.time() - self.last_used) > timeout_seconds


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

        env = os.environ.copy()
        for key, value in config.env_vars.items():
            if value.startswith("${env:") and value.endswith("}"):
                var_name = value[6:-1]
                env[key] = os.environ.get(var_name, "")
            else:
                env[key] = value

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

        try:
            process = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            raise RuntimeError(f"MCP server command not found: {config.server_command}")
        except Exception as e:
            raise RuntimeError(f"Failed to start MCP server: {e}")

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
            raise RuntimeError(f"MCP server initialization failed: {e}")

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
                    logger.debug(f"[mcp:{conn.server_id}] {decoded}")
        except Exception as e:
            logger.debug(f"stderr drain ended for {conn.server_id}: {e}")

    # ---- http transport ----

    def _start_http_connection(self, config: MCPToolConfig) -> MCPConnection:
        try:
            import httpx
        except ImportError as e:
            raise RuntimeError(
                "httpx is required for HTTP-transport MCP servers"
            ) from e

        logger.info(f"Connecting to MCP server (http): {config.url}")

        # Interpolate ${env:VAR} in headers.
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        for k, v in config.headers.items():
            if v.startswith("${env:") and v.endswith("}"):
                headers[k] = os.environ.get(v[6:-1], "")
            else:
                headers[k] = v

        # Not using base_url: httpx appends a trailing slash on empty paths which
        # some MCP servers reject. We POST directly to config.url each request.
        client = httpx.Client(headers=headers, timeout=None)
        conn = MCPConnection(config=config, http_client=client)

        init_timeout = config.startup_timeout_seconds or INIT_TIMEOUT_DEFAULT
        try:
            self._initialize_server(conn, init_timeout=init_timeout)
        except Exception as e:
            self._shutdown_connection(conn)
            raise RuntimeError(f"MCP HTTP server initialization failed: {e}")

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
        if conn.config.transport == "http":
            return self._http_send_request(conn, request, timeout)
        return self._stdio_send_request(conn, request, timeout)

    def _send_notification(self, conn: MCPConnection, notification: Dict[str, Any]) -> None:
        if conn.config.transport == "http":
            self._http_send_notification(conn, notification)
        else:
            self._stdio_send_notification(conn, notification)

    # ---- stdio JSON-RPC ----

    def _stdio_send_request(
        self, conn: MCPConnection, request: Dict[str, Any], timeout: int
    ) -> Dict[str, Any]:
        if not conn.is_alive():
            raise RuntimeError("MCP server process has died")

        request_bytes = (json.dumps(request) + "\n").encode("utf-8")

        try:
            conn.process.stdin.write(request_bytes)
            conn.process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f"Failed to send request: {e}")

        start_time = time.time()
        response_line = b""

        while (time.time() - start_time) < timeout:
            if not conn.is_alive():
                raise RuntimeError("MCP server process died during request")
            try:
                if sys.platform == "win32":
                    conn.process.stdout.flush()
                    data = conn.process.stdout.readline()
                    if data:
                        response_line = data
                        break
                    time.sleep(0.01)
                else:
                    import select
                    readable, _, _ = select.select([conn.process.stdout], [], [], 0.1)
                    if readable:
                        response_line = conn.process.stdout.readline()
                        if response_line:
                            break
            except Exception as e:
                logger.debug(f"Read error (may be temporary): {e}")
                time.sleep(0.1)

        if not response_line:
            raise RuntimeError(f"Timeout waiting for MCP response after {timeout}s")

        try:
            return json.loads(response_line.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {e}")

    def _stdio_send_notification(
        self, conn: MCPConnection, notification: Dict[str, Any]
    ) -> None:
        if not conn.is_alive():
            return
        notification_bytes = (json.dumps(notification) + "\n").encode("utf-8")
        try:
            conn.process.stdin.write(notification_bytes)
            conn.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

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
            resp = client.post(conn.config.url, json=request, headers=headers, timeout=timeout)
        except Exception as e:
            raise RuntimeError(f"HTTP request failed: {e}")

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
            raise RuntimeError(f"Unexpected MCP HTTP response: {e}")

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
            client.post(conn.config.url, json=notification, headers=headers, timeout=10)
        except Exception as e:
            logger.debug(f"Notification send failed (ignored): {e}")

    # ---- tool call ----

    async def call_tool(self, config: MCPToolConfig, params: Dict[str, Any]) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.call_tool_sync, config, params)

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

            response = self._send_request(conn, request, timeout=CALL_TIMEOUT_DEFAULT)

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
            for stream in (conn.process.stdin, conn.process.stdout, conn.process.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass

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
