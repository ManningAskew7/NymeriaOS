"""Pydantic schemas for user-defined custom tools."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from ...core.time_utils import utc_now
from .mcp_schema import MCPToolConfig


class ToolParameter(BaseModel):
    """Definition of a single tool parameter."""

    type: Literal["string", "integer", "number", "boolean", "array", "object"] = Field(  # type: ignore[assignment]
        default="string",
        description="JSON Schema type of the parameter",
    )
    description: str = Field(
        default="",
        description="Human-readable description of the parameter",
    )
    required: bool = Field(
        default=False,
        description="Whether this parameter is required",
    )
    default: Optional[Any] = Field(
        default=None,
        description="Default value if not provided",
    )
    enum: Optional[List[str]] = Field(
        default=None,
        description="Allowed values (for string type)",
    )


class HTTPToolConfig(BaseModel):
    """Configuration for HTTP-based custom tools.

    Supports ${param} interpolation in URL, headers, query params, and body.
    Secrets should use credential-vault references such as
    ${credential:cred_id.value}; agent-created tools reject raw secrets and
    ${env:...} secret references.

    Example:
        url: "https://api.example.com/users/${user_id}"
        headers: {"Authorization": "Bearer ${credential:cred_id.value}"}
        body_template: '{"query": "${query}", "limit": ${limit}}'
    """

    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"] = Field(  # type: ignore[assignment]
        default="GET",
        description="HTTP method",
    )
    url: str = Field(
        ...,
        description=(
            "URL template with ${param} placeholders. Secret values should use "
            "${credential:cred_id.field}, not raw keys."
        ),
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "HTTP headers. Sensitive headers such as Authorization or X-API-Key "
            "must use ${credential:cred_id.field}; raw secrets are rejected."
        ),
    )
    body_template: Optional[str] = Field(
        default=None,
        description=(
            "JSON/text body template with ${param} placeholders. Secret values "
            "should use ${credential:cred_id.field}."
        ),
    )
    query_params: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Query parameter templates. Secret values should use "
            "${credential:cred_id.field}."
        ),
    )
    timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Request timeout in seconds",
    )
    response_path: Optional[str] = Field(
        default=None,
        description=(
            "JSONPath to extract result (e.g., '$.data.items'). A path that does "
            "not match the response returns an error instead of the full body."
        ),
    )
    response_format: Literal["json", "text", "auto"] = Field(
        default="auto",
        description="How to parse the response",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate URL format."""
        if not v.startswith(("http://", "https://", "${env:")):
            raise ValueError("URL must start with http://, https://, or ${env:}")
        return v


class PythonToolConfig(BaseModel):
    """Configuration for subprocess-backed Python custom tools."""

    source_code: str = Field(
        ...,
        min_length=1,
        description="Python source code containing the tool entrypoint",
    )
    entrypoint: str = Field(
        default="run",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Callable function name to invoke",
    )
    runtime: Literal["subprocess"] = Field(
        default="subprocess",
        description="Execution runtime. Python custom tools run out-of-process.",
    )


class WorkflowToolConfig(BaseModel):
    """Configuration for nym-SDK workflow custom tools.

    The source runs out of process against the parent-side nym.* verb RPC
    (core/workflows). The definition's ``parameters`` map is DERIVED from the
    entrypoint signature at draft time (never hand-declared), and
    ``revision_hash`` covers {source, entrypoint, continuations, parameters},
    so any behavior-changing edit resets approval by construction. Execution
    of a revision requires ``approved_revision`` to equal the recomputed hash
    (checked at execution time; see core/workflows/authoring.py).
    """

    source_code: str = Field(
        ...,
        min_length=1,
        description="Python source containing the workflow entrypoint",
    )
    entrypoint: str = Field(
        default="run",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Entrypoint function name to invoke",
    )
    continuations: List[str] = Field(
        default_factory=list,
        description=(
            "Declared continuation entrypoints for nym.approve resume; each "
            "must exist in the source with signature (state, decision)"
        ),
    )
    revision_hash: str = Field(
        default="",
        description=(
            "Content hash over {source, entrypoint, continuations, parameters}; "
            "recomputed on every save"
        ),
    )
    approved_revision: Optional[str] = Field(
        default=None,
        description="revision_hash an admin approved; execution requires a match",
    )
    approved_by: Optional[str] = Field(default=None, description="Approving admin user id")
    approved_at: Optional[datetime] = Field(default=None, description="Approval timestamp")
    declined_by: Optional[str] = Field(default=None, description="Declining admin user id")
    declined_at: Optional[datetime] = Field(default=None, description="Decline timestamp")
    declined_revision: Optional[str] = Field(
        default=None,
        description="revision_hash an admin declined (a re-request needs an edit)",
    )
    decline_note: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Admin note explaining a decline (author-visible)",
    )
    created_by: str = Field(
        default="",
        description="Author user id; approval decisions notify this user",
    )
    wall_clock_seconds: Optional[float] = Field(
        default=None,
        ge=5,
        le=3600,
        description="Budget override: run wall clock (engine default when unset)",
    )
    max_calls: Optional[int] = Field(
        default=None,
        ge=1,
        le=500,
        description="Budget override: total nym.* calls (engine default when unset)",
    )
    max_ai_calls: Optional[int] = Field(
        default=None,
        ge=0,
        le=100,
        description="Budget override: AI nym.* calls (engine default when unset)",
    )

    @field_validator("continuations")
    @classmethod
    def validate_continuations(cls, v: List[str]) -> List[str]:
        """Each continuation must be a valid Python identifier."""
        import re

        for name in v:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
                raise ValueError(f"invalid continuation entrypoint name: {name!r}")
        return v


class CustomToolDefinition(BaseModel):
    """Complete definition of a custom tool.

    Custom tools can be HTTP-based, MCP-based, subprocess-backed Python, or
    nym-SDK workflows.

    The tool is converted to a LangChain @tool function at runtime,
    with parameters extracted from the definition.
    """

    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$",
        description="Unique identifier (alphanumeric, underscore, hyphen)",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Human-readable name for the tool",
    )
    description: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Description shown to the LLM for tool selection",
    )
    parameters: Dict[str, ToolParameter] = Field(
        default_factory=dict,
        description="Tool parameters with their schemas",
    )
    implementation_type: Literal["http", "mcp", "python", "workflow"] = Field(
        ...,
        description="Type of tool implementation",
    )
    http_config: Optional[HTTPToolConfig] = Field(
        default=None,
        description="HTTP tool configuration (required if type is 'http')",
    )
    mcp_config: Optional[MCPToolConfig] = Field(
        default=None,
        description="MCP tool configuration (required if type is 'mcp')",
    )
    python_config: Optional[PythonToolConfig] = Field(
        default=None,
        description="Python tool configuration (required if type is 'python')",
    )
    workflow_config: Optional[WorkflowToolConfig] = Field(
        default=None,
        description="Workflow tool configuration (required if type is 'workflow')",
    )
    enabled: bool = Field(
        default=True,
        description="Whether the tool is enabled",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Tags for categorization",
    )
    created_at: datetime = Field(
        default_factory=utc_now,
        description="Creation timestamp",
    )
    updated_at: datetime = Field(
        default_factory=utc_now,
        description="Last update timestamp",
    )

    @field_validator("http_config", "mcp_config")
    @classmethod
    def validate_config_matches_type(cls, v, info):
        """Config will be validated in model_post_init."""
        return v

    def model_post_init(self, __context: Any) -> None:
        """Validate that the config matches the implementation type."""
        if self.implementation_type == "http" and self.http_config is None:
            raise ValueError("http_config is required when implementation_type is 'http'")
        if self.implementation_type == "mcp" and self.mcp_config is None:
            raise ValueError("mcp_config is required when implementation_type is 'mcp'")
        if self.implementation_type == "python" and self.python_config is None:
            raise ValueError("python_config is required when implementation_type is 'python'")
        if self.implementation_type == "workflow" and self.workflow_config is None:
            raise ValueError("workflow_config is required when implementation_type is 'workflow'")

    def to_json_schema(self) -> Dict[str, Any]:
        """Convert parameters to JSON Schema format for LangChain."""
        properties = {}
        required = []

        for name, param in self.parameters.items():
            prop: Dict[str, Any] = {
                "type": param.type,
                "description": param.description,
            }
            if param.default is not None:
                prop["default"] = param.default
            if param.enum:
                prop["enum"] = param.enum
            properties[name] = prop

            if param.required:
                required.append(name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }


__all__ = [
    "CustomToolDefinition",
    "HTTPToolConfig",
    "MCPToolConfig",
    "PythonToolConfig",
    "ToolParameter",
    "WorkflowToolConfig",
]
