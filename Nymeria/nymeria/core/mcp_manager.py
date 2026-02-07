"""MCP Server Manager for Model Context Protocol servers.

Manages the lifecycle of MCP server subprocesses:
- Starts servers on-demand when a tool is called
- Maintains connection pools with idle timeout
- Handles JSON-RPC communication over stdio
- Cleans up servers on shutdown

Based on MCP SDK best practices 2025-2026:
- Use stdio transport for local servers
- Never write to stdout in server (corrupts JSON-RPC)
- Implement idle timeout for resource management
- Use SDK version 1.2.0+ for stability
"""

import asyncio
import json
import logging
import os
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


@dataclass
class MCPConnection:
    """Represents an active connection to an MCP server."""

    config: MCPToolConfig
    process: subprocess.Popen
    server_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    last_used: float = field(default_factory=time.time)
    initialized: bool = False
    available_tools: List[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _request_id: int = 0

    def next_request_id(self) -> int:
        """Get the next request ID for JSON-RPC."""
        with self._lock:
            self._request_id += 1
            return self._request_id

    def touch(self) -> None:
        """Update last used timestamp."""
        self.last_used = time.time()

    def is_alive(self) -> bool:
        """Check if the server process is still running."""
        return self.process.poll() is None

    def is_idle(self, timeout_seconds: int) -> bool:
        """Check if the connection has been idle for too long."""
        return (time.time() - self.last_used) > timeout_seconds


class MCPServerManager:
    """Manages MCP server subprocesses and connections.

    Servers are started on-demand and kept alive with an idle timeout.
    JSON-RPC messages are sent/received over stdin/stdout.
    """

    def __init__(self):
        """Initialize the MCP server manager."""
        # Active connections: config_key -> MCPConnection
        self._connections: Dict[str, MCPConnection] = {}
        self._lock = threading.Lock()

        # Background cleanup thread
        self._cleanup_interval = 30  # seconds
        self._cleanup_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()

        # Start cleanup thread
        self._start_cleanup_thread()

    def _start_cleanup_thread(self) -> None:
        """Start the background cleanup thread."""
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="MCPCleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        """Background thread to cleanup idle connections."""
        while not self._shutdown_event.is_set():
            try:
                self._cleanup_idle_connections()
            except Exception as e:
                logger.error(f"Error in MCP cleanup loop: {e}")

            # Wait for cleanup interval or shutdown
            self._shutdown_event.wait(self._cleanup_interval)

    def _cleanup_idle_connections(self) -> None:
        """Cleanup connections that have been idle too long."""
        with self._lock:
            to_remove = []

            for key, conn in self._connections.items():
                # Check if process is dead
                if not conn.is_alive():
                    logger.info(f"MCP server {conn.server_id} died, removing connection")
                    to_remove.append(key)
                    continue

                # Check idle timeout
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
        """Generate a unique key for a config to identify connections."""
        # Use command + args + tool_name as the key
        return f"{config.server_command}|{':'.join(config.server_args)}|{config.tool_name}"

    def _get_or_create_connection(self, config: MCPToolConfig) -> MCPConnection:
        """Get an existing connection or create a new one.

        Args:
            config: MCP tool configuration.

        Returns:
            Active MCP connection.

        Raises:
            RuntimeError: If server fails to start or initialize.
        """
        key = self._get_config_key(config)

        with self._lock:
            # Check for existing connection
            if key in self._connections:
                conn = self._connections[key]
                if conn.is_alive():
                    conn.touch()
                    return conn
                else:
                    # Dead connection, remove and create new
                    del self._connections[key]

            # Create new connection
            conn = self._start_server(config)
            self._connections[key] = conn
            return conn

    def _start_server(self, config: MCPToolConfig) -> MCPConnection:
        """Start a new MCP server subprocess.

        Args:
            config: MCP tool configuration.

        Returns:
            New MCP connection.

        Raises:
            RuntimeError: If server fails to start.
        """
        logger.info(f"Starting MCP server: {config.server_command} {' '.join(config.server_args)}")

        # Prepare environment
        env = os.environ.copy()
        for key, value in config.env_vars.items():
            # Interpolate environment variables in values
            if value.startswith("${env:") and value.endswith("}"):
                var_name = value[6:-1]
                env[key] = os.environ.get(var_name, "")
            else:
                env[key] = value

        # Prepare command
        cmd = [config.server_command] + config.server_args

        # Prepare working directory
        cwd = config.working_directory
        if cwd:
            cwd = os.path.expanduser(cwd)
            cwd = os.path.expandvars(cwd)

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                text=False,  # Binary mode for proper encoding
                bufsize=0,  # Unbuffered
            )
        except FileNotFoundError:
            raise RuntimeError(f"MCP server command not found: {config.server_command}")
        except Exception as e:
            raise RuntimeError(f"Failed to start MCP server: {e}")

        # Create connection
        conn = MCPConnection(config=config, process=process)

        # Initialize the server (send initialize request)
        try:
            self._initialize_server(conn, timeout=config.startup_timeout_seconds)
        except Exception as e:
            # Cleanup on failure
            process.kill()
            raise RuntimeError(f"MCP server initialization failed: {e}")

        logger.info(f"MCP server {conn.server_id} started and initialized")
        return conn

    def _initialize_server(self, conn: MCPConnection, timeout: int = 30) -> None:
        """Initialize the MCP server with the initialize request.

        Args:
            conn: MCP connection.
            timeout: Initialization timeout in seconds.

        Raises:
            RuntimeError: If initialization fails.
        """
        # Send initialize request
        init_request = {
            "jsonrpc": "2.0",
            "id": conn.next_request_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {
                    "name": "nymeria",
                    "version": "1.0.0",
                },
            },
        }

        response = self._send_request(conn, init_request, timeout=timeout)

        if "error" in response:
            raise RuntimeError(f"Initialize failed: {response['error']}")

        # Send initialized notification
        init_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }
        self._send_notification(conn, init_notification)

        # Get available tools
        tools_request = {
            "jsonrpc": "2.0",
            "id": conn.next_request_id(),
            "method": "tools/list",
        }

        tools_response = self._send_request(conn, tools_request, timeout=timeout)
        if "result" in tools_response:
            tools = tools_response["result"].get("tools", [])
            conn.available_tools = [t.get("name", "") for t in tools]
            logger.debug(f"MCP server {conn.server_id} tools: {conn.available_tools}")

        conn.initialized = True

    def _send_request(
        self,
        conn: MCPConnection,
        request: Dict[str, Any],
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request and wait for response.

        Args:
            conn: MCP connection.
            request: JSON-RPC request object.
            timeout: Response timeout in seconds.

        Returns:
            JSON-RPC response object.

        Raises:
            RuntimeError: If communication fails.
        """
        if not conn.is_alive():
            raise RuntimeError("MCP server process has died")

        # Serialize and send request
        request_bytes = (json.dumps(request) + "\n").encode("utf-8")

        try:
            conn.process.stdin.write(request_bytes)
            conn.process.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f"Failed to send request: {e}")

        # Read response
        start_time = time.time()
        response_line = b""

        while (time.time() - start_time) < timeout:
            if not conn.is_alive():
                # Check stderr for error messages
                stderr = conn.process.stderr.read()
                if stderr:
                    logger.error(f"MCP server stderr: {stderr.decode('utf-8', errors='replace')}")
                raise RuntimeError("MCP server process died during request")

            try:
                # Non-blocking read with select
                import select
                if sys.platform == "win32":
                    # Windows doesn't support select on pipes
                    # Use a small timeout read
                    conn.process.stdout.flush()
                    data = conn.process.stdout.readline()
                    if data:
                        response_line = data
                        break
                    time.sleep(0.01)
                else:
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
            response = json.loads(response_line.decode("utf-8"))
            return response
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Invalid JSON response: {e}")

    def _send_notification(self, conn: MCPConnection, notification: Dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no response expected).

        Args:
            conn: MCP connection.
            notification: JSON-RPC notification object.
        """
        if not conn.is_alive():
            return

        notification_bytes = (json.dumps(notification) + "\n").encode("utf-8")

        try:
            conn.process.stdin.write(notification_bytes)
            conn.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # Ignore errors for notifications

    async def call_tool(self, config: MCPToolConfig, params: Dict[str, Any]) -> str:
        """Call a tool on an MCP server.

        This is the main entry point for MCP tool execution.

        Args:
            config: MCP tool configuration.
            params: Tool parameters.

        Returns:
            Tool result as a string.
        """
        # Run in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.call_tool_sync, config, params)

    def call_tool_sync(self, config: MCPToolConfig, params: Dict[str, Any]) -> str:
        """Synchronous version of call_tool.

        Args:
            config: MCP tool configuration.
            params: Tool parameters.

        Returns:
            Tool result as a string.
        """
        try:
            conn = self._get_or_create_connection(config)

            # Check if tool is available
            if config.tool_name not in conn.available_tools:
                available = ", ".join(conn.available_tools) if conn.available_tools else "none"
                return f"[Error]: Tool '{config.tool_name}' not found. Available: {available}"

            # Send tool call request
            request = {
                "jsonrpc": "2.0",
                "id": conn.next_request_id(),
                "method": "tools/call",
                "params": {
                    "name": config.tool_name,
                    "arguments": params,
                },
            }

            response = self._send_request(conn, request, timeout=60)

            if "error" in response:
                error = response["error"]
                return f"[Error]: {error.get('message', 'Unknown error')}"

            # Extract result content
            result = response.get("result", {})
            content = result.get("content", [])

            # Format content blocks
            output_parts = []
            for block in content:
                block_type = block.get("type", "text")
                if block_type == "text":
                    # Ensure text is always a string (some MCP servers return numbers)
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

    def _shutdown_connection(self, conn: MCPConnection) -> None:
        """Shutdown a single connection.

        Args:
            conn: Connection to shutdown.
        """
        try:
            if conn.is_alive():
                # Try graceful shutdown
                conn.process.terminate()
                try:
                    conn.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    conn.process.kill()

            logger.debug(f"Shutdown MCP server {conn.server_id}")
        except Exception as e:
            logger.warning(f"Error shutting down MCP server {conn.server_id}: {e}")

    def shutdown_all(self) -> None:
        """Shutdown all MCP server connections."""
        logger.info("Shutting down all MCP servers...")

        # Signal cleanup thread to stop
        self._shutdown_event.set()

        # Shutdown all connections
        with self._lock:
            for conn in self._connections.values():
                self._shutdown_connection(conn)
            self._connections.clear()

        # Wait for cleanup thread
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)

        logger.info("All MCP servers shutdown")

    def get_active_servers(self) -> List[Dict[str, Any]]:
        """Get information about active MCP servers.

        Returns:
            List of server info dicts.
        """
        with self._lock:
            return [
                {
                    "server_id": conn.server_id,
                    "command": conn.config.server_command,
                    "tool_name": conn.config.tool_name,
                    "available_tools": conn.available_tools,
                    "last_used": conn.last_used,
                    "is_alive": conn.is_alive(),
                }
                for conn in self._connections.values()
            ]
