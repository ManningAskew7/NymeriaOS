"""MCP server API request schemas."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class MCPServerCreateRequest(BaseModel):
    """Request model for creating an MCP server."""

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    name: str = Field(..., min_length=1, max_length=128)
    description: str = ""
    server_command: str = Field(..., min_length=1)
    server_args: List[str] = []
    env_vars: Dict[str, str] = {}
    working_directory: Optional[str] = None
    idle_timeout_seconds: int = 300
    startup_timeout_seconds: int = 30
    enabled: bool = True


class MCPServerUpdateRequest(BaseModel):
    """Request model for updating an MCP server."""

    name: Optional[str] = None
    description: Optional[str] = None
    server_command: Optional[str] = None
    server_args: Optional[List[str]] = None
    env_vars: Optional[Dict[str, str]] = None
    working_directory: Optional[str] = None
    idle_timeout_seconds: Optional[int] = None
    startup_timeout_seconds: Optional[int] = None
    enabled: Optional[bool] = None


class MCPServerInstallRequest(BaseModel):
    source: str = Field(
        default="",
        description="Install source: Claude Desktop JSON blob, bare stdio command, HTTP URL, or registry id",
    )
    name: Optional[str] = None
    preview_token: Optional[str] = None
    candidate_id: Optional[str] = None
    confirmed: bool = False
    confirmed_risk_ids: List[str] = Field(default_factory=list)
    config_values: Dict[str, str] = Field(default_factory=dict)
    credential_values: Dict[str, str] = Field(default_factory=dict)
    credential_bindings: Dict[str, Any] = Field(default_factory=dict)
    auto_enable: bool = True
    thread_id: Optional[str] = None


class MCPServerInstallPreviewRequest(BaseModel):
    source: str = Field(..., description="Install source to inspect")
    name: Optional[str] = None


class MCPServerInstallRetryRequest(BaseModel):
    confirmed: bool = False
    confirmed_risk_ids: List[str] = Field(default_factory=list)
    config_values: Dict[str, str] = Field(default_factory=dict)
    credential_values: Dict[str, str] = Field(default_factory=dict)
    credential_bindings: Dict[str, Any] = Field(default_factory=dict)
