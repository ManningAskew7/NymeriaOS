"""Best-effort Rich REPL header snapshot construction."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ...vendor.react_agent.cliproxy import looks_like_cliproxy_url
from .transport.disconnected import is_disconnected_client

HeaderHealthStatus = Literal["ok", "error", "local", "disconnected", "unknown"]

DEFAULT_HEADER_FETCH_TIMEOUT_SECONDS = float(
    os.environ.get("NYMERIA_HEADER_TIMEOUT", "5.0")
)
_MISSING = object()


@dataclass(frozen=True, slots=True)
class HeaderHealthSnapshot:
    """Connection health shown by the header and compact status bar."""

    status: HeaderHealthStatus = "unknown"
    backend_url: str = ""
    latency_ms: int | None = None
    message: str = ""

    @property
    def label(self) -> str:
        if self.status == "ok":
            suffix = f" {self.latency_ms}ms" if self.latency_ms is not None else ""
            return f"api ok{suffix}"
        if self.status == "error":
            return "api error"
        if self.status == "local":
            return "local"
        if self.status == "disconnected":
            return "disconnected"
        return "api"


@dataclass(frozen=True, slots=True)
class CLIHeaderSnapshot:
    """Rendered startup/header data for the Rich REPL."""

    thread_id: str
    user_id: str
    user_display_name: str = ""
    user_role: str = ""
    thread_title: str = "New Chat"
    platform: str = "cli"
    pinned: bool = False
    callable: bool = False
    callable_name: str = ""
    callable_team: str = ""
    provider: str = ""
    api_type: str = ""
    model: str = ""
    thinking_mode: str = "off"
    context_tokens: int | None = None
    context_limit: int | None = None
    context_percent: float | None = None
    compaction_count: int | None = None
    tool_count: int = 0
    mcp_tool_count: int = 0
    callable_tool_count: int = 0
    skill_count: int = 0
    skill_kit_count: int = 0
    todo_count: int = 0
    todo_labels: tuple[str, ...] = ()
    trigger_count: int = 0
    trigger_labels: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    backend_url: str = ""
    health: HeaderHealthSnapshot = field(default_factory=HeaderHealthSnapshot)
    failures: tuple[str, ...] = ()

    @property
    def short_thread_id(self) -> str:
        return compact_id(self.thread_id)

    @property
    def connection_label(self) -> str:
        return self.health.label


async def build_header_snapshot(
    state: Any,
    client: Any,
    *,
    runtime_config: Any | None = None,
    timeout_seconds: float = DEFAULT_HEADER_FETCH_TIMEOUT_SECONDS,
) -> CLIHeaderSnapshot:
    """Fetch header data in parallel and degrade when optional calls fail."""

    thread_id = str(getattr(state, "thread_id", "") or "")
    user_id = str(getattr(state, "user_id", "default") or "default")
    local_data = _local_header_data(state, thread_id=thread_id, user_id=user_id)
    overview_failures: tuple[str, ...] = ()
    overview_missing = False

    overview_owner = _method_owner(client, "get_thread_overview")
    if overview_owner is not None:
        overview_result, health_result = await asyncio.gather(
            _optional_client_call(
                client,
                "get_thread_overview",
                thread_id,
                timeout_seconds=timeout_seconds,
                user_id=user_id,
            ),
            build_health_snapshot(
                client,
                runtime_config=runtime_config,
                timeout_seconds=timeout_seconds,
            ),
        )
        if isinstance(overview_result, Mapping):
            health = health_result
            if not isinstance(health, HeaderHealthSnapshot):
                health = HeaderHealthSnapshot(status="unknown")
            backend_url = health.backend_url or _backend_url(client, runtime_config)
            return _snapshot_from_overview(
                overview_result,
                thread_id=thread_id,
                user_id=user_id,
                backend_url=backend_url,
                health=health if health.backend_url else _with_backend_url(health, backend_url),
            )
        if isinstance(overview_result, BaseException):
            overview_failures = ("overview",)
        elif overview_result is None:
            overview_missing = True

    tasks = {
        "settings": _optional_client_call(
            client,
            "get_settings",
            timeout_seconds=timeout_seconds,
            user_id=user_id,
        ),
        "threads": _optional_client_call(
            client,
            "list_threads",
            user_id,
            timeout_seconds=timeout_seconds,
        ),
        "thread_config": _optional_client_call(
            client,
            "get_thread_config",
            thread_id,
            timeout_seconds=timeout_seconds,
            user_id=user_id,
        ),
        "context_stats": _optional_client_call(
            client,
            "get_context_stats",
            thread_id,
            timeout_seconds=timeout_seconds,
            user_id=user_id,
        ),
        "default_tools": _optional_client_call(
            client,
            "get_default_tools",
            timeout_seconds=timeout_seconds,
            user_id=user_id,
        ),
        "active_skills": _optional_client_call(
            client,
            "get_thread_active_skills",
            thread_id,
            timeout_seconds=timeout_seconds,
            user_id=user_id,
        ),
        "callable_tools": _optional_client_call(
            client,
            "get_thread_callable_tools",
            thread_id,
            timeout_seconds=timeout_seconds,
            user_id=user_id,
        ),
        "triggers": _optional_client_call(
            client,
            "list_triggers",
            timeout_seconds=timeout_seconds,
            user_id=user_id,
            enabled_only=True,
            thread_id=thread_id,
        ),
        "todos": _optional_client_call(
            client,
            "list_todos",
            user_id,
            timeout_seconds=timeout_seconds,
            thread_id=thread_id,
        ),
        "me": _optional_client_call(
            client,
            "get_me",
            timeout_seconds=timeout_seconds,
            act_as=user_id,
        ),
        "health": build_health_snapshot(
            client,
            runtime_config=runtime_config,
            timeout_seconds=timeout_seconds,
        ),
    }
    results = dict(zip(tasks, await asyncio.gather(*tasks.values()), strict=True))

    settings = _mapping_or(results["settings"], local_data.get("settings", {}))
    threads = _sequence_or(results["threads"], local_data.get("threads", ()))
    thread_config = _mapping_or(
        results["thread_config"],
        local_data.get("thread_config", {}),
    )
    context_stats = _mapping_or(
        results["context_stats"],
        local_data.get("context_stats", {}),
    )
    default_tools = _mapping_or(
        results["default_tools"],
        local_data.get("default_tools", {}),
    )
    active_skills = _mapping_or(
        results["active_skills"],
        local_data.get("active_skills", {}),
    )
    callable_tools = _mapping_or(
        results["callable_tools"],
        local_data.get("callable_tools", {}),
    )
    triggers = _sequence_or(results["triggers"], local_data.get("triggers", ()))
    todos = _sequence_or(results["todos"], local_data.get("todos", ()))
    me = _mapping_or(results["me"], {})
    health = results["health"]
    if not isinstance(health, HeaderHealthSnapshot):
        health = HeaderHealthSnapshot(status="unknown")

    thread = _thread_from_list(threads, thread_id) or _mapping_or(
        local_data.get("thread"),
        {},
    )
    provider, api_type, model, thinking_mode = _resolve_llm(
        settings,
        thread_config,
        context_stats,
    )
    tool_count, mcp_tool_count = _tool_counts(default_tools, thread_config)
    skill_count, skill_kit_count = _skill_counts(active_skills)
    active_todos = _current_thread_active_todos(todos, thread_id)
    enabled_triggers = _current_thread_enabled_triggers(triggers, thread_id)

    legacy_failures = _fetch_failures(results)
    if overview_missing and legacy_failures:
        overview_failures = ("overview unavailable",)

    backend_url = health.backend_url or _backend_url(client, runtime_config)
    return CLIHeaderSnapshot(
        thread_id=thread_id,
        user_id=user_id,
        user_display_name=str(me.get("display_name") or me.get("email") or ""),
        user_role=str(me.get("role") or ""),
        thread_title=_thread_title(thread, thread_config, thread_id),
        platform=str(_mapping_get(thread, "platform", "") or _classify_platform(thread_id)),
        pinned=bool(_mapping_get(thread, "pinned", False)),
        callable=bool(_mapping_get(thread_config, "callable", False)),
        callable_name=str(_mapping_get(thread_config, "callable_name", "") or ""),
        callable_team=str(
            _mapping_get(thread_config, "callable_team_name", "")
            or _mapping_get(thread_config, "callable_team_id", "")
            or ""
        ),
        provider=provider,
        api_type=api_type,
        model=model,
        thinking_mode=thinking_mode,
        context_tokens=_int_or_none(
            _first_number(context_stats, "total_tokens", "used_tokens", "context_tokens")
        ),
        context_limit=_int_or_none(
            _first_number(context_stats, "context_limit", "max_tokens", "context_window")
        ),
        context_percent=_context_percent(context_stats),
        compaction_count=_int_or_none(
            _first_number(context_stats, "compaction_count", "compactions")
        ),
        tool_count=tool_count,
        mcp_tool_count=mcp_tool_count,
        callable_tool_count=_callable_tool_count(callable_tools),
        skill_count=skill_count,
        skill_kit_count=skill_kit_count,
        todo_count=len(active_todos),
        todo_labels=_item_labels(active_todos, "task"),
        trigger_count=len(enabled_triggers),
        trigger_labels=_item_labels(enabled_triggers, "name"),
        flags=_config_flags(thread_config),
        backend_url=backend_url,
        health=health if health.backend_url else _with_backend_url(health, backend_url),
        failures=tuple(dict.fromkeys((*overview_failures, *legacy_failures))),
    )


async def build_health_snapshot(
    client: Any,
    *,
    runtime_config: Any | None = None,
    timeout_seconds: float = DEFAULT_HEADER_FETCH_TIMEOUT_SECONDS,
) -> HeaderHealthSnapshot:
    """Measure API health once for a header refresh."""

    backend_url = _backend_url(client, runtime_config)
    if client is None or is_disconnected_client(client):
        return HeaderHealthSnapshot(
            status="disconnected",
            backend_url=backend_url,
            message=str(getattr(client, "startup_error", "") or ""),
        )
    if _is_local_client(client):
        return HeaderHealthSnapshot(status="local", backend_url=backend_url)

    owner = _method_owner(client, "health")
    if owner is None:
        return HeaderHealthSnapshot(status="unknown", backend_url=backend_url)

    start = time.perf_counter()
    try:
        result = getattr(owner, "health")()
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - health is advisory.
        return HeaderHealthSnapshot(
            status="error",
            backend_url=backend_url,
            message=str(exc) or exc.__class__.__name__,
        )

    latency_ms = max(0, round((time.perf_counter() - start) * 1000))
    ok = bool(result)
    if isinstance(result, Mapping):
        ok = str(result.get("status", "")).casefold() in {"ok", "healthy", "up"}
    return HeaderHealthSnapshot(
        status="ok" if ok else "error",
        backend_url=backend_url,
        latency_ms=latency_ms,
    )


def _snapshot_from_overview(
    overview: Mapping[str, Any],
    *,
    thread_id: str,
    user_id: str,
    backend_url: str,
    health: HeaderHealthSnapshot,
) -> CLIHeaderSnapshot:
    thread = _mapping_or(overview.get("thread"), {})
    context = _mapping_or(overview.get("context"), {})
    config = _mapping_or(overview.get("config_summary"), {})
    llm = _mapping_or(overview.get("llm"), {})
    callable_section = _mapping_or(overview.get("callable"), {})
    tools = _mapping_or(overview.get("tools"), {})
    skills = _mapping_or(overview.get("skills"), {})
    todos = _mapping_or(overview.get("todos"), {})
    triggers = _mapping_or(overview.get("triggers"), {})
    user = _mapping_or(overview.get("user"), {})
    section_errors = _mapping_or(overview.get("section_errors"), {})

    return CLIHeaderSnapshot(
        thread_id=thread_id,
        user_id=user_id,
        user_display_name=str(user.get("display_name") or ""),
        user_role=str(user.get("role") or ""),
        thread_title=str(thread.get("title") or compact_id(thread_id)),
        platform=str(thread.get("platform") or _classify_platform(thread_id)),
        pinned=bool(thread.get("pinned", False)),
        callable=bool(callable_section.get("enabled", False)),
        callable_name=str(callable_section.get("name") or ""),
        callable_team=str(
            callable_section.get("team_name")
            or callable_section.get("team_id")
            or ""
        ),
        provider=str(llm.get("provider_label") or llm.get("provider") or ""),
        api_type=str(llm.get("api_mode_label") or llm.get("api_mode") or ""),
        model=str(llm.get("model") or context.get("model") or ""),
        thinking_mode=str(llm.get("thinking_label") or "off"),
        context_tokens=_int_or_none(
            _first_number(context, "total_tokens", "used_tokens", "context_tokens")
        ),
        context_limit=_int_or_none(
            _first_number(context, "context_limit", "max_tokens", "context_window")
        ),
        context_percent=_context_percent(context),
        compaction_count=_int_or_none(
            _first_number(context, "compaction_count", "compactions")
        ),
        tool_count=_int_or_zero(tools.get("effective_builtin_count")),
        mcp_tool_count=_int_or_zero(tools.get("effective_mcp_count")),
        callable_tool_count=_int_or_zero(tools.get("effective_callable_count")),
        skill_count=_int_or_zero(skills.get("active_skill_count")),
        skill_kit_count=_int_or_zero(skills.get("active_skill_kit_count")),
        todo_count=_int_or_zero(todos.get("active_count")),
        todo_labels=tuple(str(item) for item in _string_list(todos.get("first_labels"))[:2]),
        trigger_count=_int_or_zero(triggers.get("enabled_count")),
        trigger_labels=tuple(str(item) for item in _string_list(triggers.get("first_labels"))[:2]),
        flags=_overview_flags(config, callable_section),
        backend_url=backend_url,
        health=health,
        failures=tuple(str(name) for name in section_errors),
    )


def compact_id(value: Any, *, width: int = 8) -> str:
    text = str(value or "")
    return text[:width] if len(text) > width else text


def concise_connection_label(snapshot: CLIHeaderSnapshot | None, client: Any = None) -> str:
    """Return the compact label used by live status bars."""

    if snapshot is not None:
        return snapshot.connection_label
    if client is None:
        return ""
    if is_disconnected_client(client):
        return "disconnected"
    if _is_local_client(client):
        return "local"
    label = str(getattr(client, "connection_label", "") or "")
    if label.startswith("api "):
        return "api"
    return label


async def _optional_client_call(
    client: Any,
    method_name: str,
    *args: Any,
    timeout_seconds: float,
    **kwargs: Any,
) -> Any:
    owner = _method_owner(client, method_name)
    if owner is None:
        return _MISSING
    try:
        result = getattr(owner, method_name)(*args, **kwargs)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(result, timeout=timeout_seconds)
        return result
    except TypeError:
        if not kwargs:
            return _MISSING
        try:
            result = getattr(owner, method_name)(*args)
            if inspect.isawaitable(result):
                return await asyncio.wait_for(result, timeout=timeout_seconds)
            return result
        except Exception as exc:  # noqa: BLE001 - optional header data.
            return exc
    except Exception as exc:  # noqa: BLE001 - optional header data.
        return exc


def _method_owner(client: Any, method_name: str) -> Any | None:
    if client is None:
        return None
    method = getattr(client, method_name, None)
    if callable(method):
        return client
    api = getattr(client, "api", None)
    method = getattr(api, method_name, None)
    if callable(method):
        return api
    return None


def _local_header_data(state: Any, *, thread_id: str, user_id: str) -> dict[str, Any]:
    agent = getattr(state, "agent", None)
    if agent is None:
        return {"settings": _settings_mapping(getattr(state, "settings", None))}

    data: dict[str, Any] = {"settings": _settings_mapping(getattr(state, "settings", None))}
    try:
        store = agent.thread_metadata_manager.get_store(user_id)
        threads = [_model_dump(meta) for meta in store.threads.values()]
        data["threads"] = threads
        data["thread"] = next(
            (thread for thread in threads if _thread_id(thread) == thread_id),
            {},
        )
    except Exception:
        data["threads"] = []

    try:
        config = agent.thread_config_manager.get_config(thread_id)
        data["thread_config"] = _model_dump(config) if config is not None else {}
    except Exception:
        data["thread_config"] = {}

    try:
        data["context_stats"] = agent.get_context_stats(thread_id)
    except Exception:
        data["context_stats"] = {}

    try:
        data["default_tools"] = _local_default_tools(agent, user_id)
    except Exception:
        data["default_tools"] = {}

    try:
        data["active_skills"] = _local_active_skills(agent, user_id, data["thread_config"])
    except Exception:
        data["active_skills"] = {}

    try:
        data["callable_tools"] = _local_callable_tools(agent, user_id, thread_id, data["thread_config"])
    except Exception:
        data["callable_tools"] = {}

    try:
        manager = getattr(agent, "trigger_manager", None)
        if manager is None:
            from ...core.trigger_manager import TriggerManager

            manager = TriggerManager(getattr(state, "settings").data_dir)
        data["triggers"] = [
            _model_dump(trigger)
            for trigger in manager.get_triggers(user_id)
            if getattr(trigger, "enabled", False)
            and getattr(trigger, "thread_id", "") == thread_id
        ]
    except Exception:
        data["triggers"] = []
    try:
        manager = getattr(agent, "todo_manager", None)
        if manager is None:
            from ...core.todo_manager import TodoManager

            manager = TodoManager(getattr(state, "settings").data_dir)
        todo_list = manager.get_todos(user_id)
        data["todos"] = [
            _model_dump(todo)
            for todo in todo_list.get_active_todos_for_thread(thread_id)
        ]
    except Exception:
        data["todos"] = []
    return data


def _settings_mapping(settings: Any) -> dict[str, Any]:
    if settings is None:
        return {}
    keys = (
        "llm_provider",
        "llm_model",
        "llm_base_url",
        "llm_extended_thinking",
        "openai_api_mode",
        "llm_reasoning_effort",
    )
    return {key: getattr(settings, key, None) for key in keys}


def _local_default_tools(agent: Any, user_id: str) -> dict[str, Any]:
    from ...tools import ALL_TOOLS

    profile = agent.profile_manager.get_profile(user_id)
    default_tools = profile.tool_preferences.default_thread_tools
    names = list(default_tools) if default_tools is not None else [tool.name for tool in ALL_TOOLS]
    return {"default_tools": names, "available_tools": [{"name": name} for name in names]}


def _local_active_skills(
    agent: Any,
    user_id: str,
    thread_config: Mapping[str, Any],
) -> dict[str, Any]:
    manager = getattr(agent, "skill_manager", None)
    if manager is None:
        return {"skills": []}
    profile = agent.profile_manager.get_profile(user_id)
    active = manager.list_for_thread(
        user_id=user_id,
        enabled_global_skills=list(getattr(profile, "enabled_global_skills", []) or []),
        thread_enabled_skills=_string_list(thread_config.get("enabled_skills")),
        thread_disabled_skills=_string_list(thread_config.get("disabled_skills")),
    )
    return {"skills": [_model_dump(skill) for skill in active]}


def _local_callable_tools(
    agent: Any,
    user_id: str,
    thread_id: str,
    thread_config: Mapping[str, Any],
) -> dict[str, Any]:
    get_scoped = getattr(agent, "_get_team_scoped_callable_threads", None)
    if not callable(get_scoped):
        return {"callable_threads": []}
    disabled = set(_string_list(thread_config.get("disabled_tools")))
    own_name = (
        str(thread_config.get("callable_name") or "")
        if bool(thread_config.get("callable"))
        else ""
    )
    visible = []
    for config in get_scoped(user_id=user_id, caller_thread_id=thread_id):
        name = str(getattr(config, "callable_name", "") or "")
        if not name or getattr(config, "thread_id", "") == thread_id:
            continue
        if name in disabled or (own_name and name == own_name):
            continue
        visible.append(_model_dump(config))
    return {"callable_thread_count": len(visible), "callable_threads": visible}


def _resolve_llm(
    settings: Mapping[str, Any],
    thread_config: Mapping[str, Any],
    context_stats: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    llm_config = _mapping_or(thread_config.get("llm_config"), {})
    provider = str(
        llm_config.get("provider")
        or settings.get("llm_provider")
        or ""
    )
    base_url = _resolve_llm_base_url(settings, llm_config, provider)
    model = str(
        llm_config.get("model")
        or context_stats.get("model")
        or context_stats.get("active_model")
        or settings.get("llm_model")
        or ""
    )
    extended = _coalesce_bool(
        llm_config.get("extended_thinking"),
        settings.get("llm_extended_thinking"),
        default=False,
    )
    effort = llm_config.get("reasoning_effort")
    if effort is None:
        effort = settings.get("llm_reasoning_effort")
    return (
        _provider_label(provider, base_url),
        _api_type_label(provider, settings, llm_config),
        model,
        thinking_mode(provider=provider, model=model, extended=extended, effort=effort),
    )


def _resolve_llm_base_url(
    settings: Mapping[str, Any],
    llm_config: Mapping[str, Any],
    provider: str,
) -> str:
    """Mirror NymeriaAgent's effective LLM base URL resolution for display."""

    if "base_url" in llm_config and llm_config.get("base_url") is not None:
        return str(llm_config.get("base_url") or "").strip().rstrip("/")

    global_provider = str(settings.get("llm_provider") or "")
    global_url = str(settings.get("llm_base_url") or "").strip().rstrip("/")
    if provider != global_provider:
        if provider == "anthropic" and global_url and looks_like_cliproxy_url(global_url):
            return global_url[:-3] if global_url.endswith("/v1") else global_url
        return ""
    return global_url


def _provider_label(provider: str, base_url: str) -> str:
    provider_text = str(provider or "").strip()
    normalized = provider_text.casefold()
    if normalized == "anthropic":
        if base_url and looks_like_cliproxy_url(base_url):
            return "cliproxy OAuth"
        if base_url:
            return "custom Claude"
        return "Claude API"
    if normalized == "openai":
        if base_url and looks_like_cliproxy_url(base_url):
            return "OpenAI proxy"
        if base_url:
            return "OpenAI compat"
        return "OpenAI API"
    if normalized == "openrouter":
        return "OpenRouter"
    if normalized == "custom":
        return "Custom provider"
    return provider_text


def _api_type_label(
    provider: str,
    settings: Mapping[str, Any],
    llm_config: Mapping[str, Any],
) -> str:
    normalized = str(provider or "").strip().casefold()
    if normalized == "anthropic":
        return "messages/v1"
    if normalized in {"openai", "openrouter", "custom"}:
        mode = str(
            llm_config.get("openai_api_mode")
            or settings.get("openai_api_mode")
            or "responses"
        ).strip()
        return _openai_api_mode_label(mode)
    return ""


def _openai_api_mode_label(mode: str) -> str:
    normalized = str(mode or "").strip().casefold().replace("-", "_")
    if normalized == "chat_completions":
        return "chat completions/v1"
    if normalized == "responses":
        return "responses/v1"
    if normalized == "completions":
        return "completions/v1"
    return str(mode or "").strip()


def thinking_mode(
    *,
    provider: str,
    model: str,
    extended: bool,
    effort: Any = None,
) -> str:
    """Return the concise thinking-mode label used in the CLI header."""

    effort_text = str(effort or "").strip().casefold()
    enabled = bool(extended) or bool(effort_text)
    if not enabled:
        return "off"
    if _uses_adaptive_thinking(provider=provider, model=model):
        return f"adaptive ({effort_text})" if effort_text else "adaptive"
    return effort_text or "medium"


def _uses_adaptive_thinking(*, provider: str, model: str) -> bool:
    provider_text = provider.casefold()
    model_text = model.casefold()
    if "anthropic" not in provider_text and "claude" not in model_text:
        return False
    return any(
        marker in model_text
        for marker in ("opus-4-6", "sonnet-4-6", "opus-4-7", "sonnet-4-7")
    )


def _tool_counts(
    default_tools: Mapping[str, Any],
    thread_config: Mapping[str, Any],
) -> tuple[int, int]:
    available = _mapping_sequence(default_tools.get("available_tools"))
    metadata = {_tool_name(item): item for item in available if _tool_name(item)}
    names = set(_string_list(default_tools.get("default_tools")))
    if not names:
        names.update(_tool_name(item) for item in available if item.get("is_default"))
    names.update(_string_list(thread_config.get("enabled_tools")))
    names.difference_update(_string_list(thread_config.get("disabled_tools")))
    mcp = {name for name in names if _is_mcp_tool(name, metadata.get(name, {}))}
    return max(0, len(names) - len(mcp)), len(mcp)


def _skill_counts(active_skills: Mapping[str, Any]) -> tuple[int, int]:
    skills = _mapping_sequence(active_skills.get("skills"))
    kits = [skill for skill in skills if bool(skill.get("is_skill_kit"))]
    return max(0, len(skills) - len(kits)), len(kits)


def _callable_tool_count(callable_tools: Mapping[str, Any]) -> int:
    count = callable_tools.get("callable_thread_count")
    if isinstance(count, int):
        return max(0, count)
    return len(_mapping_sequence(callable_tools.get("callable_threads")))


def _current_thread_active_todos(
    todos: Sequence[Any],
    thread_id: str,
) -> list[Mapping[str, Any]]:
    selected = []
    for item in todos:
        if not isinstance(item, Mapping):
            continue
        status = str(_mapping_get(item, "status", "") or "").casefold()
        if status == "done":
            continue
        if thread_id and str(_mapping_get(item, "thread_id", "") or "") != thread_id:
            continue
        selected.append(item)
    return selected


def _current_thread_enabled_triggers(
    triggers: Sequence[Any],
    thread_id: str,
) -> list[Mapping[str, Any]]:
    selected = []
    for item in triggers:
        if not isinstance(item, Mapping):
            continue
        if not _mapping_get(item, "enabled", True):
            continue
        if thread_id and str(_mapping_get(item, "thread_id", "") or "") != thread_id:
            continue
        selected.append(item)
    return selected


def _item_labels(
    items: Sequence[Mapping[str, Any]],
    key: str,
    *,
    limit: int = 2,
) -> tuple[str, ...]:
    labels: list[str] = []
    for item in items:
        label = str(
            _mapping_get(item, key)
            or _mapping_get(item, "title")
            or _mapping_get(item, "id")
            or ""
        ).strip()
        if not label:
            continue
        labels.append(label)
        if len(labels) >= limit:
            break
    return tuple(labels)


def _config_flags(thread_config: Mapping[str, Any]) -> tuple[str, ...]:
    flags: list[str] = []
    if thread_config.get("instructions"):
        flags.append("instructions")
    if thread_config.get("system_prompt"):
        flags.append("system prompt")
    if bool(thread_config.get("inject_todos_in_prompt")):
        flags.append("TODOs")
    if bool(thread_config.get("inject_profile_in_prompt")):
        flags.append("profile")
    if bool(thread_config.get("callable")):
        name = str(thread_config.get("callable_name") or "callable")
        flags.append(f"callable {name}")
        team = thread_config.get("callable_team_name") or thread_config.get("callable_team_id")
        if team:
            flags.append(f"team {team}")
    return tuple(flags)


def _overview_flags(
    config: Mapping[str, Any],
    callable_section: Mapping[str, Any],
) -> tuple[str, ...]:
    flags: list[str] = []
    if bool(config.get("instructions_present")):
        flags.append("instructions")
    if bool(config.get("system_prompt_override_present")):
        flags.append("system prompt")
    if bool(config.get("inject_todos_in_prompt")):
        flags.append("TODOs")
    if bool(config.get("inject_profile_in_prompt")):
        flags.append("profile")
    if bool(callable_section.get("enabled")):
        name = str(callable_section.get("name") or "callable")
        flags.append(f"callable {name}")
        team = callable_section.get("team_name") or callable_section.get("team_id")
        if team:
            flags.append(f"team {team}")
    if bool(config.get("show_autonomous_prompts")):
        flags.append("autonomous prompts")
    if bool(config.get("show_prompt_metadata")):
        flags.append("prompt metadata")
    delivery = str(config.get("telegram_autonomous_delivery") or "")
    if delivery and delivery != "full":
        flags.append(f"telegram {delivery}")
    notifications = str(config.get("in_app_notification_level") or "")
    if notifications and notifications != "notify_only":
        flags.append(f"notifications {notifications}")
    return tuple(flags)


def _context_percent(stats: Mapping[str, Any]) -> float | None:
    percent = _first_number(stats, "usage_percentage", "percent_used", "percent")
    if percent is not None:
        return percent * 100 if 0 < percent <= 1 else percent
    used = _first_number(stats, "total_tokens", "used_tokens", "context_tokens")
    limit = _first_number(stats, "context_limit", "max_tokens", "context_window")
    if used is None or not limit:
        return None
    return (used / limit) * 100


def _thread_from_list(
    threads: Sequence[Any],
    thread_id: str,
) -> Mapping[str, Any] | None:
    for item in threads:
        if isinstance(item, Mapping) and _thread_id(item) == thread_id:
            return item
    return None


def _thread_title(
    thread: Mapping[str, Any],
    thread_config: Mapping[str, Any],
    thread_id: str,
) -> str:
    if bool(thread_config.get("callable")) and thread_config.get("callable_name"):
        return str(thread_config["callable_name"])
    title = str(thread.get("title") or "").strip()
    if title:
        return title
    return compact_id(thread_id)


def _thread_id(thread: Mapping[str, Any]) -> str:
    return str(thread.get("thread_id") or thread.get("id") or "")


def _classify_platform(thread_id: str) -> str:
    try:
        from ...core.thread_classification import classify_platform

        return str(classify_platform(thread_id))
    except Exception:
        return "cli"


def _is_mcp_tool(name: str, metadata: Mapping[str, Any]) -> bool:
    if str(name).startswith("mcp__"):
        return True
    values = {
        str(metadata.get("category") or "").casefold(),
        str(metadata.get("tool_type") or "").casefold(),
        str(metadata.get("implementation_type") or "").casefold(),
    }
    return bool(values.intersection({"mcp", "mcp_server", "mcp-server"}))


def _tool_name(tool: Mapping[str, Any]) -> str:
    return str(tool.get("name") or tool.get("id") or tool.get("tool_id") or "")


def _fetch_failures(results: Mapping[str, Any]) -> tuple[str, ...]:
    failures = []
    for name, value in results.items():
        if name == "health":
            continue
        if isinstance(value, BaseException):
            failures.append(name)
    return tuple(failures)


def _backend_url(client: Any, runtime_config: Any | None = None) -> str:
    for source in (client, getattr(client, "api", None), runtime_config):
        value = getattr(source, "base_url", None) or getattr(source, "api_url", None)
        if value:
            return str(value).rstrip("/")
    label = str(getattr(client, "connection_label", "") or "")
    if label.startswith("api "):
        return label[4:].strip()
    return ""


def _with_backend_url(
    health: HeaderHealthSnapshot,
    backend_url: str,
) -> HeaderHealthSnapshot:
    return HeaderHealthSnapshot(
        status=health.status,
        backend_url=backend_url,
        latency_ms=health.latency_ms,
        message=health.message,
    )


def _is_local_client(client: Any) -> bool:
    return str(getattr(client, "connection_label", "") or "").casefold() in {
        "local",
        "local agent",
    }


def _mapping_or(value: Any, fallback: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return fallback if isinstance(fallback, Mapping) else {}


def _sequence_or(value: Any, fallback: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return fallback if isinstance(fallback, Sequence) and not isinstance(fallback, (str, bytes)) else ()


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item)]


def _first_number(payload: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _int_or_none(value: float | None) -> int | None:
    return None if value is None else int(value)


def _int_or_zero(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def _coalesce_bool(*values: Any, default: bool = False) -> bool:
    for value in values:
        if value is not None:
            return bool(value)
    return default


def _model_dump(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return dict(vars(value))


__all__ = [
    "CLIHeaderSnapshot",
    "DEFAULT_HEADER_FETCH_TIMEOUT_SECONDS",
    "HeaderHealthSnapshot",
    "build_header_snapshot",
    "build_health_snapshot",
    "compact_id",
    "concise_connection_label",
    "thinking_mode",
]
