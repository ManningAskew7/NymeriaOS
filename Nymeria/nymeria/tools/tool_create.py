"""Agent-facing custom tool creation workflow.

V1 supports HTTP custom tools only. Drafts are scoped per user; published
definitions go into the global custom-tool registry but are not enabled by
default for other users or threads.
"""

from __future__ import annotations

import json
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
    clamp_validation_timeout,
    validate_python_tool_runtime,
)
from ..core.time_utils import utc_now
from ..core.time_utils import parse_tool_ttl
from ..core.tool_reload import tool_reload_command
from .definitions.custom_tool_schema import (
    CustomToolDefinition,
    HTTPToolConfig,
    PythonToolConfig,
    ToolParameter,
)
from .tool_search import DEFAULT_TTL, _enable
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)

TOOL_CREATE_VERSION = "2026-04-30.1"
TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
PARAM_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
ENV_SECRET_PATTERN = re.compile(r"\$\{env:", re.IGNORECASE)
CREDENTIAL_REF_PATTERN = re.compile(
    r"\$\{credential:[A-Za-z][A-Za-z0-9_-]{2,127}\.[A-Za-z][A-Za-z0-9_-]{0,63}\}"
)


class HTTPToolDraft(BaseModel):
    """Persisted draft for an agent-created HTTP or Python tool."""

    draft_id: str
    tool_id: str
    name: str
    description: str
    parameters: dict[str, ToolParameter] = Field(default_factory=dict)
    implementation_type: Literal["http", "python"] = "http"
    http_config: Optional[HTTPToolConfig] = None
    python_config: Optional[PythonToolConfig] = None
    created_by_user_id: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    last_test_ok: Optional[bool] = None
    last_tested_at: Optional[datetime] = None
    last_test_params: dict[str, Any] = Field(default_factory=dict)
    last_test_response_preview: Optional[str] = None
    last_test_error: Optional[str] = None

    def public_summary(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "tool_id": self.tool_id,
            "name": self.name,
            "description": self.description,
            "implementation_type": self.implementation_type,
            "parameter_names": sorted(self.parameters.keys()),
            "method": self.http_config.method if self.http_config else None,
            "url": self.http_config.url if self.http_config else None,
            "entrypoint": self.python_config.entrypoint if self.python_config else None,
            "last_test_ok": self.last_test_ok,
            "last_tested_at": self.last_tested_at.isoformat() if self.last_tested_at else None,
            "updated_at": self.updated_at.isoformat(),
        }


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


def _json_result(**payload: Any) -> str:
    return json.dumps({"tool_version": TOOL_CREATE_VERSION, **payload}, indent=2, default=str)


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


def _reserved_tool_names(agent: Any = None) -> set[str]:
    from . import ALL_TOOLS, OPTIONAL_TOOLS

    names = {t.name for t in ALL_TOOLS}
    names.update(OPTIONAL_TOOLS.keys())
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
    if impl not in {"http", "python"}:
        raise ValueError("implementation_type must be 'http' or 'python'")

    clean_parameters = _coerce_parameters(parameters)
    clean_http_config = None
    clean_python_config = None
    if impl == "http":
        clean_http_config = _coerce_http_config(http_config)
    else:
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
        created_by_user_id=user_id,
    )


async def test_draft(
    store: ToolDraftStore,
    user_id: str,
    draft_id: str,
    sample_params: Optional[dict[str, Any]],
    validation_timeout_seconds: Optional[int] = None,
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
        response = await execute_http_tool(draft.http_config, params)
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
    return {
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
    draft_id: str = "",
    sample_params: Optional[dict[str, Any]] = None,
    ttl: str = DEFAULT_TTL,
    validation_timeout_seconds: int = DEFAULT_VALIDATION_TIMEOUT_SECONDS,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> Union[str, Command]:
    """Draft, test, publish, list, or delete agent-created custom tools.

    Supports HTTP tools and subprocess-backed Python tools. HTTP tools are best
    after discovering a stable API request with api_discover/http_request.
    HTTP auth/secrets must use credential-vault references such as
    ${credential:cred_id.value}; raw secrets and ${env:...} references are
    rejected for agent-created tools. Plain public headers like Accept are OK.
    Python tools are for small deterministic helpers that can be expressed as
    a pure function. Python code is stored in data/custom_tools and executed in
    a child process; it is never imported into the API process.

    Actions:
      draft:   Save or update a per-user tool draft. HTTP requires http_config.
               Python requires implementation_type="python", python_code, and
               an entrypoint function (default "run").
      test:    Execute a saved draft with sample_params and record whether it
               succeeded.
      publish: Save a successfully tested draft into the global custom-tool
               registry, rerun Python validation when applicable, reload it,
               and enable it on this thread with ttl. For Python drafts,
               providing sample_params on publish can validate and publish in
               one call.
      list:    Show this user's drafts and globally published custom tools
               without exposing request headers or bodies.
      delete:  Delete this user's draft only. It does not delete a globally
               published tool.

    Published tools are globally discoverable via tool_search but are not
    added to default_thread_tools and do not affect other users unless they
    explicitly enable them.

    Args:
      http_config: HTTP request config. Supports ${param} placeholders in URL,
           headers, query params, and body_template. Sensitive headers
           (Authorization, X-API-Key, Cookie, etc.) must use
           ${credential:cred_id.field}; never pass raw secret strings.
      ttl: Publish-only TTL for enabling the new tool on this thread. Format:
           Nm/Nh/Nd/Nw or "never"/"permanent". Default "2h".
      validation_timeout_seconds: Python test/publish timeout. Default 60s.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    store = _draft_store()
    action_key = (action or "").strip().lower()

    try:
        if action_key == "draft":
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
                draft_id=draft_id,
                agent=get_current_agent(),
            )
            store.save(user_id, draft)
            return _json_result(ok=True, action="draft", draft=draft.public_summary())

        if action_key == "test":
            target_draft_id = _normalize_draft_id(draft_id or tool_id)
            result = await test_draft(
                store,
                user_id,
                target_draft_id,
                sample_params,
                validation_timeout_seconds=validation_timeout_seconds,
            )
            return _json_result(action="test", **result)

        if action_key == "publish":
            target_draft_id = _normalize_draft_id(draft_id or tool_id)
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

        return _json_result(
            ok=False,
            error={
                "type": "validation_error",
                "message": "action must be one of: draft, test, publish, list, delete",
            },
        )
    except ValueError as exc:
        return _json_result(ok=False, error={"type": "validation_error", "message": str(exc)})
    except Exception as exc:
        logger.error("tool_create failed", exc_info=True)
        return _json_result(ok=False, error={"type": type(exc).__name__, "message": str(exc)})


TOOL_CREATE_TOOLS = [tool_create]
