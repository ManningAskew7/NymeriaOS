"""Agent-facing custom tool creation workflow.

Supports HTTP, Python, and nym-SDK workflow custom tools. Drafts are scoped
per user; published definitions go into the global custom-tool registry but
are not enabled by default for other users or threads.

Workflow tools follow the per-revision approval model (plan:
docs/private/plans/workflow-tools.md "Authoring gate"): ANY user may draft
(authoring is inert), but executing a revision (test, publish, run) requires
an admin-approved revision hash. Admin saves self-approve; non-admin saves
create a pending request announced to admins. The approval service functions
(``list_pending_workflows`` / ``resolve_workflow_approval``) live here so the
REST router and the ``workflow_info`` tool share one implementation.
"""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError

from ..config import get_settings
from ..core.custom_tools import execute_http_tool, get_custom_tool_loader
from ..core.http_policy import SECRET_PATTERNS, SENSITIVE_HEADER_NAMES
from ..core.python_custom_tools import (
    DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    approve_python_revision,
    clamp_validation_timeout,
    validate_python_tool_runtime,
)
from ..core.time_utils import utc_now
from ..core.time_utils import parse_tool_ttl
from ..core.tool_reload import tool_reload_command
from ..core.workflows.authoring import (
    approval_state,
    approve_revision,
    budget_from_config,
    config_revision_hash,
    decline_revision,
    retain_source_revision,
    stamp_revision,
    validate_workflow_static,
    workflow_execution_gate,
)
from .definitions.custom_tool_schema import (
    CustomToolDefinition,
    HTTPToolConfig,
    PythonToolConfig,
    ToolParameter,
    WorkflowToolConfig,
)
from .tool_search import DEFAULT_TTL, _enable
from .utils import get_thread_id, get_user_id, versioned_json_result

logger = logging.getLogger(__name__)

TOOL_CREATE_VERSION = "2026-04-30.1"
TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
PARAM_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
ENV_SECRET_PATTERN = re.compile(r"\$\{env:", re.IGNORECASE)
CREDENTIAL_REF_PATTERN = re.compile(
    r"\$\{credential:[A-Za-z][A-Za-z0-9_-]{2,127}\.[A-Za-z][A-Za-z0-9_-]{0,63}\}"
)


class HTTPToolDraft(BaseModel):
    """Persisted draft for an agent-created HTTP, Python, or workflow tool."""

    draft_id: str
    tool_id: str
    name: str
    description: str
    parameters: dict[str, ToolParameter] = Field(default_factory=dict)
    implementation_type: Literal["http", "python", "workflow"] = "http"
    http_config: Optional[HTTPToolConfig] = None
    python_config: Optional[PythonToolConfig] = None
    workflow_config: Optional[WorkflowToolConfig] = None
    created_by_user_id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_test_ok: Optional[bool] = None
    last_tested_at: Optional[datetime] = None
    last_test_params: dict[str, Any] = Field(default_factory=dict)
    last_test_response_preview: Optional[str] = None
    last_test_error: Optional[str] = None

    def public_summary(self) -> dict[str, Any]:
        entrypoint = None
        if self.python_config:
            entrypoint = self.python_config.entrypoint
        elif self.workflow_config:
            entrypoint = self.workflow_config.entrypoint
        summary = {
            "draft_id": self.draft_id,
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "implementation_type": self.implementation_type,
            "parameter_names": sorted(self.parameters.keys()),
            "method": self.http_config.method if self.http_config else None,
            "url": self.http_config.url if self.http_config else None,
            "entrypoint": entrypoint,
            "last_test_ok": self.last_test_ok,
            "last_tested_at": self.last_tested_at.isoformat() if self.last_tested_at else None,
            "updated_at": self.updated_at.isoformat(),
        }
        if self.workflow_config is not None:
            summary["revision"] = self.workflow_config.revision_hash[:12]
            summary["approval"] = approval_state(self.workflow_config, self.parameters)
            summary["continuations"] = list(self.workflow_config.continuations)
            if self.workflow_config.decline_note:
                summary["decline_note"] = self.workflow_config.decline_note
        return summary


class ToolDraftStore:
    """Filesystem store for per-user tool drafts."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _safe_segment(self, value: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
        return safe.strip("._") or "default"

    def _user_dir(self, user_id: str) -> Path:
        path = self.base_dir / self._safe_segment(user_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _draft_path(self, user_id: str, draft_id: str) -> Path:
        return self._user_dir(user_id) / f"{self._safe_segment(draft_id)}.json"

    def save(self, user_id: str, draft: HTTPToolDraft) -> Path:
        draft.updated_at = utc_now()
        path = self._draft_path(user_id, draft.draft_id)
        path.write_text(draft.model_dump_json(indent=2), encoding="utf-8")
        return path

    def get(self, user_id: str, draft_id: str) -> Optional[HTTPToolDraft]:
        path = self._draft_path(user_id, draft_id)
        if not path.exists():
            return None
        return HTTPToolDraft.model_validate_json(path.read_text(encoding="utf-8"))

    def delete(self, user_id: str, draft_id: str) -> bool:
        path = self._draft_path(user_id, draft_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list(self, user_id: str) -> list[HTTPToolDraft]:
        drafts: list[HTTPToolDraft] = []
        user_dir = self._user_dir(user_id)
        for path in sorted(user_dir.glob("*.json")):
            try:
                drafts.append(HTTPToolDraft.model_validate_json(path.read_text(encoding="utf-8")))
            except Exception as exc:
                logger.warning("Skipping invalid tool draft %s: %s", path, exc)
        return drafts


def _draft_store() -> ToolDraftStore:
    return ToolDraftStore(get_settings().data_dir / "tool_drafts")


class WorkflowApprovalRequired(ValueError):
    """Raised when an unapproved workflow revision is asked to execute."""


class WorkflowRevisionMismatch(ValueError):
    """Raised when an approval targets a revision the content no longer matches."""


def _json_result(**payload: Any) -> str:
    return versioned_json_result(TOOL_CREATE_VERSION, **payload)


def _normalize_tool_id(tool_id: str) -> str:
    normalized = (tool_id or "").strip().lower()
    if not TOOL_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "tool_id must be 3-64 chars, start with a lowercase letter, "
            "and contain only lowercase letters, numbers, and underscores"
        )
    return normalized


def _normalize_draft_id(draft_id: str, tool_id: str = "") -> str:
    candidate = (draft_id or tool_id or "").strip().lower()
    return _normalize_tool_id(candidate)


def _coerce_parameters(raw: Optional[dict[str, Any]]) -> dict[str, ToolParameter]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("parameters must be an object keyed by parameter name")

    parameters: dict[str, ToolParameter] = {}
    for name, value in raw.items():
        if not PARAM_NAME_PATTERN.fullmatch(str(name)):
            raise ValueError(
                f"Invalid parameter name {name!r}; use letters, numbers, and underscores, "
                "starting with a letter or underscore"
            )
        try:
            parameters[str(name)] = (
                value if isinstance(value, ToolParameter) else ToolParameter.model_validate(value)
            )
        except ValidationError as exc:
            raise ValueError(f"Invalid schema for parameter {name!r}: {exc}") from exc
    return parameters


def _iter_strings(value: Any, label: str) -> list[tuple[str, str]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [(label, value)]
    if isinstance(value, dict):
        strings: list[tuple[str, str]] = []
        for key, item in value.items():
            strings.extend(_iter_strings(item, f"{label}.{key}"))
        return strings
    if isinstance(value, list):
        strings = []
        for index, item in enumerate(value):
            strings.extend(_iter_strings(item, f"{label}[{index}]"))
        return strings
    return []


def _validate_no_inline_secrets(http_config: HTTPToolConfig) -> None:
    """Reject raw secrets and secret interpolation for agent-created V1 tools."""
    errors: list[str] = []

    for header_name, header_value in http_config.headers.items():
        if header_name.strip().lower() in SENSITIVE_HEADER_NAMES:
            if not CREDENTIAL_REF_PATTERN.search(str(header_value)):
                errors.append(
                    f"header {header_name!r} must use a credential-vault reference "
                    "like ${credential:cred_id.value}; raw secret headers are not allowed"
                )

    strings: list[tuple[str, str]] = []
    strings.extend(_iter_strings(http_config.url, "url"))
    strings.extend(_iter_strings(http_config.headers, "headers"))
    strings.extend(_iter_strings(http_config.query_params, "query_params"))
    strings.extend(_iter_strings(http_config.body_template, "body_template"))

    for label, value in strings:
        if ENV_SECRET_PATTERN.search(value):
            errors.append(f"{label} contains an env secret reference, which is disabled for V1")
        for pattern in SECRET_PATTERNS:
            if pattern.search(value):
                errors.append(f"{label} appears to contain a raw secret")
                break

    if errors:
        raise ValueError("; ".join(errors))


def _coerce_http_config(raw: Optional[dict[str, Any]]) -> HTTPToolConfig:
    if not isinstance(raw, dict):
        raise ValueError("http_config is required and must be an object")
    try:
        config = HTTPToolConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid http_config: {exc}") from exc
    _validate_no_inline_secrets(config)
    return config


def _coerce_python_config(python_code: str, entrypoint: str = "run") -> PythonToolConfig:
    code = (python_code or "").strip()
    if not code:
        raise ValueError("python_code is required for Python tools")
    try:
        return PythonToolConfig(source_code=code, entrypoint=entrypoint or "run")
    except ValidationError as exc:
        raise ValueError(f"Invalid python_config: {exc}") from exc


_WORKFLOW_BUDGET_KEYS = ("wall_clock_seconds", "max_calls", "max_ai_calls")


def _coerce_workflow_config(
    python_code: str,
    entrypoint: str = "run",
    continuations: Optional[list[Any]] = None,
    budget: Optional[dict[str, Any]] = None,
) -> WorkflowToolConfig:
    code = (python_code or "").strip()
    if not code:
        raise ValueError("python_code is required for workflow tools (the nym-SDK source)")
    overrides: dict[str, Any] = {}
    if budget is not None:
        if not isinstance(budget, dict):
            raise ValueError("budget must be an object")
        unknown = set(budget) - set(_WORKFLOW_BUDGET_KEYS)
        if unknown:
            raise ValueError(
                f"unknown budget key(s): {', '.join(sorted(map(str, unknown)))}; "
                f"allowed: {', '.join(_WORKFLOW_BUDGET_KEYS)}"
            )
        overrides = {k: budget[k] for k in _WORKFLOW_BUDGET_KEYS if budget.get(k) is not None}
    try:
        return WorkflowToolConfig(
            source_code=code,
            entrypoint=entrypoint or "run",
            continuations=[str(c) for c in (continuations or [])],
            **overrides,
        )
    except ValidationError as exc:
        raise ValueError(f"Invalid workflow_config: {exc}") from exc


def _reserved_tool_names(agent: Any = None) -> set[str]:
    from . import SEED_TOOLS, CATALOG_TOOLS

    names = {t.name for t in SEED_TOOLS}
    names.update(CATALOG_TOOLS.keys())
    if agent is not None and getattr(agent, "tool_registry", None):
        try:
            names.update(agent.tool_registry.list_tools())
        except Exception:
            logger.debug("Failed to query tool registry for reserved names")
    return names


def _global_definition_exists(tool_id: str, agent: Any = None) -> bool:
    settings = get_settings()
    if (settings.custom_tools_dir / f"{tool_id}.json").exists():
        return True
    loader = get_custom_tool_loader()
    if loader.get_definition(tool_id):
        return True
    return tool_id in _reserved_tool_names(agent)


def create_draft_definition(
    *,
    user_id: str,
    tool_id: str,
    name: str,
    description: str,
    parameters: Optional[dict[str, Any]],
    http_config: Optional[dict[str, Any]],
    implementation_type: str = "http",
    python_code: str = "",
    entrypoint: str = "run",
    continuations: Optional[list[Any]] = None,
    budget: Optional[dict[str, Any]] = None,
    draft_id: str = "",
    agent: Any = None,
) -> HTTPToolDraft:
    """Validate and build a draft object. Exposed for focused tests."""
    normalized_tool_id = _normalize_tool_id(tool_id)
    normalized_draft_id = _normalize_draft_id(draft_id, normalized_tool_id)

    if _global_definition_exists(normalized_tool_id, agent):
        raise ValueError(f"tool_id {normalized_tool_id!r} already exists")

    clean_name = (name or normalized_tool_id).strip()
    clean_description = (description or "").strip()
    if not clean_name:
        raise ValueError("name is required")
    if not clean_description:
        raise ValueError("description is required")
    impl = (implementation_type or "http").strip().lower()
    if impl not in {"http", "python", "workflow"}:
        raise ValueError("implementation_type must be 'http', 'python', or 'workflow'")

    clean_http_config = None
    clean_python_config = None
    clean_workflow_config = None
    if impl == "workflow":
        # Workflow parameters are DERIVED from the entrypoint signature
        # (generation replaces cross-validation); a declared map would drift.
        if parameters:
            raise ValueError(
                "workflow parameters are derived from the entrypoint signature; "
                "do not pass parameters"
            )
        clean_workflow_config = _coerce_workflow_config(
            python_code, entrypoint, continuations, budget
        ).model_copy(update={"created_by": user_id})
        derived, errors = validate_workflow_static(config=clean_workflow_config)
        if errors:
            raise ValueError("; ".join(errors))
        clean_parameters = derived
        clean_workflow_config = stamp_revision(clean_workflow_config, derived)
    elif impl == "http":
        clean_parameters = _coerce_parameters(parameters)
        clean_http_config = _coerce_http_config(http_config)
    else:
        clean_parameters = _coerce_parameters(parameters)
        clean_python_config = _coerce_python_config(python_code, entrypoint)

    return HTTPToolDraft(
        draft_id=normalized_draft_id,
        tool_id=normalized_tool_id,
        name=clean_name[:64],
        description=clean_description[:1000],
        parameters=clean_parameters,
        implementation_type=impl,
        http_config=clean_http_config,
        python_config=clean_python_config,
        workflow_config=clean_workflow_config,
        created_by_user_id=user_id,
    )


async def test_draft(
    store: ToolDraftStore,
    user_id: str,
    draft_id: str,
    sample_params: Optional[dict[str, Any]],
    validation_timeout_seconds: Optional[int] = None,
    thread_id: str = "",
) -> dict[str, Any]:
    draft = store.get(user_id, _normalize_draft_id(draft_id))
    if draft is None:
        raise ValueError(f"Draft not found: {draft_id}")

    params = sample_params or {}
    if not isinstance(params, dict):
        raise ValueError("sample_params must be an object")

    if draft.implementation_type == "http":
        if draft.http_config is None:
            raise ValueError("Draft is missing http_config")
        # Same target string the tool will resolve under once published:
        # ``publish`` sets ``definition.id = draft.tool_id``. Testing a draft is
        # a real execution with real credentials, so it has to sit behind the
        # same vault gate as the published tool, or "draft it and test it"
        # becomes a way to reach a credential the published tool could not.
        response = await execute_http_tool(
            draft.http_config,
            params,
            actor=user_id,
            target_type="custom_tool",
            target_id=draft.tool_id,
        )
        ok = not response.startswith("[Error]:")
    elif draft.implementation_type == "python":
        if draft.python_config is None:
            raise ValueError("Draft is missing python_config")
        import asyncio

        run_result = await asyncio.to_thread(
            validate_python_tool_runtime,
            tool_id=draft.tool_id,
            config=draft.python_config,
            parameters=draft.parameters,
            sample_params=params,
            timeout_seconds=clamp_validation_timeout(validation_timeout_seconds),
        )
        ok = run_result.ok
        response = run_result.public_text()
    elif draft.implementation_type == "workflow":
        # A workflow test IS a real run of an approved revision: side effects
        # fire (there is no mock nym in v1), and the approval gate applies
        # exactly as it would to any other execution.
        if draft.workflow_config is None:
            raise ValueError("Draft is missing workflow_config")
        gate_error = workflow_execution_gate(draft.workflow_config, draft.parameters)
        if gate_error:
            raise WorkflowApprovalRequired(gate_error)
        missing = [
            name
            for name, parameter in draft.parameters.items()
            if parameter.required and name not in params
        ]
        if missing:
            raise ValueError(
                f"sample_params missing required parameter(s): {', '.join(sorted(missing))}"
            )
        from ..core.workflows.envelope import STATUS_NEEDS_APPROVAL
        from ..core.workflows.executor import execute_workflow
        from ..core.workflows.registry import ApprovalRuntime
        from ..core.workflows.tool_runtime import format_envelope_for_agent

        run = await execute_workflow(
            source=draft.workflow_config.source_code,
            entrypoint=draft.workflow_config.entrypoint,
            params=params,
            user_id=user_id,
            thread_id=thread_id,
            workflow_id=draft.tool_id,
            budget=budget_from_config(draft.workflow_config),
            approval=ApprovalRuntime(
                origin="draft",
                revision_hash=config_revision_hash(
                    draft.workflow_config, draft.parameters
                ),
                continuations=tuple(draft.workflow_config.continuations or []),
                owner_user_id=user_id,
            ),
        )
        # Reaching nym.approve IS the workflow behaving correctly, so a
        # suspended test counts as a pass (else a checkpoint workflow could
        # never satisfy the tested-before-publish rule).
        ok = run.envelope.ok or run.envelope.status == STATUS_NEEDS_APPROVAL
        response = format_envelope_for_agent(run.envelope)
    else:
        raise ValueError(f"Unsupported implementation_type: {draft.implementation_type}")

    draft.last_test_ok = ok
    draft.last_tested_at = utc_now()
    draft.last_test_params = params
    draft.last_test_response_preview = response[:4000]
    draft.last_test_error = None if ok else response[:1000]
    store.save(user_id, draft)

    return {
        "ok": ok,
        "draft": draft.public_summary(),
        "response_preview": response[:4000],
    }


def _published_summary(definition: CustomToolDefinition) -> dict[str, Any]:
    summary = {
        "tool_id": definition.id,
        "name": definition.name,
        "description": definition.description,
        "implementation_type": definition.implementation_type,
        "tags": definition.tags,
        "enabled": definition.enabled,
        "parameter_names": sorted(definition.parameters.keys()),
        "created_at": definition.created_at.isoformat(),
        "updated_at": definition.updated_at.isoformat(),
    }
    if definition.workflow_config is not None:
        summary["revision"] = definition.workflow_config.revision_hash[:12]
        summary["approval"] = approval_state(
            definition.workflow_config, definition.parameters
        )
    return summary


def _prefix_command_result(result: Union[str, Command], prefix: str, tool_call_id: str) -> Union[str, Command]:
    if isinstance(result, Command):
        old_content = ""
        try:
            messages = result.update.get("messages", []) if isinstance(result.update, dict) else []
            if messages:
                old_content = str(messages[0].content)
        except Exception:
            old_content = ""
        return tool_reload_command(f"{prefix}\n\n{old_content}".strip(), tool_call_id)
    return f"{prefix}\n\n{result}".strip()


def _publish_draft(
    *,
    store: ToolDraftStore,
    user_id: str,
    draft_id: str,
    thread_id: str,
    ttl: str,
    tool_call_id: str,
    sample_params: Optional[dict[str, Any]] = None,
    validation_timeout_seconds: Optional[int] = None,
    reload_source: str = "tool_create",
    reload_reason: str = "tool_published",
) -> Union[str, Command]:
    from ..core.agent import get_current_agent

    normalized_draft_id = _normalize_draft_id(draft_id)
    draft = store.get(user_id, normalized_draft_id)
    if draft is None:
        return _json_result(ok=False, error={"type": "not_found", "message": f"Draft not found: {draft_id}"})

    publish_params: Optional[dict[str, Any]] = draft.last_test_params
    if sample_params is not None:
        if not isinstance(sample_params, dict):
            return _json_result(
                ok=False,
                error={"type": "validation_error", "message": "sample_params must be an object"},
            )
        publish_params = sample_params

    if draft.last_test_ok is not True:
        # Workflows have no publish-with-sample_params shortcut: a workflow
        # test is a real side-effecting run the author should invoke
        # deliberately via action='test'.
        if draft.implementation_type == "python" and sample_params is not None:
            draft.last_test_params = sample_params
        else:
            return _json_result(
                ok=False,
                error={
                    "type": "untested_draft",
                    "message": "Run action='test' successfully before publishing this draft.",
                },
            )
    elif sample_params is not None:
        draft.last_test_params = sample_params

    if draft.implementation_type == "python" and publish_params is None:
        return _json_result(
            ok=False,
            error={
                "type": "untested_draft",
                "message": "Run action='test' successfully or provide sample_params when publishing this Python draft.",
            },
        )

    try:
        ttl_key, _ = parse_tool_ttl(DEFAULT_TTL if ttl is None else ttl)
    except ValueError as exc:
        return _json_result(
            ok=False,
            error={"type": "validation_error", "message": str(exc)},
        )

    agent = get_current_agent()
    if _global_definition_exists(draft.tool_id, agent):
        return _json_result(
            ok=False,
            error={"type": "duplicate_tool_id", "message": f"tool_id {draft.tool_id!r} already exists"},
        )

    if draft.implementation_type == "python":
        if draft.python_config is None:
            return _json_result(
                ok=False,
                error={"type": "validation_error", "message": "Draft is missing python_config"},
            )
        validation = validate_python_tool_runtime(
            tool_id=draft.tool_id,
            config=draft.python_config,
            parameters=draft.parameters,
            sample_params=publish_params,
            timeout_seconds=clamp_validation_timeout(validation_timeout_seconds),
        )
        draft.last_test_ok = validation.ok
        draft.last_tested_at = utc_now()
        draft.last_test_response_preview = validation.public_text()[:4000]
        draft.last_test_error = None if validation.ok else validation.public_text()[:1000]
        store.save(user_id, draft)
        if not validation.ok:
            return _json_result(
                ok=False,
                error={
                    "type": validation.error_type or "validation_error",
                    "message": validation.error_message or validation.public_text(),
                },
            )
        # Stamp admin approval on the validated revision so the execution-time
        # gate (python_execution_gate) admits it. Admin is guaranteed upstream
        # (the dispatch gate blocks non-admin python publish), so a direct file
        # write of a definition cannot produce a valid approval without an admin.
        draft.python_config = approve_python_revision(
            draft.python_config, draft.parameters, approved_by=user_id
        )
        store.save(user_id, draft)

    if draft.implementation_type == "workflow":
        if draft.workflow_config is None:
            return _json_result(
                ok=False,
                error={"type": "validation_error", "message": "Draft is missing workflow_config"},
            )
        # The publish-time gate mirrors test/run: an unapproved or edited
        # revision cannot become a live tool. The approval fields carry over
        # verbatim, so the published definition stays executable.
        gate_error = workflow_execution_gate(draft.workflow_config, draft.parameters)
        if gate_error:
            return _json_result(
                ok=False,
                error={"type": "approval_required", "message": gate_error},
            )

    if draft.implementation_type == "http":
        definition = CustomToolDefinition(
            id=draft.tool_id,
            name=draft.name,
            description=draft.description,
            parameters=draft.parameters,
            implementation_type="http",
            http_config=draft.http_config,
            enabled=True,
            tags=["agent-created", f"user:{user_id}"],
        )
    elif draft.implementation_type == "python":
        definition = CustomToolDefinition(
            id=draft.tool_id,
            name=draft.name,
            description=draft.description,
            parameters=draft.parameters,
            implementation_type="python",
            python_config=draft.python_config,
            enabled=True,
            tags=["agent-created", "python", f"user:{user_id}"],
        )
    elif draft.implementation_type == "workflow":
        definition = CustomToolDefinition(
            id=draft.tool_id,
            name=draft.name,
            description=draft.description,
            parameters=draft.parameters,
            implementation_type="workflow",
            workflow_config=draft.workflow_config,
            enabled=True,
            tags=["agent-created", "workflow", f"user:{user_id}"],
        )
    else:
        return _json_result(
            ok=False,
            error={"type": "validation_error", "message": f"Unsupported implementation_type: {draft.implementation_type}"},
        )
    loader = get_custom_tool_loader()
    registry_before = 0
    if agent is not None:
        try:
            registry_before = len(agent.tool_registry.get_all_tools())
        except Exception:
            registry_before = 0
    path = loader.save_definition(definition)
    if definition.implementation_type == "workflow" and definition.workflow_config:
        retain_source_revision(
            definition.id,
            definition.workflow_config.revision_hash,
            definition.workflow_config.source_code,
        )

    loaded_names: list[str] = []
    if agent is not None:
        loaded_names = agent.reload_custom_tools()
    else:
        loaded_names = [tool.name for tool in loader.load_all()]
    registry_after = registry_before
    if agent is not None:
        try:
            registry_after = len(agent.tool_registry.get_all_tools())
        except Exception:
            registry_after = registry_before

    publish_text = _json_result(
        ok=True,
        action="publish",
        published=True,
        global_registry=True,
        default_enabled_for_other_threads=False,
        saved_path=str(path),
        loaded_tools=loaded_names,
        registry_before_count=registry_before,
        registry_after_count=registry_after,
        tool=_published_summary(definition),
    )

    if agent is None:
        return publish_text

    enable_reason = (
        "python_tool_published"
        if draft.implementation_type == "python" and reload_reason == "tool_published"
        else reload_reason
    )
    enable_result = _enable(
        [draft.tool_id],
        "",
        thread_id,
        user_id,
        ttl=ttl_key,
        tool_call_id=tool_call_id,
        source=reload_source,
        reason=enable_reason,
    )
    return _prefix_command_result(enable_result, publish_text, tool_call_id)


def _user_is_admin(user_id: str) -> bool:
    """Return True only if ``user_id`` resolves to an active admin account.

    Python custom tools execute code in a child process that inherits the
    API's OS user, full environment (DB URI, vault key, OAuth tokens, service
    token), and network, so creating/testing/publishing them is restricted to
    admins -- matching the admin-gated REST custom-tool endpoints. Resolution
    failures fail closed (deny). The single-operator bootstrap ``default`` user
    is provisioned as admin, so solo installs are unaffected.
    """
    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        repo = getattr(agent, "accounts_repo", None) if agent is not None else None
        if repo is None:
            return False
        user = repo.get_user_by_id(user_id)
        return bool(user is not None and getattr(user, "role", None) == "admin")
    except Exception:
        logger.warning(
            "tool_create: could not resolve role for user %r; denying Python path",
            user_id,
            exc_info=True,
        )
        return False


def _active_admin_user_ids() -> list[str]:
    """All enabled admin account ids (shared helper; kept as the patch seam)."""
    from ..core.notifications import active_admin_user_ids

    return active_admin_user_ids()


def _notify_user(user_id: str, summary: str, thread_id: str = "") -> None:
    """Best-effort in-app notification (the hooks notify-action idiom)."""
    try:
        from ..core.notifications import create_notification

        create_notification(
            user_id=user_id,
            summary=summary[:200],
            thread_id=thread_id,
            task_id=None,
        )
    except Exception:  # noqa: BLE001 - notification failure never blocks authoring
        logger.warning("workflow approval notification failed", exc_info=True)


def _announce_workflow_approval_request(draft: HTTPToolDraft, author_user_id: str) -> None:
    """Tell every active admin a workflow revision awaits review."""
    if draft.workflow_config is None:
        return
    revision = draft.workflow_config.revision_hash[:12]
    summary = (
        f"Workflow '{draft.tool_id}' by {author_user_id} awaits approval "
        f"(revision {revision}). Review via GET /workflows/pending."
    )
    for admin_id in _active_admin_user_ids():
        _notify_user(admin_id, summary)


def _definition_on_disk(tool_id: str) -> Optional[CustomToolDefinition]:
    """One global tool definition read straight from disk, INCLUDING disabled.

    The loader cache deliberately drops disabled definitions
    (``CustomToolLoader._load_tool_file`` returns before caching them), so a
    cache read cannot tell "installed but disabled" from "absent" while the file
    check still sees the file. Matching on the definition's own ``id`` rather
    than the filename also survives a tool stored under a non-conventional file
    name. Mirrors ``_iter_workflow_definitions``' disk read.
    """
    import json as _json

    try:
        paths = sorted(get_settings().custom_tools_dir.glob("*.json"))
    except OSError:
        return None
    for path in paths:
        try:
            definition = CustomToolDefinition(
                **_json.loads(path.read_text(encoding="utf-8"))
            )
        except Exception:  # noqa: BLE001 - skip invalid/corrupt definitions
            continue
        if definition.id == tool_id:
            return definition
    return None


def install_workflow_template(
    *,
    user_id: str,
    template_id: str,
    agent: Any = None,
    is_admin: Optional[bool] = None,
) -> tuple[Optional[CustomToolDefinition], bool]:
    """Publish a bundled workflow template as an approved global workflow tool.

    Admin-gated: unlike a per-user hook-template install, publishing an
    approved GLOBAL workflow tool is an admin action (the workflow trust model
    gates execution on admin approval). Idempotent by tool id: if this exact
    bundled revision is already installed it returns ``(existing, False)``
    unchanged (including when it is installed-but-disabled, which the caller can
    see on the returned definition). It NEVER overwrites: an id held by a
    different tool, or by a DIVERGENT copy of this template (edited, planted, or
    installed from an older release), raises a conflict naming which case it is.
    Identity is verified against the shipped revision hash, not a self-asserted
    tag, since the custom-tools dir is an agent-writable resource surface. On a
    fresh install it self-approves the trusted bundled revision (admins approve
    their own saves), saves it to the global registry, and hot-loads it,
    returning ``(definition, True)``. The check-then-save is best-effort (not a
    lock), matching the existing publish path. Raises ``ValueError`` for a
    non-admin caller, an unknown template, an id conflict, or (defensively) a
    bundled file that fails static validation.

    Enabling the new tool on a thread is the caller's job (the tool surface does
    it via ``_enable``); this function only publishes.
    """
    from ..core.agent import get_current_agent
    from ..core.workflow_templates import get_template as _get_wf_template
    from ..core.workflow_templates import load_templates as _load_wf_templates

    if agent is None:
        agent = get_current_agent()
    admin = _user_is_admin(user_id) if is_admin is None else bool(is_admin)
    if not admin:
        raise ValueError(
            "Installing a bundled workflow publishes an approved global workflow "
            "tool, which requires an admin. Ask an admin to install it, or draft "
            "your own with tool_create (implementation_type='workflow')."
        )

    template = _get_wf_template(template_id)
    if template is None:
        known = ", ".join(t.id for t in _load_wf_templates()) or "(none)"
        raise ValueError(
            f"Unknown workflow template '{template_id}'. Available: {known}."
        )

    workflow = template.workflow or {}
    config = _coerce_workflow_config(
        str(workflow.get("source_code") or ""),
        str(workflow.get("entrypoint") or "run"),
        list(workflow.get("continuations") or []),
        workflow.get("budget") or None,
    ).model_copy(update={"created_by": user_id})
    derived, errors = validate_workflow_static(config=config)
    if errors:
        # A shipped bundled file validates at load; reaching here is a
        # packaging bug, surfaced rather than silently publishing a broken tool.
        raise ValueError("; ".join(errors))
    config = stamp_revision(config, derived)

    loader = get_custom_tool_loader()
    if _global_definition_exists(template.id, agent):
        # Read from disk, not the loader cache: the cache hides disabled
        # definitions, so a cache read would call an admin's own disabled
        # template a foreign tool. (The check-then-save is best-effort, not a
        # lock.)
        existing = _definition_on_disk(template.id)
        installed_config = existing.workflow_config if existing is not None else None
        if existing is not None and f"template:{template.id}" in (existing.tags or []):
            # RECOMPUTE the installed revision from its own content rather than
            # reading its stored revision_hash, exactly as workflow_execution_gate
            # does: on an agent-writable dir the stored hash is as self-asserted
            # as the tag, so a planted file could otherwise carry the shipped
            # hash over foreign source and be waved through as "already
            # installed".
            if (
                installed_config is not None
                and config_revision_hash(installed_config, existing.parameters)
                == config.revision_hash
            ):
                # Idempotent: this exact bundled revision is already installed.
                return existing, False
            # Tagged as ours but the source does not match what we ship. Never
            # publish over it, and never call it "already installed" either: an
            # admin who believes the vetted recipe is live would be the one
            # approving the divergent revision in the pending queue.
            raise ValueError(
                f"tool id '{template.id}' holds a DIVERGENT copy of this bundled "
                "template: its source does not match the shipped recipe (edited "
                "in place, installed from an older release, or planted). "
                "Refusing to overwrite it. Review it with workflow_info "
                "(action='show'), then delete it to install the shipped version."
            )
        # The id is occupied by a different tool, a corrupt file, or a builtin
        # name; refuse with a clear conflict rather than a misleading "already
        # installed".
        raise ValueError(
            f"tool id '{template.id}' is already in use by another tool; "
            "cannot install this bundled template (remove or rename the "
            "conflicting tool first)."
        )

    config = approve_revision(config, derived, approved_by=user_id)

    definition = CustomToolDefinition(
        id=template.id,
        name=(template.name or template.id)[:64],
        description=(template.description or template.id)[:1000],
        parameters=derived,
        implementation_type="workflow",
        workflow_config=config,
        enabled=True,
        tags=["bundled", "workflow", f"template:{template.id}"],
    )
    loader.save_definition(definition)
    retain_source_revision(definition.id, config.revision_hash, config.source_code)
    if agent is not None:
        agent.reload_custom_tools()
    else:
        loader.load_all()
    return definition, True


# --- workflow approval service (shared by the REST router and workflow_info) --


def list_all_workflow_drafts() -> list[HTTPToolDraft]:
    """Every user's workflow drafts (admin surfaces only)."""
    store = _draft_store()
    drafts: list[HTTPToolDraft] = []
    try:
        user_dirs = sorted(p for p in store.base_dir.iterdir() if p.is_dir())
    except OSError:
        return []
    for user_dir in user_dirs:
        for path in sorted(user_dir.glob("*.json")):
            try:
                draft = HTTPToolDraft.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - skip invalid drafts, matching store.list
                continue
            if draft.implementation_type == "workflow" and draft.workflow_config is not None:
                drafts.append(draft)
    return drafts


def _iter_workflow_definitions() -> list[CustomToolDefinition]:
    """Workflow definitions read from disk (includes disabled ones)."""
    definitions: list[CustomToolDefinition] = []
    try:
        import json as _json

        for path in sorted(get_settings().custom_tools_dir.glob("*.json")):
            try:
                definition = CustomToolDefinition(
                    **_json.loads(path.read_text(encoding="utf-8"))
                )
            except Exception:  # noqa: BLE001 - skip invalid definitions
                continue
            if (
                definition.implementation_type == "workflow"
                and definition.workflow_config is not None
            ):
                definitions.append(definition)
    except OSError:
        return definitions
    return definitions


def _pending_entry(
    *,
    kind: str,
    owner_user_id: str,
    target_id: str,
    tool_id: str,
    name: str,
    config: WorkflowToolConfig,
    parameters: dict[str, ToolParameter],
    updated_at: datetime,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "owner_user_id": owner_user_id,
        "id": target_id,
        "tool_id": tool_id,
        "name": name,
        "author": config.created_by or owner_user_id,
        "entrypoint": config.entrypoint,
        "continuations": list(config.continuations),
        "revision": config_revision_hash(config, parameters),
        "parameter_names": sorted(parameters.keys()),
        "updated_at": updated_at.isoformat(),
    }


def list_pending_workflows() -> list[dict[str, Any]]:
    """Workflow drafts and published definitions awaiting approval."""
    pending: list[dict[str, Any]] = []
    for draft in list_all_workflow_drafts():
        config = draft.workflow_config
        if config is None or approval_state(config, draft.parameters) != "pending":
            continue
        pending.append(
            _pending_entry(
                kind="draft",
                owner_user_id=draft.created_by_user_id,
                target_id=draft.draft_id,
                tool_id=draft.tool_id,
                name=draft.name,
                config=config,
                parameters=draft.parameters,
                updated_at=draft.updated_at,
            )
        )
    for definition in _iter_workflow_definitions():
        config = definition.workflow_config
        if config is None or approval_state(config, definition.parameters) != "pending":
            continue
        pending.append(
            _pending_entry(
                kind="tool",
                owner_user_id=config.created_by or "",
                target_id=definition.id,
                tool_id=definition.id,
                name=definition.name,
                config=config,
                parameters=definition.parameters,
                updated_at=definition.updated_at,
            )
        )
    return pending


def get_workflow_source(kind: str, owner_user_id: str, target_id: str) -> Optional[dict[str, Any]]:
    """Source and metadata for one pending target (the admin review view)."""
    if kind == "draft":
        draft = _draft_store().get(owner_user_id, _normalize_draft_id(target_id))
        if (
            draft is None
            or draft.implementation_type != "workflow"
            or draft.workflow_config is None
        ):
            return None
        entry = _pending_entry(
            kind="draft",
            owner_user_id=draft.created_by_user_id,
            target_id=draft.draft_id,
            tool_id=draft.tool_id,
            name=draft.name,
            config=draft.workflow_config,
            parameters=draft.parameters,
            updated_at=draft.updated_at,
        )
        entry["source_code"] = draft.workflow_config.source_code
        entry["approval"] = approval_state(draft.workflow_config, draft.parameters)
        return entry
    definition = next(
        (d for d in _iter_workflow_definitions() if d.id == target_id), None
    )
    if definition is None or definition.workflow_config is None:
        return None
    entry = _pending_entry(
        kind="tool",
        owner_user_id=definition.workflow_config.created_by or "",
        target_id=definition.id,
        tool_id=definition.id,
        name=definition.name,
        config=definition.workflow_config,
        parameters=definition.parameters,
        updated_at=definition.updated_at,
    )
    entry["source_code"] = definition.workflow_config.source_code
    entry["approval"] = approval_state(definition.workflow_config, definition.parameters)
    return entry


def _check_expected_revision(
    expected_revision: Optional[str],
    config: WorkflowToolConfig,
    parameters: dict[str, ToolParameter],
) -> None:
    """Refuse a resolution pinned to a revision the content no longer matches."""
    if not expected_revision:
        return
    current = config_revision_hash(config, parameters)
    if expected_revision != current:
        raise WorkflowRevisionMismatch(
            "workflow content changed since it was reviewed: current revision is "
            f"{current[:16]}; re-review the source before deciding"
        )


def resolve_workflow_approval(
    *,
    kind: str,
    owner_user_id: str,
    target_id: str,
    approve: bool,
    actor_user_id: str,
    note: Optional[str] = None,
    expected_revision: Optional[str] = None,
) -> dict[str, Any]:
    """Approve or decline one workflow revision (draft or published tool).

    The caller must have verified the actor is an admin; this function is the
    single state transition both surfaces share. Approving pins the hash
    recomputed from CONTENT (never the stored field) and retains the source
    revision for later diffing; either decision notifies the author.
    ``expected_revision`` (when given) pins the decision to the revision the
    admin actually reviewed: if the content on disk hashes differently, the
    resolution is refused with ``WorkflowRevisionMismatch``.
    Raises ValueError when the target does not exist.
    """
    decision = "approved" if approve else "declined"
    if kind == "draft":
        store = _draft_store()
        draft = store.get(owner_user_id, _normalize_draft_id(target_id))
        if (
            draft is None
            or draft.implementation_type != "workflow"
            or draft.workflow_config is None
        ):
            raise ValueError(f"workflow draft not found: {owner_user_id}/{target_id}")
        _check_expected_revision(
            expected_revision, draft.workflow_config, draft.parameters
        )
        if approve:
            draft.workflow_config = approve_revision(
                draft.workflow_config, draft.parameters, approved_by=actor_user_id
            )
            retain_source_revision(
                draft.tool_id,
                draft.workflow_config.revision_hash,
                draft.workflow_config.source_code,
            )
        else:
            draft.workflow_config = decline_revision(
                draft.workflow_config,
                draft.parameters,
                declined_by=actor_user_id,
                note=note,
            )
        store.save(owner_user_id, draft)
        author = draft.workflow_config.created_by or draft.created_by_user_id
        _notify_user(
            author,
            f"Your workflow draft '{draft.tool_id}' was {decision} by {actor_user_id}"
            + (f": {note}" if note else ""),
        )
        return {"kind": "draft", "id": draft.draft_id, "decision": decision, "draft": draft.public_summary()}

    if kind != "tool":
        raise ValueError("kind must be 'draft' or 'tool'")
    loader = get_custom_tool_loader()
    definition = next(
        (d for d in _iter_workflow_definitions() if d.id == target_id), None
    )
    if definition is None or definition.workflow_config is None:
        raise ValueError(f"workflow tool not found: {target_id}")
    _check_expected_revision(
        expected_revision, definition.workflow_config, definition.parameters
    )
    if approve:
        definition.workflow_config = approve_revision(
            definition.workflow_config, definition.parameters, approved_by=actor_user_id
        )
        retain_source_revision(
            definition.id,
            definition.workflow_config.revision_hash,
            definition.workflow_config.source_code,
        )
    else:
        definition.workflow_config = decline_revision(
            definition.workflow_config,
            definition.parameters,
            declined_by=actor_user_id,
            note=note,
        )
    loader.save_definition(definition)
    author = definition.workflow_config.created_by
    if author:
        _notify_user(
            author,
            f"Your workflow tool '{definition.id}' was {decision} by {actor_user_id}"
            + (f": {note}" if note else ""),
        )
    return {
        "kind": "tool",
        "id": definition.id,
        "decision": decision,
        "tool": _published_summary(definition),
    }


def _python_admin_required_result() -> str:
    return _json_result(
        ok=False,
        error={
            "type": "admin_required",
            "message": (
                "Python custom tools run code inside the assistant's own "
                "process, so creating, testing, and publishing them is "
                "restricted to admin users. Use an HTTP custom tool instead, "
                "or ask an administrator to publish this Python tool."
            ),
        },
    )


@tool
async def tool_create(
    action: str,
    tool_id: str = "",
    name: str = "",
    description: str = "",
    parameters: Optional[dict[str, Any]] = None,
    http_config: Optional[dict[str, Any]] = None,
    implementation_type: str = "http",
    python_code: str = "",
    entrypoint: str = "run",
    continuations: Optional[list[str]] = None,
    budget: Optional[dict[str, Any]] = None,
    draft_id: str = "",
    template_id: str = "",
    sample_params: Optional[dict[str, Any]] = None,
    ttl: str = DEFAULT_TTL,
    validation_timeout_seconds: int = DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> Union[str, Command]:
    """Draft, test, publish, list, or delete agent-created custom tools.

    Supports HTTP tools, subprocess-backed Python tools, and nym-SDK workflow
    tools. HTTP tools are best after discovering a stable API request with
    api_discover/http_request. HTTP auth/secrets must use credential-vault
    references such as ${credential:cred_id.value}; raw secrets and
    ${env:...} references are rejected for agent-created tools. Plain public
    headers like Accept are OK.
    Python tools are for small deterministic helpers that can be expressed as
    a pure function. Python code is stored in data/custom_tools and executed in
    a child process. That child runs with the same OS user, environment, and
    network access as the API, so it is not a security sandbox; creating,
    testing, and publishing Python tools is therefore restricted to admins.
    Workflow tools (implementation_type="workflow") run python_code out of
    process against the nym.* SDK (nym.tools.<name>, nym.llm, nym.thread,
    nym.emit, nym.todo.add, nym.notify, nym.memory.*). Anyone may draft one,
    but EXECUTING a revision (test, publish, or run) requires that exact
    revision to be admin-approved: admin saves approve themselves; non-admin
    saves notify admins and wait. Editing the source, entrypoint, or
    continuations resets approval. Tool parameters are DERIVED from the
    entrypoint's type-hinted signature (str/int/float/bool/list/dict,
    Optional[...] or a default makes a parameter optional; docstring Args
    lines become parameter descriptions), so do not pass parameters for
    workflows. A workflow test is a REAL run: side effects fire.

    Actions:
      draft:   Save or update a per-user tool draft. HTTP requires http_config.
               Python requires implementation_type="python", python_code, and
               an entrypoint function (default "run"). Workflow requires
               implementation_type="workflow" and python_code (nym-SDK
               source); optional continuations and budget
               ({wall_clock_seconds, max_calls, max_ai_calls}).
      test:    Execute a saved draft with sample_params and record whether it
               succeeded. Workflow tests require an approved revision and
               really execute (side effects fire).
      publish: Save a successfully tested draft into the global custom-tool
               registry, rerun Python validation when applicable, reload it,
               and enable it on this thread with ttl. For Python drafts,
               providing sample_params on publish can validate and publish in
               one call. Workflow drafts must have a successful prior test.
      list:    Show this user's drafts and globally published custom tools
               without exposing request headers or bodies.
      delete:  Delete this user's draft only. It does not delete a globally
               published tool.
      install_template: Install a bundled workflow recipe (see
               workflow_info action='templates') as a published workflow tool
               and enable it on this thread. Admin-only: it publishes an
               approved GLOBAL tool. Idempotent by tool id: re-installing
               publishes nothing but still enables the recipe on this thread,
               so call it from any thread that needs it. Never overwrites an
               existing tool. Pass template_id.

    Published tools are globally discoverable via tool_search but are not
    added to default_thread_tools and do not affect other users unless they
    explicitly enable them.

    Args:
      http_config: HTTP request config. Supports ${param} placeholders in URL,
           headers, query params, and body_template. Sensitive headers
           (Authorization, X-API-Key, Cookie, etc.) must use
           ${credential:cred_id.field}; never pass raw secret strings.
      continuations: Workflow-only. Continuation entrypoint names for
           nym.approve resume; each must exist in python_code with the
           signature (state, decision).
      budget: Workflow-only. Budget overrides, e.g. {"wall_clock_seconds":
           900, "max_calls": 100, "max_ai_calls": 20}.
      ttl: Publish/install TTL for enabling the new tool on this thread. Format:
           Nm/Nh/Nd/Nw or "never"/"permanent". Default "2h".
      template_id: install_template-only. Id of the bundled workflow recipe to
           install (list them with workflow_info action='templates').
      validation_timeout_seconds: Python test/publish timeout. Default 60s.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    store = _draft_store()
    action_key = (action or "").strip().lower()

    try:
        if action_key == "draft":
            impl = (implementation_type or "http").strip().lower()
            if impl == "python" and not _user_is_admin(user_id):
                return _python_admin_required_result()

            from ..core.agent import get_current_agent

            draft = create_draft_definition(
                user_id=user_id,
                tool_id=tool_id,
                name=name,
                description=description,
                parameters=parameters,
                http_config=http_config,
                implementation_type=implementation_type,
                python_code=python_code,
                entrypoint=entrypoint,
                continuations=continuations,
                budget=budget,
                draft_id=draft_id,
                agent=get_current_agent(),
            )
            approval_note: Optional[str] = None
            if draft.implementation_type == "workflow" and draft.workflow_config is not None:
                if _user_is_admin(user_id):
                    # Admin saves self-approve (the trust model that already
                    # lets admins publish Python custom tools).
                    draft.workflow_config = approve_revision(
                        draft.workflow_config, draft.parameters, approved_by=user_id
                    )
                    approval_note = "revision auto-approved (admin author)"
                else:
                    approval_note = (
                        "revision awaits admin approval; admins were notified. "
                        "Testing and publishing are blocked until it is approved."
                    )
            store.save(user_id, draft)
            if (
                draft.implementation_type == "workflow"
                and draft.workflow_config is not None
                and draft.workflow_config.approved_revision
                != draft.workflow_config.revision_hash
            ):
                _announce_workflow_approval_request(draft, user_id)
            result_payload: dict[str, Any] = {"draft": draft.public_summary()}
            if approval_note:
                result_payload["approval_note"] = approval_note
            return _json_result(ok=True, action="draft", **result_payload)

        if action_key == "test":
            target_draft_id = _normalize_draft_id(draft_id or tool_id)
            existing = store.get(user_id, target_draft_id)
            if (
                existing is not None
                and existing.implementation_type == "python"
                and not _user_is_admin(user_id)
            ):
                return _python_admin_required_result()
            result = await test_draft(
                store,
                user_id,
                target_draft_id,
                sample_params,
                validation_timeout_seconds=validation_timeout_seconds,
                thread_id=thread_id,
            )
            return _json_result(action="test", **result)

        if action_key == "publish":
            target_draft_id = _normalize_draft_id(draft_id or tool_id)
            existing = store.get(user_id, target_draft_id)
            if (
                existing is not None
                and existing.implementation_type == "python"
                and not _user_is_admin(user_id)
            ):
                return _python_admin_required_result()
            return _publish_draft(
                store=store,
                user_id=user_id,
                draft_id=target_draft_id,
                thread_id=thread_id,
                ttl=ttl,
                tool_call_id=tool_call_id,
                sample_params=sample_params,
                validation_timeout_seconds=validation_timeout_seconds,
            )

        if action_key == "list":
            loader = get_custom_tool_loader()
            published = [
                _published_summary(defn)
                for defn in sorted(loader.get_all_definitions(), key=lambda item: item.id)
            ]
            return _json_result(
                ok=True,
                action="list",
                drafts=[draft.public_summary() for draft in store.list(user_id)],
                published=published,
            )

        if action_key == "delete":
            target_draft_id = _normalize_draft_id(draft_id or tool_id)
            deleted = store.delete(user_id, target_draft_id)
            return _json_result(
                ok=deleted,
                action="delete",
                deleted=deleted,
                draft_id=target_draft_id,
                error=None if deleted else {"type": "not_found", "message": f"Draft not found: {target_draft_id}"},
            )

        if action_key == "install_template":
            requested = (template_id or tool_id).strip()
            if not requested:
                return _json_result(
                    ok=False,
                    error={
                        "type": "validation_error",
                        "message": "install_template requires template_id (workflow_info action='templates' lists them).",
                    },
                )
            from ..core.agent import get_current_agent

            agent = get_current_agent()
            definition, created = install_workflow_template(
                user_id=user_id, template_id=requested, agent=agent
            )
            approval = (
                approval_state(definition.workflow_config, definition.parameters)
                if definition is not None and definition.workflow_config is not None
                else "unknown"
            )
            publish_text = _json_result(
                ok=True,
                action="install_template",
                created=created,
                already_installed=not created,
                template_id=definition.id if definition is not None else requested,
                approval=approval,
                tool=_published_summary(definition) if definition is not None else {"tool_id": requested},
            )
            # Enable on re-install too, not just on a fresh publish: an agent
            # calling install_template in a thread wants to USE the recipe
            # there, and the common case (already installed globally, new
            # thread) would otherwise return "already_installed" with no
            # binding. A definition disabled at the registry level has nothing
            # to bind, so it is left alone and reported as enabled: false.
            if agent is None or definition is None or not definition.enabled:
                return publish_text
            try:
                ttl_key, _ = parse_tool_ttl(DEFAULT_TTL if ttl is None else ttl)
            except ValueError:
                return publish_text
            enable_result = _enable(
                [definition.id],
                "",
                thread_id,
                user_id,
                ttl=ttl_key,
                tool_call_id=tool_call_id,
                source="tool_create",
                reason="workflow_template_installed",
            )
            return _prefix_command_result(enable_result, publish_text, tool_call_id)

        return _json_result(
            ok=False,
            error={
                "type": "validation_error",
                "message": "action must be one of: draft, test, publish, list, delete, install_template",
            },
        )
    except WorkflowApprovalRequired as exc:
        return _json_result(ok=False, error={"type": "approval_required", "message": str(exc)})
    except ValueError as exc:
        return _json_result(ok=False, error={"type": "validation_error", "message": str(exc)})
    except Exception as exc:
        logger.error("tool_create failed", exc_info=True)
        return _json_result(ok=False, error={"type": type(exc).__name__, "message": str(exc)})


TOOL_CREATE_TOOLS = [tool_create]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="tool_create", tools=tuple(TOOL_CREATE_TOOLS)))
