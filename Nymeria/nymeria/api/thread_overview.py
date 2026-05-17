"""Resolved per-thread overview read model for headers and dashboards."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.accounts import AuthenticatedUser
from ..core.checkpoint_status import (
    get_graph_state_revision,
    get_latest_checkpoint_revision,
    has_direct_checkpoint_revision_backend,
)
from ..core.thread_classification import classify_platform
from ..core.thread_config import ThreadConfig
from ..core.time_utils import ensure_aware_utc, utc_now
from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url

logger = logging.getLogger(__name__)

_HIGH_TOOL_COUNT_WARNING_THRESHOLD = 100
NATIVE_THREAD_PLATFORMS = {
    "discord",
    "telegram",
    "slack",
    "matrix",
    "whatsapp",
    "messenger",
    "instagram",
    "webex",
    "mattermost",
    "zulip",
    "rocketchat",
    "teams",
    "googlechat",
    "line",
    "signal",
    "trigger",
    "twitch",
}
CHATAPP_BINDING_PLATFORMS = (
    "telegram",
    "slack",
    "matrix",
    "whatsapp",
    "messenger",
    "instagram",
    "webex",
    "mattermost",
    "zulip",
    "rocketchat",
    "teams",
    "googlechat",
    "line",
    "signal",
)


def build_thread_overview(
    *,
    agent: Any,
    settings: Any,
    user: AuthenticatedUser,
    user_id: str,
    thread_id: str,
) -> dict[str, Any]:
    """Build a best-effort read model for a single thread.

    The route-level ownership check is intentionally outside this helper.
    Individual sections are isolated so a broken optional subsystem does not
    prevent the CLI header from rendering the rest of the status snapshot.
    """

    section_errors: dict[str, str] = {}

    def record_error(section: str, exc: BaseException) -> None:
        detail = str(exc) or exc.__class__.__name__
        section_errors[section] = detail
        logger.warning(
            "Failed to build thread overview section %s for %s: %s",
            section,
            thread_id,
            detail,
        )

    tc_saved: ThreadConfig | None = None
    try:
        manager = getattr(agent, "thread_config_manager", None)
        tc_saved = manager.get_config(thread_id) if manager is not None else None
    except Exception as exc:  # noqa: BLE001 - overview is best-effort.
        record_error("config", exc)

    tc = tc_saved or ThreadConfig(thread_id=thread_id)

    status = _status_defaults(thread_id)
    try:
        status = _status_section(agent, settings, thread_id)
    except Exception as exc:  # noqa: BLE001
        record_error("status", exc)

    context = _context_defaults(thread_id, settings)
    try:
        context = _context_section(agent, settings, thread_id)
    except Exception as exc:  # noqa: BLE001
        record_error("context", exc)

    recovery_sources = _recovery_sources(
        agent=agent,
        settings=settings,
        user_id=user_id,
        thread_id=thread_id,
        checkpoint_revision=status.get("revision"),
        record_error=record_error,
    )

    thread = _thread_defaults(thread_id)
    try:
        thread = _thread_section(
            agent=agent,
            user_id=user_id,
            thread_id=thread_id,
            tc=tc,
            tc_saved=tc_saved,
            recovery_sources=recovery_sources,
        )
    except Exception as exc:  # noqa: BLE001
        record_error("thread", exc)

    config_summary = _config_summary(tc, tc_saved=tc_saved)

    llm = _llm_defaults(settings, context)
    try:
        llm = _llm_section(agent, settings, thread_id, tc_saved)
    except Exception as exc:  # noqa: BLE001
        record_error("llm", exc)

    callable_section = _callable_defaults(tc)
    try:
        callable_section = _callable_section(agent, user_id, thread_id, tc)
    except Exception as exc:  # noqa: BLE001
        record_error("callable", exc)

    runtime: dict[str, Any] = {
        "effective_tool_names": set(),
        "effective_mcp_names": set(),
        "visible_callables": callable_section.get("visible_threads", []),
    }
    tools = _tools_defaults(tc)
    try:
        tools = _tools_section(
            agent=agent,
            user=user,
            user_id=user_id,
            thread_id=thread_id,
            tc=tc,
            visible_callables=runtime["visible_callables"],
            runtime=runtime,
        )
    except Exception as exc:  # noqa: BLE001
        record_error("tools", exc)

    mcp = _mcp_defaults()
    try:
        mcp = _mcp_section(settings, runtime.get("effective_mcp_names", set()))
    except Exception as exc:  # noqa: BLE001
        record_error("mcp", exc)

    skills = _skills_defaults(tc)
    try:
        skills = _skills_section(agent, user_id, tc)
    except Exception as exc:  # noqa: BLE001
        record_error("skills", exc)

    todos = _todos_defaults()
    try:
        todos = _todos_section(agent, settings, user_id, thread_id)
    except Exception as exc:  # noqa: BLE001
        record_error("todos", exc)

    triggers = _triggers_defaults()
    try:
        triggers = _triggers_section(agent, settings, user_id, thread_id)
    except Exception as exc:  # noqa: BLE001
        record_error("triggers", exc)

    chat_apps = _chat_apps_defaults(tc)
    try:
        chat_apps = _chat_apps_section(agent, settings, thread_id, tc)
    except Exception as exc:  # noqa: BLE001
        record_error("chat_apps", exc)

    return {
        "thread": thread,
        "status": status,
        "context": context,
        "config_summary": config_summary,
        "llm": llm,
        "callable": callable_section,
        "tools": tools,
        "mcp": mcp,
        "skills": skills,
        "todos": todos,
        "triggers": triggers,
        "chat_apps": chat_apps,
        "user": {
            "id": user.id,
            "display_name": user.display_name,
            "role": user.role,
        },
        "section_errors": section_errors,
    }


def _status_defaults(thread_id: str) -> dict[str, Any]:
    return {"thread_id": thread_id, "revision": None, "processing": False}


def _status_section(agent: Any, settings: Any, thread_id: str) -> dict[str, Any]:
    if has_direct_checkpoint_revision_backend(settings):
        revision = get_latest_checkpoint_revision(settings, thread_id)
    else:
        revision = get_graph_state_revision(agent, thread_id)
    return {
        "thread_id": thread_id,
        "revision": revision,
        "processing": _is_thread_processing(agent, thread_id),
    }


def _is_thread_processing(agent: Any, thread_id: str) -> bool:
    thread_locks = getattr(agent, "_thread_locks", None)
    if thread_locks is None:
        return False
    try:
        return thread_locks.get_lock_info(thread_id) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to inspect processing state for %s: %s", thread_id, exc)
        return False


def _context_defaults(thread_id: str, settings: Any) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "model": getattr(settings, "llm_model", None),
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cumulative_tokens": 0,
        "context_limit": None,
        "usage_percentage": 0,
        "compaction_count": 0,
        "last_compaction": None,
        "context_management": getattr(settings, "context_management", "none"),
    }


def _context_section(agent: Any, settings: Any, thread_id: str) -> dict[str, Any]:
    stats = getattr(agent, "get_context_stats")(thread_id)
    data = dict(stats or {})
    defaults = _context_defaults(thread_id, settings)
    defaults.update(data)
    defaults["thread_id"] = thread_id
    defaults.setdefault("context_management", getattr(settings, "context_management", "none"))
    return defaults


def _thread_defaults(thread_id: str) -> dict[str, Any]:
    return {
        "id": thread_id,
        "thread_id": thread_id,
        "title": "New Chat",
        "title_source": "default",
        "platform": classify_platform(thread_id),
        "platform_meta": None,
        "pinned": False,
        "callable": False,
        "recovered": False,
        "recovery_sources": [],
        "created_at": None,
        "updated_at": None,
    }


def _thread_section(
    *,
    agent: Any,
    user_id: str,
    thread_id: str,
    tc: ThreadConfig,
    tc_saved: ThreadConfig | None,
    recovery_sources: set[str],
) -> dict[str, Any]:
    metadata_manager = getattr(agent, "thread_metadata_manager", None)
    meta = (
        metadata_manager.get_thread(user_id, thread_id)
        if metadata_manager is not None
        else None
    )
    if meta is not None:
        payload = _model_dump(meta)
    else:
        payload = _thread_defaults(thread_id)

    payload["id"] = payload.get("id") or payload.get("thread_id") or thread_id
    payload["thread_id"] = thread_id
    payload["platform"] = _thread_platform(agent, thread_id, meta, tc_saved)
    is_callable = bool(tc_saved and tc.callable)
    payload["callable"] = is_callable
    if is_callable and tc.callable_name:
        payload["title"] = tc.callable_name
        payload["title_source"] = "callable"
    payload["recovered"] = meta is None and bool(recovery_sources)
    payload["recovery_sources"] = sorted(recovery_sources if meta is None else [])
    payload.setdefault("platform_meta", None)
    payload.setdefault("pinned", False)
    payload.setdefault("created_at", None)
    payload.setdefault("updated_at", None)
    payload.setdefault("title_source", "default")
    return payload


def _thread_platform(
    agent: Any,
    thread_id: str,
    meta: Any,
    tc_saved: ThreadConfig | None,
) -> str:
    platform = getattr(meta, "platform", None) or classify_platform(thread_id)
    repo = getattr(agent, "chat_bindings_repo", None)
    if repo is not None:
        try:
            for provider in CHATAPP_BINDING_PLATFORMS:
                if repo.lookup_thread_binding_by_thread(provider, thread_id):
                    return provider
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to inspect chat-app binding for %s: %s", thread_id, exc)
    native = classify_platform(thread_id)
    if native in NATIVE_THREAD_PLATFORMS:
        return native
    if platform in NATIVE_THREAD_PLATFORMS:
        return platform
    if tc_saved and tc_saved.callable:
        return "callable"
    if platform == "callable":
        return "desktop"
    return platform or "desktop"


def _recovery_sources(
    *,
    agent: Any,
    settings: Any,
    user_id: str,
    thread_id: str,
    checkpoint_revision: Any,
    record_error: Any,
) -> set[str]:
    sources: set[str] = set()
    if checkpoint_revision:
        sources.add("checkpoint")

    try:
        todo_manager = getattr(agent, "todo_manager", None)
        if todo_manager is None:
            from ..core.todo_manager import TodoManager

            todo_manager = TodoManager(settings.data_dir)
        todo_list = todo_manager.get_todos(user_id)
        for item in getattr(todo_list, "items", []):
            if getattr(item, "thread_id", None) != thread_id:
                continue
            sources.add("scheduled_todo" if getattr(item, "scheduled_for", None) else "todo")
    except Exception as exc:  # noqa: BLE001
        record_error("recovery_todos", exc)

    try:
        manager = getattr(agent, "trigger_manager", None)
        if manager is None:
            from ..core.trigger_manager import TriggerManager

            manager = TriggerManager(settings.data_dir)
        if any(t.thread_id == thread_id for t in manager.get_triggers(user_id)):
            sources.add("trigger")
    except Exception as exc:  # noqa: BLE001
        record_error("recovery_triggers", exc)

    repo = getattr(agent, "chat_bindings_repo", None)
    if repo is not None:
        try:
            if repo.list_thread_bindings(thread_id):
                sources.add("chat_binding")
        except Exception as exc:  # noqa: BLE001
            record_error("recovery_chat_apps", exc)
        try:
            ids = repo.list_bind_code_thread_ids_for_user(user_id)
            if thread_id in set(ids):
                sources.add("bind_code")
        except Exception:  # noqa: BLE001
            logger.debug("Bind-code thread recovery lookup failed", exc_info=True)
    return sources


def _config_summary(tc: ThreadConfig, *, tc_saved: ThreadConfig | None) -> dict[str, Any]:
    return {
        "has_customizations": bool(tc_saved and tc_saved.has_customizations()),
        "instructions_present": bool(tc.instructions),
        "instructions_char_count": len(tc.instructions or ""),
        "system_prompt_override_present": bool(tc.system_prompt),
        "system_prompt_override_char_count": len(tc.system_prompt or ""),
        "created_at": _iso(getattr(tc_saved, "created_at", None)),
        "updated_at": _iso(getattr(tc_saved, "updated_at", None)),
        "disabled_tool_count": len(tc.disabled_tools or []),
        "enabled_tool_count": len(tc.enabled_tools or []),
        "temporary_tool_count": len(tc.temporary_tools or {}),
        "enabled_skill_count": len(tc.enabled_skills or []),
        "disabled_skill_count": len(tc.disabled_skills or []),
        "callable": bool(tc.callable),
        "inject_profile_in_prompt": bool(tc.inject_profile_in_prompt),
        "inject_todos_in_prompt": bool(tc.inject_todos_in_prompt),
        "show_autonomous_prompts": bool(tc.show_autonomous_prompts),
        "show_prompt_metadata": bool(tc.show_prompt_metadata),
        "telegram_autonomous_delivery": tc.telegram_autonomous_delivery,
        "in_app_notification_level": tc.in_app_notification_level,
    }


def _llm_defaults(settings: Any, context: dict[str, Any]) -> dict[str, Any]:
    provider = str(getattr(settings, "llm_provider", "") or "")
    model = str(context.get("model") or getattr(settings, "llm_model", "") or "")
    mode = getattr(settings, "openai_api_mode", None)
    extended = bool(getattr(settings, "llm_extended_thinking", False))
    effort = getattr(settings, "llm_reasoning_effort", None)
    return {
        "provider": provider,
        "provider_label": _provider_label(provider, getattr(settings, "llm_base_url", None)),
        "model": model,
        "api_mode": mode,
        "api_mode_label": _api_mode_label(provider, mode),
        "thinking_label": _thinking_label(
            provider=provider,
            model=model,
            extended=extended,
            effort=effort,
        ),
        "extended_thinking": extended,
        "reasoning_effort": effort,
        "temperature": getattr(settings, "llm_temperature", None),
        "max_tokens": getattr(settings, "llm_max_tokens", None),
        "use_model_defaults": getattr(settings, "llm_use_model_defaults", None),
        "base_url_configured": bool(getattr(settings, "llm_base_url", None)),
        "api_key_configured": False,
        "overrides": {},
    }


def _llm_section(
    agent: Any,
    settings: Any,
    thread_id: str,
    tc_saved: ThreadConfig | None,
) -> dict[str, Any]:
    resolver = getattr(agent, "_get_llm_config_for_thread", None)
    effective = resolver(thread_id) if callable(resolver) else None
    if effective is None:
        data = _llm_defaults(settings, {"model": getattr(settings, "llm_model", "")})
    else:
        provider = str(getattr(effective, "provider", "") or "")
        model = str(getattr(effective, "model", "") or "")
        base_url = getattr(effective, "base_url", None)
        mode = getattr(effective, "openai_api_mode", None)
        extended = bool(getattr(effective, "extended_thinking", False))
        effort = getattr(effective, "reasoning_effort", None)
        data = {
            "provider": provider,
            "provider_label": _provider_label(provider, base_url),
            "model": model,
            "api_mode": mode,
            "api_mode_label": _api_mode_label(provider, mode),
            "thinking_label": _thinking_label(
                provider=provider,
                model=model,
                extended=extended,
                effort=effort,
            ),
            "extended_thinking": extended,
            "reasoning_effort": effort,
            "temperature": getattr(effective, "temperature", None),
            "max_tokens": getattr(effective, "max_tokens", None),
            "use_model_defaults": getattr(effective, "temperature", None) is None,
            "base_url_configured": bool(base_url),
            "api_key_configured": bool(getattr(effective, "api_key", None)),
        }

    overrides: dict[str, Any] = {}
    if tc_saved and tc_saved.llm_config:
        raw = tc_saved.llm_config.model_dump(exclude_none=True)
        overrides = {
            key: value
            for key, value in raw.items()
            if key not in {"api_key", "base_url"}
        }
        overrides["api_key_override_configured"] = bool(raw.get("api_key"))
        overrides["base_url_override_configured"] = "base_url" in raw
    data["overrides"] = overrides
    return data


def _provider_label(provider: str, base_url: Any) -> str:
    provider_text = str(provider or "").strip()
    normalized = provider_text.casefold()
    base = str(base_url or "").strip()
    if normalized == "anthropic":
        if base and looks_like_cliproxy_url(base):
            return "cliproxy OAuth"
        if base:
            return "custom Claude"
        return "Claude API"
    if normalized == "openai":
        if base and looks_like_cliproxy_url(base):
            return "OpenAI proxy"
        if base:
            return "OpenAI compat"
        return "OpenAI API"
    if normalized == "openrouter":
        return "OpenRouter"
    if normalized == "custom":
        return "Custom provider"
    return provider_text


def _api_mode_label(provider: str, mode: Any) -> str:
    normalized_provider = str(provider or "").strip().casefold()
    if normalized_provider == "anthropic":
        return "messages/v1"
    if normalized_provider in {"openai", "openrouter", "custom"}:
        normalized = str(mode or "responses").strip().casefold().replace("-", "_")
        if normalized == "chat_completions":
            return "chat completions/v1"
        if normalized == "responses":
            return "responses/v1"
        if normalized == "completions":
            return "completions/v1"
    return str(mode or "").strip()


def _thinking_label(
    *,
    provider: str,
    model: str,
    extended: bool,
    effort: Any = None,
) -> str:
    effort_text = str(effort or "").strip().casefold()
    if not extended and not effort_text:
        return "off"
    model_text = model.casefold()
    provider_text = provider.casefold()
    adaptive = (
        ("anthropic" in provider_text or "claude" in model_text)
        and any(
            marker in model_text
            for marker in ("opus-4-6", "sonnet-4-6", "opus-4-7", "sonnet-4-7")
        )
    )
    if adaptive:
        return f"adaptive ({effort_text})" if effort_text else "adaptive"
    return effort_text or "medium"


def _callable_defaults(tc: ThreadConfig) -> dict[str, Any]:
    return {
        "enabled": bool(tc.callable),
        "name": tc.callable_name,
        "description_present": bool(tc.callable_description),
        "description_char_count": len(tc.callable_description or ""),
        "max_iterations": tc.callable_max_iterations,
        "effective_max_iterations": None,
        "team_id": tc.callable_team_id,
        "team_name": tc.callable_team_name,
        "visible_thread_count": 0,
        "visible_threads": [],
    }


def _callable_section(
    agent: Any,
    user_id: str,
    thread_id: str,
    tc: ThreadConfig,
) -> dict[str, Any]:
    visible = _visible_callable_threads(agent, user_id, thread_id, tc)
    main_default = getattr(agent, "MAIN_AGENT_MAX_ITERATIONS", None)
    callable_default = getattr(agent, "CALLABLE_DEFAULT_MAX_ITERATIONS", None)
    effective = (
        (tc.callable_max_iterations or callable_default)
        if tc.callable
        else main_default
    )
    data = _callable_defaults(tc)
    data.update(
        {
            "effective_max_iterations": effective,
            "visible_thread_count": len(visible),
            "visible_threads": visible,
        }
    )
    return data


def _visible_callable_threads(
    agent: Any,
    user_id: str,
    thread_id: str,
    tc: ThreadConfig,
) -> list[dict[str, Any]]:
    get_scoped = getattr(agent, "_get_team_scoped_callable_threads", None)
    if not callable(get_scoped):
        return []
    disabled = set(tc.disabled_tools or [])
    own_name = tc.callable_name if tc.callable else None
    visible: list[dict[str, Any]] = []
    seen: set[str] = set()
    for callable_tc in get_scoped(user_id=user_id, caller_thread_id=thread_id):
        name = str(getattr(callable_tc, "callable_name", "") or "")
        if not name or getattr(callable_tc, "thread_id", "") == thread_id:
            continue
        if own_name and name == own_name:
            continue
        if name in disabled or name in seen:
            continue
        seen.add(name)
        visible.append(
            {
                "thread_id": callable_tc.thread_id,
                "name": name,
                "description_present": bool(callable_tc.callable_description),
                "description_char_count": len(callable_tc.callable_description or ""),
                "team_id": callable_tc.callable_team_id,
                "team_name": callable_tc.callable_team_name,
            }
        )
    return visible


def _tools_defaults(tc: ThreadConfig) -> dict[str, Any]:
    return {
        "default_tool_names": [],
        "core_tool_names": [tool.name for tool in ALL_TOOLS],
        "disabled_names": list(tc.disabled_tools or []),
        "enabled_optional_names": list(tc.enabled_tools or []),
        "live_temporary_tools": [],
        "mcp_override_count": 0,
        "effective_builtin_count": 0,
        "effective_mcp_count": 0,
        "effective_callable_count": 0,
        "total_effective_count": 0,
        "high_count_warning": False,
    }


def _tools_section(
    *,
    agent: Any,
    user: AuthenticatedUser,
    user_id: str,
    thread_id: str,
    tc: ThreadConfig,
    visible_callables: list[dict[str, Any]],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    default_names = _default_tool_names(agent, user_id)
    live_temp_names = _live_temporary_tool_names(agent, tc)
    live_temp_details = [
        {
            "name": name,
            "enabled_at": _iso(getattr(tc.temporary_tools[name], "enabled_at", None)),
            "expires_at": _iso(getattr(tc.temporary_tools[name], "expires_at", None)),
        }
        for name in sorted(live_temp_names)
        if name in tc.temporary_tools
    ]

    effective_names = _effective_tool_names(
        agent=agent,
        user_id=user_id,
        thread_id=thread_id,
        tc=tc,
        default_names=default_names,
        live_temp_names=live_temp_names,
        visible_callables=visible_callables,
    )
    callable_names = {str(item.get("name")) for item in visible_callables if item.get("name")}
    effective_mcp = {name for name in effective_names if name.startswith("mcp__")}
    effective_callable = effective_names & callable_names
    effective_builtin = effective_names - effective_mcp - effective_callable

    thread_overrides = set(tc.enabled_tools or []) | set(live_temp_names)
    mcp_override_count = len({name for name in thread_overrides if name.startswith("mcp__")})

    runtime["effective_tool_names"] = effective_names
    runtime["effective_mcp_names"] = effective_mcp

    total = len(effective_names)
    return {
        "default_tool_names": sorted(default_names),
        "core_tool_names": [tool.name for tool in ALL_TOOLS],
        "disabled_names": sorted(tc.disabled_tools or []),
        "enabled_optional_names": sorted(tc.enabled_tools or []),
        "live_temporary_tools": live_temp_details,
        "mcp_override_count": mcp_override_count,
        "effective_builtin_count": len(effective_builtin),
        "effective_mcp_count": len(effective_mcp),
        "effective_callable_count": len(effective_callable),
        "total_effective_count": total,
        "high_count_warning": total >= _HIGH_TOOL_COUNT_WARNING_THRESHOLD,
        "role": user.role,
    }


def _default_tool_names(agent: Any, user_id: str) -> set[str]:
    profile_manager = getattr(agent, "profile_manager", None)
    if profile_manager is None:
        return {tool.name for tool in ALL_TOOLS}
    profile = profile_manager.get_profile(user_id)
    prefs = getattr(profile, "tool_preferences", None)
    configured = getattr(prefs, "default_thread_tools", None)
    if configured is None:
        return {tool.name for tool in ALL_TOOLS}
    return {str(name) for name in configured if str(name)}


def _live_temporary_tool_names(agent: Any, tc: ThreadConfig) -> set[str]:
    resolver = getattr(agent, "_resolve_temporary_tools", None)
    if callable(resolver):
        return set(resolver(tc))
    now = utc_now()
    live = set()
    for name, entry in (tc.temporary_tools or {}).items():
        try:
            if ensure_aware_utc(entry.expires_at) > now:
                live.add(name)
        except Exception:
            continue
    return live


def _effective_tool_names(
    *,
    agent: Any,
    user_id: str,
    thread_id: str,
    tc: ThreadConfig,
    default_names: set[str],
    live_temp_names: set[str],
    visible_callables: list[dict[str, Any]],
) -> set[str]:
    selector = getattr(agent, "_select_tools_for_graph", None)
    if callable(selector):
        tools, _ = selector(user_id, thread_id)
        return {str(getattr(tool, "name", "") or "") for tool in tools if getattr(tool, "name", "")}

    all_tools = {tool.name for tool in ALL_TOOLS} | set(OPTIONAL_TOOLS)
    registry = getattr(agent, "tool_registry", None)
    registry_names = {
        str(tool.get("name") or "")
        for tool in (registry.list_tools() if registry is not None else [])
        if isinstance(tool, dict)
    }
    names = set(default_names) | set(tc.enabled_tools or []) | set(live_temp_names)
    names |= {str(item.get("name")) for item in visible_callables if item.get("name")}
    names -= set(tc.disabled_tools or [])
    return {name for name in names if name in all_tools or name in registry_names or name.startswith("mcp__")}


def _mcp_defaults() -> dict[str, Any]:
    return {
        "server_count": 0,
        "enabled_server_count": 0,
        "discovered_tool_count": 0,
        "active_mcp_tool_count": 0,
        "servers": [],
    }


def _mcp_section(settings: Any, effective_mcp_names: set[str]) -> dict[str, Any]:
    from ..tools.definitions.mcp_schema import MCPServerDefinition

    servers_dir = getattr(settings, "mcp_servers_dir", None)
    if servers_dir is None:
        servers_dir = Path(settings.data_dir) / "mcp_servers"
    server_path = Path(servers_dir)
    servers: list[MCPServerDefinition] = []
    if server_path.exists():
        for path in sorted(server_path.glob("*.json")):
            try:
                servers.append(
                    MCPServerDefinition.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to load MCP server overview from %s: %s", path, exc)

    active_by_server: dict[str, set[str]] = {}
    for tool_name in effective_mcp_names:
        parts = tool_name.split("__", 2)
        if len(parts) == 3:
            active_by_server.setdefault(parts[1], set()).add(tool_name)

    server_summaries = []
    for server in sorted(servers, key=lambda item: item.id):
        discovered_names = {
            f"mcp__{server.id}__{tool.name}" for tool in server.discovered_tools
        }
        active = active_by_server.get(server.id, set()) & discovered_names
        server_summaries.append(
            {
                "id": server.id,
                "name": server.name,
                "enabled": bool(server.enabled),
                "install_status": server.install_status,
                "discovered_tool_count": len(discovered_names),
                "active_tool_count": len(active),
            }
        )

    return {
        "server_count": len(servers),
        "enabled_server_count": sum(1 for server in servers if server.enabled),
        "discovered_tool_count": sum(len(server.discovered_tools) for server in servers),
        "active_mcp_tool_count": len(effective_mcp_names),
        "servers": server_summaries,
    }


def _skills_defaults(tc: ThreadConfig) -> dict[str, Any]:
    return {
        "installed_count": 0,
        "globally_enabled_names": [],
        "thread_enabled_names": list(tc.enabled_skills or []),
        "thread_disabled_names": list(tc.disabled_skills or []),
        "active_names": [],
        "active_skill_count": 0,
        "active_skill_kit_count": 0,
        "required_tools": [],
        "skill_kits": [],
    }


def _skills_section(agent: Any, user_id: str, tc: ThreadConfig) -> dict[str, Any]:
    manager = getattr(agent, "skill_manager", None)
    if manager is None:
        return _skills_defaults(tc)
    profile = agent.profile_manager.get_profile(user_id)
    global_names = list(getattr(profile, "enabled_global_skills", []) or [])
    thread_enabled = list(tc.enabled_skills or [])
    thread_disabled = list(tc.disabled_skills or [])
    installed = manager.list_installed(user_id=user_id)
    active = manager.list_for_thread(
        user_id=user_id,
        enabled_global_skills=global_names,
        thread_enabled_skills=thread_enabled,
        thread_disabled_skills=thread_disabled,
    )
    kits = [skill for skill in active if skill.is_skill_kit]
    required_tools: list[str] = []
    for skill in active:
        for tool_name in skill.required_tools:
            if tool_name not in required_tools:
                required_tools.append(tool_name)
    return {
        "installed_count": len(installed),
        "globally_enabled_names": global_names,
        "thread_enabled_names": thread_enabled,
        "thread_disabled_names": thread_disabled,
        "active_names": [skill.name for skill in active],
        "active_skill_count": max(0, len(active) - len(kits)),
        "active_skill_kit_count": len(kits),
        "required_tools": required_tools,
        "skill_kits": [
            {
                "name": skill.name,
                "required_tools": skill.required_tools,
                "tool_ttl": skill.tool_ttl,
            }
            for skill in kits
        ],
    }


def _todos_defaults() -> dict[str, Any]:
    return {
        "total_count": 0,
        "active_count": 0,
        "pending_count": 0,
        "in_progress_count": 0,
        "done_count": 0,
        "scheduled_count": 0,
        "recurring_count": 0,
        "next_scheduled_item": None,
        "first_labels": [],
    }


def _todos_section(agent: Any, settings: Any, user_id: str, thread_id: str) -> dict[str, Any]:
    manager = getattr(agent, "todo_manager", None)
    if manager is None:
        from ..core.todo_manager import TodoManager

        manager = TodoManager(settings.data_dir)
    todo_list = manager.get_todos(user_id)
    items = [item for item in getattr(todo_list, "items", []) if item.thread_id == thread_id]
    active = [item for item in items if _status_value(item) != "done"]
    scheduled = [item for item in items if getattr(item, "scheduled_for", None)]
    next_item = min(
        scheduled,
        key=lambda item: ensure_aware_utc(item.scheduled_for),
        default=None,
    )
    return {
        "total_count": len(items),
        "active_count": len(active),
        "pending_count": sum(1 for item in items if _status_value(item) == "pending"),
        "in_progress_count": sum(1 for item in items if _status_value(item) == "in_progress"),
        "done_count": sum(1 for item in items if _status_value(item) == "done"),
        "scheduled_count": len(scheduled),
        "recurring_count": sum(1 for item in items if getattr(item, "recurrence", None)),
        "next_scheduled_item": _todo_summary(next_item) if next_item else None,
        "first_labels": [
            str(getattr(item, "task", "") or getattr(item, "id", ""))
            for item in active[:3]
        ],
    }


def _todo_summary(item: Any) -> dict[str, Any]:
    return {
        "id": getattr(item, "id", None),
        "task": getattr(item, "task", None),
        "status": _status_value(item),
        "scheduled_for": _iso(getattr(item, "scheduled_for", None)),
        "recurrence": getattr(item, "recurrence", None),
    }


def _status_value(item: Any) -> str:
    status = getattr(item, "status", "")
    return str(getattr(status, "value", status) or "")


def _triggers_defaults() -> dict[str, Any]:
    return {
        "total_count": 0,
        "enabled_count": 0,
        "degraded_count": 0,
        "failing_count": 0,
        "triggers": [],
        "first_labels": [],
    }


def _triggers_section(agent: Any, settings: Any, user_id: str, thread_id: str) -> dict[str, Any]:
    manager = getattr(agent, "trigger_manager", None)
    if manager is None:
        from ..core.trigger_manager import TriggerManager

        manager = TriggerManager(settings.data_dir)
    triggers = [trigger for trigger in manager.get_triggers(user_id) if trigger.thread_id == thread_id]
    enabled = [trigger for trigger in triggers if trigger.enabled]
    summaries = [
        {
            "id": trigger.id,
            "name": trigger.name,
            "source_type": trigger.source_type,
            "enabled": bool(trigger.enabled),
            "health_status": trigger.health_status,
            "last_fired": _iso(trigger.last_fired),
            "fire_count": trigger.fire_count,
            "consecutive_errors": trigger.consecutive_errors,
            "last_error": trigger.last_error,
        }
        for trigger in triggers
    ]
    return {
        "total_count": len(triggers),
        "enabled_count": len(enabled),
        "degraded_count": sum(1 for trigger in triggers if trigger.health_status == "degraded"),
        "failing_count": sum(1 for trigger in triggers if trigger.health_status == "failing"),
        "triggers": summaries,
        "first_labels": [trigger.name for trigger in enabled[:3]],
    }


def _chat_apps_defaults(tc: ThreadConfig) -> dict[str, Any]:
    return {
        "binding_count": 0,
        "providers": [],
        "shared_bot_count": 0,
        "byo_bot_count": 0,
        "telegram_delivery_mode": tc.telegram_autonomous_delivery,
        "in_app_notification_level": tc.in_app_notification_level,
        "bindings": [],
    }


def _chat_apps_section(
    agent: Any,
    settings: Any,
    thread_id: str,
    tc: ThreadConfig,
) -> dict[str, Any]:
    repo = getattr(agent, "chat_bindings_repo", None)
    if repo is None:
        return _chat_apps_defaults(tc)
    bindings = repo.list_thread_bindings(thread_id)
    summaries = []
    for binding in bindings:
        bot_username = None
        if binding.user_telegram_bot_id is not None:
            try:
                bot = repo.get_user_telegram_bot(binding.user_telegram_bot_id)
                bot_username = getattr(bot, "bot_username", None) if bot else None
            except Exception:
                bot_username = None
        elif binding.provider == "telegram":
            bot_username = getattr(settings, "telegram_bot_username", None)
        summaries.append(
            {
                "id": binding.id,
                "provider": binding.provider,
                "platform_chat_id": binding.platform_chat_id,
                "delivery": "byo_bot" if binding.user_telegram_bot_id is not None else "shared_bot",
                "user_telegram_bot_id": binding.user_telegram_bot_id,
                "bot_username": bot_username,
                "created_at": binding.created_at,
            }
        )
    providers = sorted({binding.provider for binding in bindings})
    return {
        "binding_count": len(bindings),
        "providers": providers,
        "shared_bot_count": sum(1 for b in bindings if b.user_telegram_bot_id is None),
        "byo_bot_count": sum(1 for b in bindings if b.user_telegram_bot_id is not None),
        "telegram_delivery_mode": tc.telegram_autonomous_delivery,
        "in_app_notification_level": tc.in_app_notification_level,
        "bindings": summaries,
    }


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    return dict(vars(value))


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_aware_utc(value).isoformat()
    return str(value)
