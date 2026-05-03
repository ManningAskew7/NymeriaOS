"""Pydantic schemas for MCP server and runtime definitions."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from ...core.time_utils import utc_now

MCPTransport = Literal["stdio", "http"]
MCPInstallStatus = Literal["ready", "draft", "failed"]


class MCPDiscoveredTool(BaseModel):
    """A tool discovered from an MCP server via tools/list."""

    name: str
    description: str = ""
    input_schema: Dict[str, Any] = {}


class MCPServerDefinition(BaseModel):
    """Definition of an MCP server and its discovered tools.

    Two transports supported:
    - stdio: local subprocess (server_command + server_args are required)
    - http:  remote or Docker-MCP-Gateway HTTP endpoint (url is required)
    """

    id: str
    name: str
    description: str = ""
    transport: MCPTransport = "stdio"
    # stdio-transport fields
    server_command: str = ""
    server_args: List[str] = []
    # http-transport fields
    url: str = ""
    headers: Dict[str, str] = {}
    # shared
    env_vars: Dict[str, str] = {}
    encrypted_env_vars: Dict[str, str] = {}
    working_directory: Optional[str] = None
    idle_timeout_seconds: int = 300
    startup_timeout_seconds: int = 30
    enabled: bool = True
    discovered_tools: List[MCPDiscoveredTool] = []
    install_status: MCPInstallStatus = "ready"
    source_type: str = ""
    runtime_type: str = ""
    original_source: str = ""
    parsed_summary: str = ""
    install_plan: Dict[str, Any] = {}
    install_logs: List[str] = []
    last_error: Optional[str] = None
    missing_config: List[Dict[str, Any]] = []
    risk_level: str = "low"
    confirmation_required: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> "MCPServerDefinition":
        if self.transport == "stdio" and not self.server_command and self.install_status == "ready":
            raise ValueError("server_command is required when transport is 'stdio'")
        if self.transport == "http" and not self.url:
            raise ValueError("url is required when transport is 'http'")
        return self


class MCPToolConfig(BaseModel):
    """Configuration for MCP (Model Context Protocol) server tools.

    MCP servers communicate via JSON-RPC. Two transports supported:
    - stdio: local subprocess (server_command + server_args)
    - http:  remote or Docker-MCP-Gateway HTTP endpoint (url + headers)

    Best practices (MCP SDK 2025-2026):
    - Never write to stdout in STDIO servers (use stderr for logging)
    - Use SDK version 1.2.0+ for stability
    - Set idle timeout for resource management

    Example (stdio):
        server_command: "npx"
        server_args: ["-y", "@anthropic/mcp-server-filesystem", "/allowed/path"]
        tool_name: "read_file"

    Example (http):
        transport: "http"
        url: "http://localhost:8811/mcp"
        tool_name: "read_file"
    """

    transport: MCPTransport = Field(
        default="stdio",
        description="Transport type: 'stdio' for local subprocess, 'http' for remote",
    )
    server_command: str = Field(
        default="",
        description="Command to start the MCP server (stdio transport only)",
    )
    server_args: List[str] = Field(
        default_factory=list,
        description="Command line arguments for the server (stdio transport only)",
    )
    url: str = Field(
        default="",
        description="HTTP/SSE endpoint URL (http transport only)",
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers (http transport only; supports ${env:VAR} for secrets)",
    )
    tool_name: str = Field(
        ...,
        description="Name of the tool exposed by the MCP server",
    )
    env_vars: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables for the server process (supports ${env:VAR})",
    )
    encrypted_env_vars: Dict[str, str] = Field(
        default_factory=dict,
        description="Encrypted environment variable values for the server process",
    )
    working_directory: Optional[str] = Field(
        default=None,
        description="Working directory for the server process",
    )
    idle_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Seconds of idle time before shutting down server",
    )
    startup_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Seconds to wait for server initialization",
    )

    @model_validator(mode="after")
    def _validate_transport_fields(self) -> "MCPToolConfig":
        if self.transport == "stdio" and not self.server_command:
            raise ValueError("server_command is required when transport is 'stdio'")
        if self.transport == "http" and not self.url:
            raise ValueError("url is required when transport is 'http'")
        return self


__all__ = [
    "MCPDiscoveredTool",
    "MCPInstallStatus",
    "MCPServerDefinition",
    "MCPToolConfig",
    "MCPTransport",
]
