"""Custom tool API schemas and conversion helpers."""

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from ...tools.definitions.custom_tool_schema import (
    CustomToolDefinition,
    HTTPToolConfig,
    PythonToolConfig,
    ToolParameter,
    WorkflowToolConfig,
)
from ...tools.definitions.mcp_schema import MCPToolConfig


ParameterType = Literal["string", "integer", "number", "boolean", "array", "object"]
HTTPMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
ResponseFormat = Literal["json", "text", "auto"]
ImplementationType = Literal["http", "mcp", "python", "workflow"]


class ToolParameterModel(BaseModel):
    """API model for tool parameters."""

    type: ParameterType = "string"
    description: str = ""
    required: bool = False
    default: str | None = None
    enum: list[str] | None = None


class HTTPToolConfigModel(BaseModel):
    """API model for HTTP tool configuration."""

    method: HTTPMethod = Field(default="GET", description="HTTP method")  # type: ignore[assignment]
    url: str = Field(
        ...,
        description=(
            "URL template with ${param} placeholders. Use "
            "${credential:cred_id.field} for secret values."
        ),
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "HTTP headers. Sensitive headers must use "
            "${credential:cred_id.field}; raw secrets are rejected."
        ),
    )
    body_template: str | None = Field(
        default=None,
        description=(
            "JSON/text body template with ${param} placeholders. Use "
            "${credential:cred_id.field} for secret values."
        ),
    )
    query_params: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Query parameter templates. Use ${credential:cred_id.field} for "
            "secret values."
        ),
    )
    timeout_seconds: int = Field(default=30, description="Request timeout in seconds")
    response_path: str | None = Field(
        default=None,
        description=(
            "Optional JSON path to extract from a JSON response. A path that does "
            "not match the response returns an error instead of the full body."
        ),
    )
    response_format: ResponseFormat = Field(
        default="auto",
        description="Response parsing mode",
    )


class MCPToolConfigModel(BaseModel):
    """API model for MCP tool configuration."""

    server_command: str
    server_args: list[str] = []
    tool_name: str
    env_vars: dict[str, str] = {}
    working_directory: str | None = None
    idle_timeout_seconds: int = 300
    startup_timeout_seconds: int = 30


class PythonToolConfigModel(BaseModel):
    """API model for subprocess-backed Python custom tools."""

    source_code: str
    entrypoint: str = "run"
    runtime: Literal["subprocess"] = "subprocess"


class WorkflowToolConfigModel(BaseModel):
    """API request model for nym-SDK workflow custom tools.

    Approval fields are deliberately absent: the server derives the revision
    hash from content and self-approves for the (admin) REST actor. Tool
    parameters are derived from the entrypoint signature, never submitted.
    """

    source_code: str
    entrypoint: str = "run"
    continuations: list[str] = []
    wall_clock_seconds: float | None = None
    max_calls: int | None = None
    max_ai_calls: int | None = None


class WorkflowToolConfigResponseModel(WorkflowToolConfigModel):
    """API response model: request fields plus server-managed state."""

    revision_hash: str = ""
    approval: str = "pending"
    approved_revision: str | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None
    declined_by: str | None = None
    declined_at: datetime | None = None
    decline_note: str | None = None
    created_by: str = ""


class CustomToolResponse(BaseModel):
    """Response model for a custom tool."""

    id: str
    name: str
    description: str
    parameters: dict[str, ToolParameterModel]
    implementation_type: ImplementationType
    http_config: HTTPToolConfigModel | None = None
    mcp_config: MCPToolConfigModel | None = None
    python_config: PythonToolConfigModel | None = None
    workflow_config: WorkflowToolConfigResponseModel | None = None
    enabled: bool
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime


class CustomToolCreateRequest(BaseModel):
    """Request model for creating a custom tool."""

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=1000)
    parameters: dict[str, ToolParameterModel] = {}
    implementation_type: ImplementationType
    http_config: HTTPToolConfigModel | None = None
    mcp_config: MCPToolConfigModel | None = None
    python_config: PythonToolConfigModel | None = None
    workflow_config: WorkflowToolConfigModel | None = None
    enabled: bool = True
    tags: list[str] = []


class CustomToolUpdateRequest(BaseModel):
    """Request model for updating a custom tool."""

    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    parameters: dict[str, ToolParameterModel] | None = None
    http_config: HTTPToolConfigModel | None = None
    mcp_config: MCPToolConfigModel | None = None
    python_config: PythonToolConfigModel | None = None
    workflow_config: WorkflowToolConfigModel | None = None
    enabled: bool | None = None
    tags: list[str] | None = None


class CustomToolTestRequest(BaseModel):
    """Request model for testing a custom tool."""

    params: dict[str, Any] = {}


class CustomToolListResponse(BaseModel):
    """Response model for custom tools list."""

    tools: list[CustomToolResponse]
    total: int


def tool_parameters_to_core(
    parameters: dict[str, ToolParameterModel],
) -> dict[str, ToolParameter]:
    """Convert API parameter models to core custom-tool parameters."""
    return {
        name: ToolParameter(
            type=param.type,
            description=param.description,
            required=param.required,
            default=param.default,
            enum=param.enum,
        )
        for name, param in parameters.items()
    }


def http_config_to_core(config: HTTPToolConfigModel) -> HTTPToolConfig:
    """Convert an API HTTP config model to the core config model."""
    return HTTPToolConfig(
        method=config.method,
        url=config.url,
        headers=config.headers,
        body_template=config.body_template,
        query_params=config.query_params,
        timeout_seconds=config.timeout_seconds,
        response_path=config.response_path,
        response_format=config.response_format,
    )


def mcp_config_to_core(config: MCPToolConfigModel) -> MCPToolConfig:
    """Convert an API MCP config model to the core config model."""
    return MCPToolConfig(
        server_command=config.server_command,
        server_args=config.server_args,
        tool_name=config.tool_name,
        env_vars=config.env_vars,
        working_directory=config.working_directory,
        idle_timeout_seconds=config.idle_timeout_seconds,
        startup_timeout_seconds=config.startup_timeout_seconds,
    )


def python_config_to_core(config: PythonToolConfigModel) -> PythonToolConfig:
    """Convert an API Python config model to the core config model."""
    return PythonToolConfig(
        source_code=config.source_code,
        entrypoint=config.entrypoint,
        runtime=config.runtime,
    )


def workflow_config_to_core(
    config: WorkflowToolConfigModel,
    *,
    actor_user_id: str,
    created_by: str = "",
) -> tuple[WorkflowToolConfig, dict[str, ToolParameter]]:
    """Build a validated, ADMIN-APPROVED core workflow config from a request.

    The REST create/update surfaces are admin-gated, so the actor's save
    self-approves the revision (the tool_create admin-draft rule). Static
    validation failures map to 400; the derived parameters are returned so the
    caller can install them on the definition (parameters are never
    client-declared for workflows).
    """
    from ...core.workflows.authoring import approve_revision, validate_workflow_static

    try:
        core = WorkflowToolConfig(
            source_code=config.source_code,
            entrypoint=config.entrypoint,
            continuations=list(config.continuations),
            wall_clock_seconds=config.wall_clock_seconds,
            max_calls=config.max_calls,
            max_ai_calls=config.max_ai_calls,
            created_by=created_by or actor_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid workflow_config: {exc}") from exc
    parameters, errors = validate_workflow_static(config=core)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    core = approve_revision(core, parameters, approved_by=actor_user_id)
    return core, parameters


def build_custom_tool_definition(
    request: CustomToolCreateRequest,
    *,
    actor_user_id: str = "",
) -> CustomToolDefinition:
    """Build a core ``CustomToolDefinition`` from a create request.

    Shared by the ``/tools/custom`` and ``/tools/unified`` create handlers.
    Raises ``HTTPException(400)`` when the implementation-specific config block
    is missing, so both routers return the same 400. ``ValueError`` from the
    ``*_to_core`` converters or model construction is intentionally left to
    propagate: the ``/tools/custom`` handler maps it to a 400, the unified
    handler does not, and that difference is preserved.

    ``request.parameters`` is always a dict (the schema defaults it to ``{}``),
    so this matches the prior ``parameters or {}`` and bare ``parameters`` call
    sites identically. Workflow tools are the exception: their parameters are
    derived from the entrypoint signature and client-declared ones are a 400;
    the (admin) actor's save self-approves the revision.
    """
    http_config = None
    mcp_config = None
    python_config = None
    workflow_config = None
    parameters = tool_parameters_to_core(request.parameters)
    if request.implementation_type == "http":
        if not request.http_config:
            raise HTTPException(
                status_code=400,
                detail="http_config is required for HTTP tools",
            )
        http_config = http_config_to_core(request.http_config)
    elif request.implementation_type == "mcp":
        if not request.mcp_config:
            raise HTTPException(
                status_code=400,
                detail="mcp_config is required for MCP tools",
            )
        mcp_config = mcp_config_to_core(request.mcp_config)
    elif request.implementation_type == "python":
        if not request.python_config:
            raise HTTPException(
                status_code=400,
                detail="python_config is required for Python tools",
            )
        from ...core.python_custom_tools import approve_python_revision

        python_config = python_config_to_core(request.python_config)
        # The (admin) actor's save self-approves the revision so the
        # execution-time gate admits it (mirrors the workflow branch).
        python_config = approve_python_revision(
            python_config, parameters, approved_by=actor_user_id
        )
    elif request.implementation_type == "workflow":
        if not request.workflow_config:
            raise HTTPException(
                status_code=400,
                detail="workflow_config is required for workflow tools",
            )
        if request.parameters:
            raise HTTPException(
                status_code=400,
                detail=(
                    "workflow parameters are derived from the entrypoint "
                    "signature; do not pass parameters"
                ),
            )
        workflow_config, parameters = workflow_config_to_core(
            request.workflow_config, actor_user_id=actor_user_id
        )

    definition = CustomToolDefinition(
        id=request.id,
        name=request.name,
        description=request.description,
        parameters=parameters,
        implementation_type=request.implementation_type,
        http_config=http_config,
        mcp_config=mcp_config,
        python_config=python_config,
        workflow_config=workflow_config,
        enabled=request.enabled,
        tags=request.tags,
    )
    # http/mcp: the (admin) actor's save self-approves the execution revision so
    # the gate admits it, mirroring the python and workflow branches above. The
    # stamp has to happen HERE rather than at save time and after the definition
    # exists, because the hash covers `parameters`, which is definition scope.
    from ...core.custom_tool_gate import stamp_custom_tool_approval

    return stamp_custom_tool_approval(definition, approved_by=actor_user_id)


def apply_custom_tool_update(
    definition: CustomToolDefinition,
    request: CustomToolUpdateRequest,
    *,
    actor_user_id: str = "",
) -> None:
    """Apply a partial update request to an existing custom-tool definition.

    Shared by the ``/tools/custom`` and ``/tools/unified`` update handlers. Only
    the config block matching the definition's ``implementation_type`` is
    replaced. Field assignments are independent, so the order here is immaterial
    to the resulting definition.

    Workflow updates replace the whole workflow_config (re-validated,
    re-derived parameters, re-approved by the admin actor while preserving the
    original author); client-declared parameters for a workflow are a 400.
    Python updates re-approve the edited revision under the admin actor,
    re-stamped even for a params-only edit (else the execution gate, which
    hashes the parameters, would fail closed). http/mcp updates get the same
    treatment for the same reason. Name/enabled/tags edits never touch the
    revision hash for ANY type, so they cannot reset an approval.
    """
    if request.name is not None:
        definition.name = request.name
    if request.description is not None:
        definition.description = request.description
    if request.parameters is not None:
        if definition.implementation_type == "workflow":
            raise HTTPException(
                status_code=400,
                detail=(
                    "workflow parameters are derived from the entrypoint "
                    "signature; do not pass parameters"
                ),
            )
        definition.parameters = tool_parameters_to_core(request.parameters)
    if request.http_config is not None and definition.implementation_type == "http":
        definition.http_config = http_config_to_core(request.http_config)
    if request.mcp_config is not None and definition.implementation_type == "mcp":
        definition.mcp_config = mcp_config_to_core(request.mcp_config)
    if definition.implementation_type == "python" and (
        request.python_config is not None or request.parameters is not None
    ):
        from ...core.python_custom_tools import approve_python_revision

        if request.python_config is not None:
            definition.python_config = python_config_to_core(request.python_config)
        if definition.python_config is not None:
            # Re-stamp against the FINAL parameters (applied above) so a
            # params-only edit does not silently fail the execution gate; the
            # admin actor's update re-approves the edited revision.
            definition.python_config = approve_python_revision(
                definition.python_config, definition.parameters, approved_by=actor_user_id
            )
    if request.workflow_config is not None and definition.implementation_type == "workflow":
        created_by = (
            definition.workflow_config.created_by if definition.workflow_config else ""
        )
        definition.workflow_config, definition.parameters = workflow_config_to_core(
            request.workflow_config,
            actor_user_id=actor_user_id,
            created_by=created_by,
        )
    if request.enabled is not None:
        definition.enabled = request.enabled
    if request.tags is not None:
        definition.tags = request.tags
    # LAST, and unlike its neighbours this one is order-dependent: it hashes the
    # definition as finally assembled.
    if definition.implementation_type in ("http", "mcp") and (
        request.http_config is not None
        or request.mcp_config is not None
        or request.parameters is not None
    ):
        from ...core.custom_tool_gate import stamp_custom_tool_approval

        # Re-stamp against the FINAL definition, exactly as the python branch
        # does and for the same reason: the hash covers `parameters`, so a
        # params-only edit would otherwise leave a legitimately updated tool
        # failing its own gate. Crucially this is the whole condition. A
        # name/description/enabled/tags edit falls through UNSTAMPED, so an
        # admin renaming a record that was planted on disk does not thereby
        # approve its launch command.
        stamp_custom_tool_approval(definition, approved_by=actor_user_id)


def custom_tool_definition_to_response(defn: CustomToolDefinition) -> CustomToolResponse:
    """Convert a CustomToolDefinition to API response format."""
    return CustomToolResponse(
        id=defn.id,
        name=defn.name,
        description=defn.description,
        parameters={
            name: ToolParameterModel(
                type=param.type,
                description=param.description,
                required=param.required,
                default=str(param.default) if param.default is not None else None,
                enum=param.enum,
            )
            for name, param in defn.parameters.items()
        },
        implementation_type=defn.implementation_type,
        http_config=HTTPToolConfigModel(
            method=defn.http_config.method,
            url=defn.http_config.url,
            headers=defn.http_config.headers,
            body_template=defn.http_config.body_template,
            query_params=defn.http_config.query_params,
            timeout_seconds=defn.http_config.timeout_seconds,
            response_path=defn.http_config.response_path,
            response_format=defn.http_config.response_format,
        ) if defn.http_config else None,
        mcp_config=MCPToolConfigModel(
            server_command=defn.mcp_config.server_command,
            server_args=defn.mcp_config.server_args,
            tool_name=defn.mcp_config.tool_name,
            env_vars=defn.mcp_config.env_vars,
            working_directory=defn.mcp_config.working_directory,
            idle_timeout_seconds=defn.mcp_config.idle_timeout_seconds,
            startup_timeout_seconds=defn.mcp_config.startup_timeout_seconds,
        ) if defn.mcp_config else None,
        python_config=PythonToolConfigModel(
            source_code=defn.python_config.source_code,
            entrypoint=defn.python_config.entrypoint,
            runtime=defn.python_config.runtime,
        ) if defn.python_config else None,
        workflow_config=_workflow_config_to_response(defn) if defn.workflow_config else None,
        enabled=defn.enabled,
        tags=defn.tags,
        created_at=defn.created_at,
        updated_at=defn.updated_at,
    )


def _workflow_config_to_response(
    defn: CustomToolDefinition,
) -> Optional[WorkflowToolConfigResponseModel]:
    config = defn.workflow_config
    if config is None:
        return None
    from ...core.workflows.authoring import approval_state

    return WorkflowToolConfigResponseModel(
        source_code=config.source_code,
        entrypoint=config.entrypoint,
        continuations=list(config.continuations),
        wall_clock_seconds=config.wall_clock_seconds,
        max_calls=config.max_calls,
        max_ai_calls=config.max_ai_calls,
        revision_hash=config.revision_hash,
        approval=approval_state(config, defn.parameters),
        approved_revision=config.approved_revision,
        approved_by=config.approved_by,
        approved_at=config.approved_at,
        declined_by=config.declined_by,
        declined_at=config.declined_at,
        decline_note=config.decline_note,
        created_by=config.created_by,
    )
