"""Portable thread configuration export/import helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional, Set

from pydantic import ValidationError

from .thread_config import ThreadConfig, ThreadLLMConfig

THREAD_SHARE_KIND = "nymeria.thread.share"
THREAD_SHARE_VERSION = 1

PORTABLE_CONFIG_FIELDS = {
    "instructions",
    "disabled_tools",
    "enabled_tools",
    "enabled_skills",
    "disabled_skills",
    "llm_config",
    "system_prompt",
    "callable",
    "callable_name",
    "callable_description",
    "callable_max_iterations",
    "inject_profile_in_prompt",
    "inject_todos_in_prompt",
    "show_autonomous_prompts",
    "show_prompt_metadata",
    "telegram_autonomous_delivery",
    "in_app_notification_level",
}

OMITTED_FIELDS = [
    "messages",
    "notepad",
    "attachments",
    "checkpoints",
    "todos",
    "triggers",
    "chat_app_bindings",
    "temporary_tools",
    "llm_config.api_key",
]

LLM_CONFIG_FIELDS = {
    "provider",
    "model",
    "temperature",
    "max_tokens",
    "extended_thinking",
    "reasoning_effort",
    "use_model_defaults",
    "provider_route",
    "openai_api_mode",
    "base_url",
    "context_length",
    "ollama_num_ctx",
}

CALLABLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ThreadShareError(ValueError):
    """Raised when a share document is structurally invalid."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name and name not in seen:
            out.append(name)
            seen.add(name)
    return out


def _portable_llm_config(llm_config: Optional[ThreadLLMConfig]) -> Optional[dict[str, Any]]:
    if llm_config is None:
        return None
    data = llm_config.model_dump(mode="json", exclude_none=True)
    data.pop("api_key", None)
    return {k: v for k, v in data.items() if k in LLM_CONFIG_FIELDS}


def build_thread_share_document(
    *,
    thread_id: str,
    title: str,
    config: Optional[ThreadConfig],
) -> dict[str, Any]:
    """Build a versioned, shareable JSON document for a thread config."""

    tc = config or ThreadConfig(thread_id=thread_id)
    raw = tc.model_dump(mode="json")
    portable: dict[str, Any] = {
        key: raw.get(key)
        for key in PORTABLE_CONFIG_FIELDS
        if key in raw and key != "llm_config"
    }
    portable["llm_config"] = _portable_llm_config(tc.llm_config)

    return {
        "kind": THREAD_SHARE_KIND,
        "version": THREAD_SHARE_VERSION,
        "exported_at": _now_iso(),
        "source": {
            "thread_id": thread_id,
            "title": title or "New Chat",
        },
        "title": title or "New Chat",
        "config": portable,
        "omitted": list(OMITTED_FIELDS),
    }


def _safe_callable_base(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in value.strip())
    safe = "_".join(part for part in safe.split("_") if part)
    safe = safe.strip("_-")
    if not safe:
        safe = "ImportedThread"
    if safe[0].isdigit():
        safe = f"Imported_{safe}"
    return safe[:56] or "ImportedThread"


def _dedupe_callable_name(
    desired: str,
    unavailable_names: Set[str],
) -> str:
    base = _safe_callable_base(desired)
    candidate = base[:64]
    if CALLABLE_NAME_RE.match(candidate) and candidate not in unavailable_names:
        return candidate
    for i in range(2, 1000):
        suffix = f"_{i}"
        candidate = f"{base[:64 - len(suffix)]}{suffix}"
        if CALLABLE_NAME_RE.match(candidate) and candidate not in unavailable_names:
            return candidate
    raise ThreadShareError("Could not derive a unique callable name")


def validate_share_document(document: dict[str, Any]) -> dict[str, Any]:
    """Validate top-level share document shape and return the config payload."""

    if not isinstance(document, dict):
        raise ThreadShareError("Import file must be a JSON object")
    if document.get("kind") != THREAD_SHARE_KIND:
        raise ThreadShareError(f"Unsupported import kind: {document.get('kind')!r}")
    if document.get("version") != THREAD_SHARE_VERSION:
        raise ThreadShareError(f"Unsupported import version: {document.get('version')!r}")
    config = document.get("config")
    if not isinstance(config, dict):
        raise ThreadShareError("Import file is missing a config object")
    return config


def sanitize_import_config(
    *,
    document: dict[str, Any],
    new_thread_id: str,
    importer_role: str,
    available_tool_names: Set[str],
    unavailable_enabled_tool_names: Set[str],
    available_skill_names: Set[str],
    unavailable_callable_names: Set[str],
) -> tuple[ThreadConfig, list[str]]:
    """Return a validated ThreadConfig plus warnings for dropped references."""

    raw_config = validate_share_document(document)
    warnings: list[str] = []

    ignored = sorted(set(raw_config) - PORTABLE_CONFIG_FIELDS)
    for field in ignored:
        warnings.append(f"Ignored unsupported config field: {field}")

    data: dict[str, Any] = {"thread_id": new_thread_id}
    for field in PORTABLE_CONFIG_FIELDS - {"llm_config"}:
        if field in raw_config:
            data[field] = raw_config[field]

    enabled_tools = _coerce_string_list(raw_config.get("enabled_tools"))
    disabled_tools = _coerce_string_list(raw_config.get("disabled_tools"))

    kept_enabled: list[str] = []
    for name in enabled_tools:
        if name not in available_tool_names:
            warnings.append(f"Dropped unavailable enabled tool: {name}")
            continue
        if name in unavailable_enabled_tool_names and importer_role != "admin":
            warnings.append(f"Dropped admin-only enabled tool: {name}")
            continue
        kept_enabled.append(name)

    kept_disabled: list[str] = []
    for name in disabled_tools:
        if name not in available_tool_names:
            warnings.append(f"Dropped unavailable disabled tool: {name}")
            continue
        kept_disabled.append(name)

    data["enabled_tools"] = kept_enabled
    data["disabled_tools"] = kept_disabled

    for skill_field in ("enabled_skills", "disabled_skills"):
        kept_skills: list[str] = []
        for name in _coerce_string_list(raw_config.get(skill_field)):
            if name in available_skill_names:
                kept_skills.append(name)
            else:
                warnings.append(f"Dropped unavailable {skill_field[:-1]}: {name}")
        data[skill_field] = kept_skills

    raw_llm = raw_config.get("llm_config")
    if isinstance(raw_llm, dict):
        if raw_llm.get("api_key"):
            warnings.append("Ignored llm_config.api_key from import file")
        llm_data = {k: v for k, v in raw_llm.items() if k in LLM_CONFIG_FIELDS}
        unknown_llm = sorted(set(raw_llm) - LLM_CONFIG_FIELDS - {"api_key"})
        for field in unknown_llm:
            warnings.append(f"Ignored unsupported llm_config field: {field}")
        data["llm_config"] = llm_data or None
    elif raw_llm is not None:
        warnings.append("Ignored invalid llm_config value")
        data["llm_config"] = None

    omitted = document.get("omitted") if isinstance(document.get("omitted"), list) else []
    if "llm_config.api_key" in omitted and data.get("llm_config"):
        warnings.append("Per-thread API key was omitted; re-enter it if this model override needs one")

    if data.get("callable"):
        desired = str(data.get("callable_name") or document.get("title") or "ImportedThread")
        if not CALLABLE_NAME_RE.match(desired) or desired in unavailable_callable_names:
            replacement = _dedupe_callable_name(desired, unavailable_callable_names)
            warnings.append(f"Callable name changed from {desired!r} to {replacement!r}")
            data["callable_name"] = replacement
        else:
            data["callable_name"] = desired
    else:
        data["callable"] = False
        data["callable_name"] = None
        data["callable_description"] = None
        data["callable_max_iterations"] = None

    try:
        return ThreadConfig.model_validate(data), warnings
    except ValidationError as e:
        raise ThreadShareError(str(e)) from e
