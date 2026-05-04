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
from typing import Annotated, Any, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError

from ..config import get_settings
from ..core.custom_tools import execute_http_tool, get_custom_tool_loader
from ..core.http_policy import SECRET_PATTERNS, SENSITIVE_HEADER_NAMES
from ..core.time_utils import utc_now
from ..core.tool_reload import tool_reload_command
from .definitions.custom_tool_schema import CustomToolDefinition, HTTPToolConfig, ToolParameter
from .tool_search import DEFAULT_TTL, TTL_PRESETS, _enable
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)

TOOL_CREATE_VERSION = "2026-04-30.1"
TOOL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
PARAM_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
ENV_SECRET_PATTERN = re.compile(r"\$\{env:", re.IGNORECASE)


class HTTPToolDraft(BaseModel):
    """Persisted draft for an agent-created HTTP tool."""

    draft_id: str
    tool_id: str
    name: str
    description: str
    parameters: dict[str, ToolParameter] = Field(default_factory=dict)
    http_config: HTTPToolConfig
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
            "parameter_names": sorted(self.parameters.keys()),
            "method": self.http_config.method,
            "url": self.http_config.url,
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

    for header_name in http_config.headers:
        if header_name.strip().lower() in SENSITIVE_HEADER_NAMES:
            errors.append(
                f"header {header_name!r} is not allowed in agent-created tools yet; "
                "secret-scoped auth will be added later"
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

    return HTTPToolDraft(
        draft_id=normalized_draft_id,
        tool_id=normalized_tool_id,
        name=clean_name[:64],
        description=clean_description[:1000],
        parameters=_coerce_parameters(parameters),
        http_config=_coerce_http_config(http_config),
        created_by_user_id=user_id,
    )


async def test_draft(store: ToolDraftStore, user_id: str, draft_id: str, sample_params: Optional[dict[str, Any]]) -> dict[str, Any]:
    draft = store.get(user_id, _normalize_draft_id(draft_id))
    if draft is None:
        raise ValueError(f"Draft not found: {draft_id}")

    params = sample_params or {}
    if not isinstance(params, dict):
        raise ValueError("sample_params must be an object")

    response = await execute_http_tool(draft.http_config, params)
    ok = not response.startswith("[Error]:")
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
) -> Union[str, Command]:
    from ..core.agent import get_current_agent

    normalized_draft_id = _normalize_draft_id(draft_id)
    draft = store.get(user_id, normalized_draft_id)
    if draft is None:
        return _json_result(ok=False, error={"type": "not_found", "message": f"Draft not found: {draft_id}"})
    if draft.last_test_ok is not True:
        return _json_result(
            ok=False,
            error={
                "type": "untested_draft",
                "message": "Run action='test' successfully before publishing this draft.",
            },
        )

    ttl_key = (ttl or DEFAULT_TTL).strip().lower()
    if ttl_key not in TTL_PRESETS:
        options = ", ".join(TTL_PRESETS.keys())
        return _json_result(
            ok=False,
            error={"type": "validation_error", "message": f"Invalid ttl. Use one of: {options}"},
        )

    agent = get_current_agent()
    if _global_definition_exists(draft.tool_id, agent):
        return _json_result(
            ok=False,
            error={"type": "duplicate_tool_id", "message": f"tool_id {draft.tool_id!r} already exists"},
        )

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
    loader = get_custom_tool_loader()
    path = loader.save_definition(definition)

    loaded_names: list[str] = []
    if agent is not None:
        loaded_names = agent.reload_custom_tools()
    else:
        loaded_names = [tool.name for tool in loader.load_all()]

    publish_text = _json_result(
        ok=True,
        action="publish",
        published=True,
        global_registry=True,
        default_enabled_for_other_threads=False,
        saved_path=str(path),
        loaded_tools=loaded_names,
        tool=_published_summary(definition),
    )

    if agent is None:
        return publish_text

    enable_result = _enable(
        [draft.tool_id],
        "",
        thread_id,
        user_id,
        ttl=ttl_key,
        tool_call_id=tool_call_id,
        source="tool_create",
        reason="tool_published",
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
    draft_id: str = "",
    sample_params: Optional[dict[str, Any]] = None,
    ttl: str = DEFAULT_TTL,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> Union[str, Command]:
    """Draft, test, publish, list, or delete agent-created HTTP tools.

    Use this after you have discovered a stable API request with api_discover
    and tested the payload shape with http_request. V1 only supports HTTP
    tools and intentionally rejects inline secrets, Authorization/API-key
    headers, and ${env:...} secret references. Secret-scoped auth is planned
    for production.

    Actions:
      draft:   Save or update a per-user HTTP tool draft. Requires tool_id,
               description, parameters, and http_config.
      test:    Execute a saved draft with sample_params and record whether it
               succeeded.
      publish: Save a successfully tested draft into the global custom-tool
               registry, reload it, and enable it on this thread with ttl.
      list:    Show this user's drafts and globally published custom tools
               without exposing request headers or bodies.
      delete:  Delete this user's draft only. It does not delete a globally
               published tool.

    Published tools are globally discoverable via tool_search but are not
    added to default_thread_tools and do not affect other users unless they
    explicitly enable them.
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
                draft_id=draft_id,
                agent=get_current_agent(),
            )
            store.save(user_id, draft)
            return _json_result(ok=True, action="draft", draft=draft.public_summary())

        if action_key == "test":
            target_draft_id = _normalize_draft_id(draft_id or tool_id)
            result = await test_draft(store, user_id, target_draft_id, sample_params)
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
