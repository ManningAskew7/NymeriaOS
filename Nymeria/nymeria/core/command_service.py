"""Central markdown slash-command service.

The service owns the command registry, parses a raw slash-command string once,
and executes command bodies against a small backend interface. In-process
callers use :class:`CommandBackendClient`; the REST client remains only for
out-of-process compatibility shims.
"""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.parse import quote

import httpx

from ..config import get_settings
from .time_utils import parse_tool_ttl

logger = logging.getLogger(__name__)

CommandSource = Literal["user", "agent", "cli"]
CommandActor = Literal["user", "agent", "system"]
CommandSurface = Literal[
    "desktop",
    "mobile",
    "cli",
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
    "twitch",
    "api",
    "agent",
]
CommandScope = Literal["global", "surface_local"]
CommandDangerLevel = Literal["safe", "normal", "dangerous"]
CommandExecutionKind = Literal["command", "chat_stream", "surface_local"]
CommandResultLevel = Literal["info", "success", "warning", "error"]

DEFAULT_GLOBAL_SURFACES: tuple[CommandSurface, ...] = (
    "desktop",
    "mobile",
    "cli",
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
    "api",
    "agent",
)

GROUPED = {"config", "env", "tools", "memory", "notepad", "todos"}
AGENT_BLOCKED = {"ask", "stop", "clear", "restart", "compact", "start"}
SKILL_SHOW_MAX_CHARS = 12_000


@dataclass(frozen=True)
class CommandContext:
    user_id: str
    thread_id: str | None = None
    source: CommandSource = "user"
    actor: CommandActor | None = None
    surface: CommandSurface | None = None
    is_admin: bool | None = None
    via_act_as: bool = False

    @property
    def effective_actor(self) -> CommandActor:
        if self.actor is not None:
            return self.actor
        return _actor_from_source(self.source)

    @property
    def effective_surface(self) -> CommandSurface | None:
        if self.surface is not None:
            return self.surface
        return _surface_from_source(self.source)


@dataclass(frozen=True)
class CommandResult:
    success: bool
    markdown: str
    command: str
    level: CommandResultLevel = "info"
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class CommandInfo:
    name: str
    description: str
    usage: str
    category: str
    subcommands: list[str] = field(default_factory=list)
    id: str = ""
    path: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    scope: CommandScope = "global"
    surfaces: list[str] = field(default_factory=list)
    agent_allowed: bool = True
    requires_thread: bool = False
    requires_admin: bool = False
    mutates_state: bool = False
    danger_level: CommandDangerLevel = "safe"
    execution_kind: CommandExecutionKind = "command"
    level: CommandResultLevel = "info"
    note: str | None = None


@dataclass(frozen=True)
class CommandDefinition:
    id: str
    path: tuple[str, ...]
    description: str
    usage: str
    category: str
    aliases: tuple[tuple[str, ...], ...] = ()
    subcommands: tuple[str, ...] = ()
    scope: CommandScope = "global"
    surfaces: tuple[CommandSurface, ...] = DEFAULT_GLOBAL_SURFACES
    agent_allowed: bool = True
    requires_thread: bool = False
    requires_admin: bool = False
    mutates_state: bool = False
    danger_level: CommandDangerLevel = "safe"
    execution_kind: CommandExecutionKind = "command"
    note: str | None = None
    hidden: bool = False

    @property
    def name(self) -> str:
        return " ".join(self.path)

    @property
    def executable(self) -> bool:
        return self.execution_kind == "command"


@dataclass(frozen=True)
class ParsedCommand:
    tokens: tuple[str, ...]
    path: tuple[str, ...]
    args: list[str]
    rest: str
    definition: CommandDefinition | None
    matched_input_len: int = 0


@dataclass(frozen=True)
class SkillSlashResult:
    success: bool
    should_stream: bool
    message: str
    skill_name: str | None = None


def _path_param(value: Any) -> str:
    return quote(str(value), safe="")


def _clean_params(**values: Any) -> dict[str, Any] | None:
    params = {key: value for key, value in values.items() if value is not None}
    return params or None


def coerce_value(value_str: str) -> Any:
    """Auto-convert command string values to bool/None/int/float/str."""
    lower = value_str.lower()
    if lower in ("true", "false"):
        return lower == "true"
    if lower == "none":
        return None
    try:
        return int(value_str)
    except ValueError:
        pass  # Not an integer; try the next scalar type.
    try:
        return float(value_str)
    except ValueError:
        pass  # Not a float; keep the original string.
    return value_str


def fmt_tokens(n: Optional[int]) -> str:
    """Format token counts for compact command output."""
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def http_error_detail(exc: httpx.HTTPStatusError, *, text_limit: int = 200) -> str:
    """Extract a concise user-facing detail from an HTTP status error."""
    response = exc.response
    if response is not None:
        try:
            body = response.json()
            if isinstance(body, dict) and "detail" in body:
                return str(body["detail"])
        except Exception:  # noqa: BLE001
            pass
        text = (response.text or "").strip()
        if text:
            return text[:text_limit]
        return f"HTTP {response.status_code}"
    return str(exc)


def _raise_http_status(status_code: int, detail: str) -> None:
    """Raise an HTTPStatusError compatible with the legacy command executor."""
    request = httpx.Request("COMMAND", "nymeria://command-service")
    response = httpx.Response(
        status_code,
        json={"detail": detail},
        request=request,
    )
    raise httpx.HTTPStatusError(detail, request=request, response=response)


def _resolve_base_url() -> str:
    explicit = os.environ.get("NYMERIA_API_URL")
    if explicit:
        return explicit.rstrip("/")
    if Path("/.dockerenv").exists():
        return "http://api:8000"
    return "http://localhost:8000"


def _actor_from_source(source: str | None) -> CommandActor:
    if source == "agent":
        return "agent"
    return "user"


def _surface_from_source(source: str | None) -> CommandSurface | None:
    if source == "cli":
        return "cli"
    if source == "agent":
        return "agent"
    return None


def _normalize_token(token: str) -> str:
    return token.strip().lower().lstrip("/").replace("-", "_")


def _normalize_path(value: str | tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if isinstance(value, str):
        cleaned = value.strip().lstrip("/")
        if not cleaned:
            return ()
        try:
            parts = shlex.split(cleaned, posix=True)
        except ValueError:
            parts = cleaned.split()
    else:
        parts = [str(part) for part in value]
    return tuple(token for token in (_normalize_token(part) for part in parts) if token)


def _display_path(path: tuple[str, ...] | list[str]) -> str:
    return " ".join(path)


def _usage_for_path(path: tuple[str, ...]) -> str:
    return "/" + _display_path(path)


def _id_for_path(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _alias_display(path: tuple[str, ...]) -> str:
    return "/" + _display_path(path)


def _split_rest_after_tokens(command_text: str, token_count: int) -> str:
    rest = command_text.strip().lstrip("/")
    for _ in range(token_count):
        rest = rest.lstrip()
        _head, sep, tail = rest.partition(" ")
        if not sep:
            return ""
        rest = tail
    return rest.strip()


def _split_args(rest: str) -> list[str]:
    try:
        return shlex.split(rest, posix=True) if rest else []
    except ValueError:
        return rest.split()


def parse_command(raw: str) -> tuple[Optional[str], Optional[str], list[str], str]:
    """Parse a command string into command, subcommand, args, and raw rest."""
    s = raw.strip()
    if not s:
        return None, None, [], ""
    if s.startswith("/"):
        s = s[1:].lstrip()
    if not s:
        return None, None, [], ""

    head, _, tail = s.partition(" ")
    command = head.lower()

    if command in GROUPED:
        tail = tail.strip()
        if not tail:
            return command, None, [], ""
        sub_head, _, sub_tail = tail.partition(" ")
        subcommand = sub_head.lower()
        rest = sub_tail.strip()
    else:
        subcommand = None
        rest = tail.strip()

    try:
        args = shlex.split(rest, posix=True) if rest else []
    except ValueError:
        args = rest.split()

    return command, subcommand, args, rest


def _consume_option(
    args: list[str],
    option: str,
    *,
    default: str,
) -> tuple[str, list[str], str]:
    """Pull ``--key value`` out of ``args``.

    Returns ``(selected_value, remaining_args, error_message)``. ``error_message``
    is empty on success. If the option appears multiple times, the last wins.
    """
    remaining: list[str] = []
    selected = default
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == option:
            if index + 1 >= len(args) or args[index + 1].startswith("--"):
                return selected, list(args), f"{option} requires a value."
            selected = args[index + 1]
            index += 2
            continue
        remaining.append(arg)
        index += 1
    return selected, remaining, ""


def _consume_flag(args: list[str], flag: str) -> tuple[bool, list[str]]:
    """Pull a boolean ``--flag`` out of ``args``."""
    remaining: list[str] = []
    present = False
    for arg in args:
        if arg == flag:
            present = True
        else:
            remaining.append(arg)
    return present, remaining


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80] + f"\n\n**Note:** Output truncated (was {len(text)} chars)."


def _format_legacy_output(text: str) -> tuple[bool, str]:
    """Convert old dispatcher prefixes into markdown command output."""
    stripped = text.strip()
    if stripped.startswith("[Error]:"):
        return False, f"**Error:** {stripped.removeprefix('[Error]:').strip()}"
    if stripped.startswith("[Success]:"):
        return True, f"**Done.** {stripped.removeprefix('[Success]:').strip()}"
    if stripped.startswith("[Info]:"):
        stripped = stripped.removeprefix("[Info]:").strip()

    if not stripped:
        return True, ""

    lines = stripped.splitlines()
    if len(lines) > 1 and lines[0] and not lines[0].startswith(("-", "*", "#", "`")):
        return True, "### " + lines[0] + "\n\n" + "\n".join(lines[1:]).strip()
    return True, stripped


class CommandHttpClient:
    """Small REST compatibility client for out-of-process command callers.

    ``use_act_as`` is enabled for service-token callers. For direct user-token
    callers, the token itself is authoritative and no X-Nymeria-Act-As header
    is sent.
    """

    def __init__(self, base_url: str, api_key: str, *, use_act_as: bool):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.use_act_as = use_act_as
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10))

    @classmethod
    def from_service_token(cls) -> "CommandHttpClient":
        settings = get_settings()
        api_key = settings.nymeria_service_token
        if not api_key:
            raise RuntimeError(
                "NYMERIA_SERVICE_TOKEN is required for internal command execution."
            )
        return cls(_resolve_base_url(), api_key, use_act_as=True)

    @classmethod
    def from_bearer(cls, token: str, *, use_act_as: bool = False) -> "CommandHttpClient":
        return cls(_resolve_base_url(), token, use_act_as=use_act_as)

    async def close(self) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _headers_for(self, act_as: Optional[str]) -> dict[str, str]:
        if not self.use_act_as or not act_as:
            return self._headers
        return {**self._headers, "X-Nymeria-Act-As": act_as}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        act_as: Optional[str] = None,
        timeout: httpx.Timeout | None = None,
    ) -> Any:
        resp = await self._client.request(
            method,
            self._url(path),
            headers=self._headers_for(act_as),
            json=json_body,
            params=params,
            timeout=timeout,
        )
        resp.raise_for_status()
        if resp.status_code == 204:
            return {}
        return resp.json()

    async def _get(self, path: str, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("GET", path, params=params, act_as=act_as)

    async def _post(self, path: str, json: Optional[dict] = None, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("POST", path, json_body=json, params=params, act_as=act_as)

    async def _patch(self, path: str, json: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("PATCH", path, json_body=json, act_as=act_as)

    async def _delete(self, path: str, params: Optional[dict] = None, act_as: Optional[str] = None) -> Any:
        return await self._request("DELETE", path, params=params, act_as=act_as)

    async def get_context_stats(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        return await self._get(f"/threads/{_path_param(thread_id)}/context", act_as=user_id)

    async def get_thread_config(self, thread_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        try:
            return await self._get(f"/threads/{_path_param(thread_id)}/config", act_as=user_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def get_settings(self, user_id: Optional[str] = None) -> dict:
        return await self._get("/settings", act_as=user_id)

    async def get_default_tools(self, user_id: str = "default") -> dict:
        return await self._get("/tools/defaults", params={"user_id": user_id}, act_as=user_id)

    async def get_tool_categories(self) -> dict:
        return await self._get("/tools/categories")

    async def list_memories(self, user_id: str) -> list[dict]:
        data = await self._get(f"/users/{_path_param(user_id)}/memories", act_as=user_id)
        return data.get("memories", [])

    async def save_memory(self, user_id: str, key: str, value: str) -> dict:
        return await self._post(
            f"/users/{_path_param(user_id)}/memories",
            json={"key": key, "value": value},
            act_as=user_id,
        )

    async def forget_memory(self, user_id: str, key: str) -> dict:
        return await self._delete(
            f"/users/{_path_param(user_id)}/memories/{_path_param(key)}",
            act_as=user_id,
        )

    async def search_memories(self, user_id: str, query: str) -> list[dict]:
        data = await self._get(
            f"/users/{_path_param(user_id)}/memories/search",
            params={"q": query},
            act_as=user_id,
        )
        return data.get("results", [])

    async def list_todos(
        self,
        user_id: str,
        *,
        filter_status: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> list[dict]:
        data = await self._get(
            "/todos",
            params=_clean_params(
                user_id=user_id,
                filter_status=filter_status,
                thread_id=thread_id,
            ),
            act_as=user_id,
        )
        return data.get("items", [])

    async def add_todo(
        self,
        user_id: str,
        task: str,
        scheduled_for: str = "1d",
        notes: Optional[str] = None,
        recurrence: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {"task": task, "scheduled_for": scheduled_for}
        if notes:
            body["notes"] = notes
        if recurrence:
            body["recurrence"] = recurrence
        if thread_id:
            body["thread_id"] = thread_id
        return await self._post("/todos", json=body, params={"user_id": user_id}, act_as=user_id)

    async def complete_todo(self, user_id: str, todo_id: str) -> dict:
        return await self._post(
            f"/todos/{_path_param(todo_id)}/complete",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def delete_todo(self, user_id: str, todo_id: str) -> dict:
        return await self._delete(
            f"/todos/{_path_param(todo_id)}",
            params={"user_id": user_id},
            act_as=user_id,
        )

    async def update_settings(self, *, user_id: Optional[str] = None, **kwargs) -> dict:
        return await self._patch("/settings", json=kwargs, act_as=user_id)

    async def update_thread_config(
        self,
        thread_id: str,
        *,
        user_id: Optional[str] = None,
        **kwargs,
    ) -> dict:
        return await self._patch(f"/threads/{_path_param(thread_id)}/config", json=kwargs, act_as=user_id)

    async def get_env_vars(self, *, user_id: Optional[str] = None) -> dict:
        return await self._get("/settings/env", act_as=user_id)

    async def get_env_var(self, key: str, *, user_id: Optional[str] = None) -> dict:
        return await self._get(f"/settings/env/{_path_param(key)}", act_as=user_id)

    async def list_available_models(
        self,
        provider: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict]:
        params = {"provider": provider} if provider else None
        return await self._get("/models/available", params=params, act_as=user_id)


@dataclass(frozen=True)
class _CommandBackendUser:
    id: str
    role: Literal["user", "admin"] = "user"
    email: str = ""
    display_name: str = ""
    via_act_as: bool = False


class CommandBackendClient:
    """In-process command backend adapter.

    The command executor only needs a small API-shaped interface. This adapter
    implements that interface by calling Nymeria managers directly instead of
    sending HTTP requests back into the same API process.
    """

    def __init__(
        self,
        agent: Any,
        *,
        user: _CommandBackendUser,
        settings_fn: Callable[[], Any] = get_settings,
    ) -> None:
        self.agent = agent
        self.user = user
        self.settings_fn = settings_fn

    @classmethod
    def from_context(
        cls,
        ctx: CommandContext,
        *,
        agent: Any | None = None,
        user: Any | None = None,
        settings_fn: Callable[[], Any] | None = None,
    ) -> "CommandBackendClient":
        if agent is None:
            from .agent import get_current_agent

            agent = get_current_agent()
        if agent is None:
            raise RuntimeError("No current NymeriaAgent is available for command execution.")

        if user is not None:
            backend_user = _CommandBackendUser(
                id=user.id,
                role=user.role,
                email=getattr(user, "email", ""),
                display_name=getattr(user, "display_name", ""),
                via_act_as=getattr(user, "via_act_as", False),
            )
        else:
            role: Literal["user", "admin"] = "admin" if ctx.is_admin else "user"
            email = ""
            display_name = ctx.user_id
            try:
                record = agent.accounts_repo.get_user_by_id(ctx.user_id)
            except Exception:  # noqa: BLE001
                record = None
            if record is not None:
                role = record.role
                email = record.email
                display_name = record.display_name
            backend_user = _CommandBackendUser(
                id=ctx.user_id,
                role=role,
                email=email,
                display_name=display_name,
                via_act_as=ctx.via_act_as,
            )
        return cls(agent, user=backend_user, settings_fn=settings_fn or get_settings)

    async def close(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    def _settings(self) -> Any:
        return self.settings_fn()

    def _require_admin(self) -> None:
        if self.user.role != "admin":
            _raise_http_status(403, "Admin only")

    def _require_same_user_or_admin(self, user_id: str) -> None:
        if user_id != self.user.id and self.user.role != "admin":
            _raise_http_status(404, "Not found")

    def _require_thread_access(self, thread_id: str) -> None:
        from .thread_classification import is_shared_channel

        if self.user.role == "admin":
            if is_shared_channel(thread_id):
                return
            self.agent.accounts_repo.claim_thread(thread_id, self.user.id)
            return

        if is_shared_channel(thread_id):
            if self.user.via_act_as:
                return
            _raise_http_status(404, "Not found")

        owner = self.agent.accounts_repo.claim_thread(thread_id, self.user.id)
        if owner != self.user.id:
            _raise_http_status(404, "Not found")

    def _checked_user_id(self, user_id: str) -> str:
        self._require_same_user_or_admin(user_id)
        return user_id

    async def clear_thread(self, thread_id: str) -> dict:
        """Clear conversation history; preserve notepad + tool config."""
        self._require_thread_access(thread_id)
        agent = self.agent
        settings = self._settings()
        user_id = self.user.id

        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = await agent._default_async_graph.aget_state(config)
            messages = state.values.get("messages", [])
            if messages:
                agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning("Pre-clear RAG flush failed for %s: %s", thread_id, e)

        agent.thread_metadata_manager.delete_thread(user_id, thread_id)

        try:
            from .checkpoint_cleanup import delete_thread_checkpoints

            delete_thread_checkpoints(settings, thread_id)
        except Exception as e:
            logger.warning("Failed to delete checkpoints for %s: %s", thread_id, e)

        logger.info("Thread %s conversation cleared (config + notepad preserved)", thread_id)
        return {"status": "ok", "thread_id": thread_id}

    async def restart_api(self) -> dict:
        """Schedule an API server restart. Admin-only."""
        self._require_admin()
        from ..api.routers.system import restart_api_process

        restart_api_process(self.agent, self._settings())
        return {"status": "restarting"}

    async def stop_thread(self, thread_id: str) -> dict:
        """Abort the running turn on a thread; cascades to callable children."""
        self._require_thread_access(thread_id)
        thread_locks = getattr(self.agent, "_thread_locks", None)
        lock_info = thread_locks.get_lock_info(thread_id) if thread_locks else None
        if lock_info:
            self.agent.abort_with_cascade(thread_id)
            return {
                "status": "stopping",
                "thread_id": thread_id,
                "holder": lock_info.get("holder"),
                "held_seconds": lock_info.get("held_seconds", 0),
            }
        return {
            "status": "idle",
            "thread_id": thread_id,
        }

    async def get_context_stats(self, thread_id: str, user_id: Optional[str] = None) -> dict:
        self._require_thread_access(thread_id)
        stats = dict(self.agent.get_context_stats(thread_id) or {})
        thread_locks = getattr(self.agent, "_thread_locks", None)
        if thread_locks is not None:
            try:
                stats["processing"] = thread_locks.get_lock_info(thread_id) is not None
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to inspect processing state for %s: %s", thread_id, e)
                stats["processing"] = False
        return stats

    async def get_thread_config(self, thread_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        self._require_thread_access(thread_id)
        from ..api.routers.thread_config import (
            _config_response,
            _default_thread_config_response,
        )

        tc = self.agent.thread_config_manager.get_config(thread_id)
        if tc:
            return _config_response(tc)
        return _default_thread_config_response(thread_id)

    async def get_settings(self, user_id: Optional[str] = None) -> dict:
        from ..api.routers.settings import _fallback_model_list

        settings = self._settings()
        return {
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "llm_fast_model": settings.llm_fast_model,
            "llm_fallback_models": _fallback_model_list(settings.llm_fallback_models),
            "llm_temperature": settings.llm_temperature,
            "llm_max_tokens": settings.llm_max_tokens,
            "llm_top_p": settings.llm_top_p,
            "llm_top_k": settings.llm_top_k,
            "llm_frequency_penalty": settings.llm_frequency_penalty,
            "llm_presence_penalty": settings.llm_presence_penalty,
            "llm_reasoning_effort": settings.llm_reasoning_effort,
            "llm_extended_thinking": settings.llm_extended_thinking,
            "llm_use_model_defaults": settings.llm_use_model_defaults,
            "llm_base_url": settings.llm_base_url,
            "openai_api_mode": settings.openai_api_mode,
            "llm_stream_max_retries": settings.llm_stream_max_retries,
            "llm_stream_retry_initial_delay": settings.llm_stream_retry_initial_delay,
            "llm_stream_retry_max_delay": settings.llm_stream_retry_max_delay,
            "context_management": settings.context_management,
            "compact_threshold": settings.compact_threshold,
            "compact_keep_messages": settings.compact_keep_messages,
            "compact_model": settings.compact_model,
            "sliding_window_cycles": settings.sliding_window_cycles,
            "tool_output_max_chars": settings.tool_output_max_chars,
            "log_level": settings.log_level,
            "watchdog_enabled": settings.watchdog_enabled,
            "watchdog_interval_minutes": settings.watchdog_interval_minutes,
            "todo_staleness_minutes": settings.todo_staleness_minutes,
            "activity_retention_hours": settings.activity_retention_hours,
            "tts_provider": settings.tts_provider,
            "tts_base_url": settings.tts_base_url,
            "tts_model": settings.tts_model,
            "tts_voice": settings.tts_voice,
            "tts_output_format": settings.tts_output_format,
            "tts_speed": settings.tts_speed,
            "stt_provider": settings.stt_provider,
            "stt_base_url": settings.stt_base_url,
            "stt_model": settings.stt_model,
            "stt_language": settings.stt_language,
            "voice_default_thread_id": settings.voice_default_thread_id,
        }

    async def get_default_tools(self, user_id: str = "default") -> dict:
        from ..tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_discoverable_optional_tool_names,
        )
        from ..tools.metadata import MCP_SERVER_TOOL_METADATA, get_tool_metadata

        target_user_id = self._checked_user_id(user_id)
        profile = self.agent.profile_manager.get_profile(target_user_id)
        prefs = profile.tool_preferences
        default_set = (
            set(prefs.default_thread_tools)
            if prefs.default_thread_tools is not None
            else {t.name for t in ALL_TOOLS}
        )

        tools_out = []
        seen = set()
        visible_optional = filter_discoverable_optional_tool_names(
            OPTIONAL_TOOLS.keys(),
            self.user.role,
        )
        for t in ALL_TOOLS:
            meta = get_tool_metadata(t.name)
            tools_out.append({
                "name": t.name,
                "description": t.description,
                "category": meta.category.value if meta else "core",
                "security_level": meta.security_level.value if meta else "moderate",
                "is_optional": False,
                "is_default": t.name in default_set,
            })
            seen.add(t.name)
        for name, tool in OPTIONAL_TOOLS.items():
            if name in visible_optional and name not in seen:
                meta = get_tool_metadata(name)
                tools_out.append({
                    "name": name,
                    "description": tool.description,
                    "category": meta.category.value if meta else "unknown",
                    "security_level": meta.security_level.value if meta else "moderate",
                    "is_optional": True,
                    "is_default": name in default_set,
                })
                seen.add(name)
        for name, meta in MCP_SERVER_TOOL_METADATA.items():
            if name not in seen:
                tools_out.append({
                    "name": name,
                    "description": meta.description,
                    "category": "mcp_server",
                    "security_level": "moderate",
                    "is_optional": True,
                    "is_default": name in default_set,
                })
                seen.add(name)

        owned = set(self.agent.accounts_repo.list_threads_for_user(target_user_id))
        callable_count = len(
            self.agent.thread_config_manager.list_callable_threads(
                owned_thread_ids=owned,
            )
        )
        return {
            "mode": "custom",
            "default_tools": sorted(default_set),
            "available_tools": tools_out,
            "callable_thread_count": callable_count,
        }

    async def get_tool_categories(self) -> dict:
        from ..tools import filter_discoverable_optional_tool_names
        from ..tools.metadata import get_category_tools_summary

        categories = get_category_tools_summary()
        visible_names = filter_discoverable_optional_tool_names(
            {name for names in categories.values() for name in names},
            self.user.role,
        )
        return {
            "categories": {
                category: [name for name in names if name in visible_names]
                for category, names in categories.items()
            }
        }

    async def list_memories(self, user_id: str) -> list[dict]:
        target_user_id = self._checked_user_id(user_id)
        profile = self.agent.profile_manager.get_profile(target_user_id)
        return [
            {
                "key": m.key,
                "value": m.value,
                "created_at": m.created_at.isoformat(),
                "accessed_at": m.accessed_at.isoformat(),
                "access_count": m.access_count,
            }
            for m in profile.memories
        ]

    def _upsert_memory_rag_chunk(self, user_id: str, key: str, value: str) -> None:
        memory_index = self.agent._get_memory_index(user_id)
        if not memory_index:
            return
        try:
            memory_index.delete_memory_key(user_id, key)
            memory_index.add_chunk(
                content=f"{key}: {value}",
                metadata={"key": key},
                chunk_type="memory",
                user_id=user_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to sync memory '%s' into RAG index: %s", key, e)

    def _delete_memory_rag_chunk(self, user_id: str, key: str) -> None:
        memory_index = self.agent._get_memory_index(user_id)
        if not memory_index:
            return
        try:
            memory_index.delete_memory_key(user_id, key)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to remove memory '%s' from RAG index: %s", key, e)

    async def save_memory(self, user_id: str, key: str, value: str) -> dict:
        target_user_id = self._checked_user_id(user_id)
        with self.agent.profile_manager.atomic_update(target_user_id) as profile:
            success = profile.add_memory(key, value)
            max_memories = profile.MAX_MEMORIES
        if not success:
            _raise_http_status(400, f"Memory limit reached ({max_memories})")
        self._upsert_memory_rag_chunk(target_user_id, key, value)
        return {"status": "ok", "key": key}

    async def forget_memory(self, user_id: str, key: str) -> dict:
        target_user_id = self._checked_user_id(user_id)
        with self.agent.profile_manager.atomic_update(target_user_id) as profile:
            removed = profile.remove_memory(key)
        if not removed:
            _raise_http_status(404, f"No memory with key '{key}'")
        self._delete_memory_rag_chunk(target_user_id, key)
        return {"status": "ok", "key": key}

    async def search_memories(self, user_id: str, query: str) -> list[dict]:
        target_user_id = self._checked_user_id(user_id)
        profile = self.agent.profile_manager.get_profile(target_user_id)
        return [
            {"key": m.key, "value": m.value, "access_count": m.access_count}
            for m in profile.search_memories(query)
        ]

    async def list_todos(
        self,
        user_id: str,
        *,
        filter_status: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> list[dict]:
        from ..api.routers.todos import STATUS_ORDER, _todo_to_response
        from .todo_manager import TodoManager, TodoStatus

        target_user_id = self._checked_user_id(user_id)
        todo_list = TodoManager(self._settings().data_dir).get_todos(target_user_id)
        if filter_status == "all":
            items = todo_list.items
        elif filter_status:
            try:
                status = TodoStatus(filter_status.lower())
            except ValueError:
                _raise_http_status(400, f"Invalid status filter '{filter_status}'")
            items = [i for i in todo_list.items if i.status == status]
        else:
            items = todo_list.get_active_todos()
        if thread_id:
            items = [i for i in items if i.thread_id == thread_id]
        sorted_items = sorted(
            items,
            key=lambda i: (STATUS_ORDER.get(i.status, 3), i.created_at),
        )
        return [_todo_to_response(item).model_dump(mode="json") for item in sorted_items]

    async def add_todo(
        self,
        user_id: str,
        task: str,
        scheduled_for: str = "1d",
        notes: Optional[str] = None,
        recurrence: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> dict:
        from ..api.routers.todos import (
            VALID_RECURRENCES,
            _get_todo_schedule_db,
            _parse_scheduled_for,
            _todo_to_response,
        )
        from .todo_manager import TodoManager

        target_user_id = self._checked_user_id(user_id)
        settings = self._settings()
        todo_manager = TodoManager(settings.data_dir)
        if recurrence and recurrence.lower() not in VALID_RECURRENCES:
            _raise_http_status(
                400,
                f"Invalid recurrence: '{recurrence}'. Use: {', '.join(VALID_RECURRENCES)}",
            )
        try:
            parsed_schedule = _parse_scheduled_for(scheduled_for)
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", 400)
            detail = getattr(exc, "detail", str(exc))
            _raise_http_status(status, str(detail))
        todo_thread_id = thread_id or f"default-{target_user_id}"
        with todo_manager.atomic_update(target_user_id) as todo_list:
            item = todo_list.add_item(
                task=task,
                notes=notes,
                scheduled_for=parsed_schedule,
                thread_id=todo_thread_id,
                created_by="user",
                recurrence=recurrence.lower() if recurrence else None,
            )
            if item is None:
                _raise_http_status(400, "Cannot create TODO: maximum limit reached")
            created_item = item
        if created_item.scheduled_for:
            schedule_db = _get_todo_schedule_db(settings)
            todo_manager.sync_schedule_to_db(target_user_id, created_item.id, schedule_db)
        return _todo_to_response(created_item).model_dump(mode="json")

    async def complete_todo(self, user_id: str, todo_id: str) -> dict:
        from ..api.routers.todos import (
            _get_todo_schedule_db,
            _raise_if_todo_executing,
            _recurrence_anchor,
            _todo_to_response,
        )
        from .todo_constants import calculate_next_recurrence_time
        from .todo_manager import TodoManager, TodoStatus

        target_user_id = self._checked_user_id(user_id)
        settings = self._settings()
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)
        existing = todo_manager.get_todos(target_user_id).get_item(todo_id)
        if not existing:
            _raise_http_status(404, f"TODO '{todo_id}' not found")
        try:
            _raise_if_todo_executing(schedule_db, todo_id, target_user_id)
        except Exception as exc:  # noqa: BLE001
            _raise_http_status(getattr(exc, "status_code", 409), str(getattr(exc, "detail", exc)))
        with todo_manager.atomic_update(target_user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            has_recurrence = item.recurrence
            if not todo_list.complete_item(todo_id):
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            if has_recurrence:
                recurrence_anchor = _recurrence_anchor(item)
                next_execution = calculate_next_recurrence_time(
                    has_recurrence,
                    recurrence_anchor,
                )
                if next_execution:
                    todo_list.update_item(
                        todo_id,
                        scheduled_for=next_execution,
                        status=TodoStatus.PENDING,
                    )
                    refreshed = todo_list.get_item(todo_id)
                    if refreshed:
                        refreshed.last_execution = recurrence_anchor
            item = todo_list.get_item(todo_id)
            if has_recurrence and item and item.scheduled_for:
                todo_manager.sync_schedule_to_db(target_user_id, todo_id, schedule_db)
            else:
                schedule_db.remove_scheduled(todo_id)
            return _todo_to_response(item).model_dump(mode="json")

    async def delete_todo(self, user_id: str, todo_id: str) -> dict:
        from ..api.routers.todos import _get_todo_schedule_db, _raise_if_todo_executing
        from .todo_manager import TodoManager

        target_user_id = self._checked_user_id(user_id)
        settings = self._settings()
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)
        existing = todo_manager.get_todos(target_user_id).get_item(todo_id)
        if not existing:
            _raise_http_status(404, f"TODO '{todo_id}' not found")
        try:
            _raise_if_todo_executing(schedule_db, todo_id, target_user_id)
        except Exception as exc:  # noqa: BLE001
            _raise_http_status(getattr(exc, "status_code", 409), str(getattr(exc, "detail", exc)))
        with todo_manager.atomic_update(target_user_id) as todo_list:
            if not todo_list.get_item(todo_id):
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            if not todo_list.delete_item(todo_id):
                _raise_http_status(404, f"TODO '{todo_id}' not found")
            schedule_db.remove_scheduled(todo_id)
        return {"status": "ok", "deleted_id": todo_id}

    async def update_settings(self, *, user_id: Optional[str] = None, **kwargs) -> dict:
        self._require_admin()
        from ..api.routers.settings import (
            _clear_settings_cache,
            _env_mapping,
            _restart_required_keys,
            _sync_process_env,
        )
        from ..api.schemas.settings import ServerSettingsUpdate
        from ..config.settings import get_env_write_path

        settings = self._settings()
        updates = ServerSettingsUpdate(**kwargs)
        updates_dict = {
            key: value for key, value in updates.model_dump().items() if value is not None
        }
        if not updates_dict:
            return {"message": "No updates provided", "restart_required": False}

        env_path = get_env_write_path(settings.project_root)
        existing_lines = (
            env_path.read_text(encoding="utf-8").splitlines()
            if env_path.exists()
            else []
        )
        env_mapping = _env_mapping()
        updated_vars = set()
        new_lines = []
        for line in existing_lines:
            updated = False
            for setting_name, env_var in env_mapping.items():
                if setting_name in updates_dict and line.startswith(f"{env_var}="):
                    value = updates_dict[setting_name]
                    if isinstance(value, bool):
                        value = str(value).lower()
                    new_lines.append(f"{env_var}={value}")
                    updated_vars.add(setting_name)
                    updated = True
                    break
            if not updated:
                new_lines.append(line)

        for setting_name, value in updates_dict.items():
            if setting_name not in updated_vars:
                env_var = env_mapping.get(setting_name)
                if env_var:
                    if isinstance(value, bool):
                        value = str(value).lower()
                    new_lines.append(f"{env_var}={value}")

        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        _sync_process_env(new_lines, set(env_mapping.values()))
        _clear_settings_cache(self.settings_fn)
        new_settings = self.settings_fn()
        self.agent.settings = new_settings

        llm_fields = {
            "llm_provider",
            "llm_model",
            "llm_fast_model",
            "llm_fallback_models",
            "llm_temperature",
            "llm_max_tokens",
            "llm_top_p",
            "llm_top_k",
            "llm_frequency_penalty",
            "llm_presence_penalty",
            "llm_reasoning_effort",
            "llm_extended_thinking",
            "llm_use_model_defaults",
            "llm_base_url",
            "openai_api_mode",
            "llm_stream_max_retries",
            "llm_stream_retry_initial_delay",
            "llm_stream_retry_max_delay",
        }
        llm_credential_fields = {
            "anthropic_api_key",
            "anthropic_direct_api_key",
            "openai_api_key",
            "openrouter_api_key",
        }
        graph_fields = llm_fields | {"tool_output_max_chars"} | llm_credential_fields
        if graph_fields & set(updates_dict):
            with self.agent._graph_cache_lock:
                self.agent._user_graphs.clear()
                self.agent._async_user_graphs.clear()
            self.agent._default_graph = self.agent._build_graph_with_prompt(
                self.agent._base_system_prompt
            )
            self.agent._default_async_graph = self.agent._build_async_graph_with_prompt(
                self.agent._base_system_prompt
            )

        needs_restart = bool(_restart_required_keys() & set(updates_dict))
        return {
            "message": "Settings updated and applied" + (
                " (some changes require /restart api to take effect)"
                if needs_restart
                else ""
            ),
            "updated": list(updates_dict.keys()),
            "restart_required": needs_restart,
        }

    async def update_thread_config(
        self,
        thread_id: str,
        *,
        user_id: Optional[str] = None,
        **kwargs,
    ) -> dict:
        from .thread_config import ThreadConfig, ThreadLLMConfig

        self._require_thread_access(thread_id)
        if user_id is not None:
            self._checked_user_id(user_id)

        tc = self.agent.thread_config_manager.get_config(thread_id)
        if tc is None:
            tc = ThreadConfig(thread_id=thread_id)

        if "enabled_tools" in kwargs and kwargs["enabled_tools"] is not None:
            enabled_tools = list(kwargs["enabled_tools"])
            if self.user.role != "admin":
                from ..tools import (
                    ADMIN_ONLY_OPTIONAL_TOOL_NAMES,
                    DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
                )

                blocked = ADMIN_ONLY_OPTIONAL_TOOL_NAMES.intersection(enabled_tools)
                if blocked:
                    _raise_http_status(
                        403,
                        "Admin-only tools cannot be enabled by this user: "
                        f"{sorted(blocked)}",
                    )
                blocked = DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES.intersection(enabled_tools)
                if blocked:
                    _raise_http_status(
                        403,
                        "Developer-only diagnostic tools cannot be enabled by this user: "
                        f"{sorted(blocked)}",
                    )
            tc.enabled_tools = enabled_tools
        if "disabled_tools" in kwargs and kwargs["disabled_tools"] is not None:
            tc.disabled_tools = list(kwargs["disabled_tools"])
        if "llm_config" in kwargs and kwargs["llm_config"] is not None:
            llm_data = dict(kwargs["llm_config"])
            if tc.llm_config is None:
                tc.llm_config = ThreadLLMConfig(
                    **{key: value for key, value in llm_data.items() if value is not None}
                )
            else:
                for key, value in llm_data.items():
                    setattr(tc.llm_config, key, value)

        if not self.agent.thread_config_manager.save_config(tc):
            _raise_http_status(500, "Failed to save thread config")
        self.agent.invalidate_thread_config_cache(thread_id)
        return tc.model_dump(mode="json") | {"has_customizations": tc.has_customizations()}

    async def get_env_vars(self, *, user_id: Optional[str] = None) -> dict:
        self._require_admin()
        from ..api.routers.settings import _env_categories, _mask_value, _secret_keys
        from ..api.schemas.settings import HIDDEN_CONFIG_SETTINGS

        settings = self._settings()
        entries = []
        secret_keys = _secret_keys()
        for category, keys in _env_categories().items():
            for key in keys:
                if key in HIDDEN_CONFIG_SETTINGS:
                    continue
                val = getattr(settings, key, None)
                is_secret = key in secret_keys
                display_val = None
                if val is not None:
                    display_val = _mask_value(str(val)) if is_secret else str(val)
                entries.append({
                    "name": key,
                    "env_var": key.upper(),
                    "value": display_val,
                    "is_set": val is not None and str(val) != "",
                    "is_secret": is_secret,
                    "category": category,
                })
        return {"entries": entries}

    async def get_env_var(self, key: str, *, user_id: Optional[str] = None) -> dict:
        self._require_admin()
        from ..api.schemas.settings import HIDDEN_CONFIG_SETTINGS

        settings = self._settings()
        key_lower = key.lower()
        if key_lower in HIDDEN_CONFIG_SETTINGS:
            _raise_http_status(404, f"Unknown setting: {key}")
        val = getattr(settings, key, None)
        if val is None:
            val = getattr(settings, key_lower, None)
            if val is None:
                _raise_http_status(404, f"Unknown setting: {key}")
            key = key_lower
        return {
            "name": key,
            "env_var": key.upper(),
            "value": str(val) if val is not None else None,
        }

    async def list_available_models(
        self,
        provider: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict]:
        from ..config.llm_providers import (
            is_openai_compatible_provider,
            normalize_llm_provider,
            provider_requires_api_key,
            resolve_provider_api_key,
            resolve_provider_base_url,
        )
        from ..config.model_capabilities import register_model_metadata
        from .llm_credentials import get_llm_provider_credential
        from .llm_provider_utils import base_url_allows_no_api_key, extract_model_metadata

        settings = self._settings()
        effective_provider = normalize_llm_provider(provider or settings.llm_provider)
        credential = get_llm_provider_credential(
            effective_provider,
            vault=getattr(self.agent, "credential_vault", None),
            owner_user_id=self.user.id,
        )
        api_key = credential.api_key if credential else None
        effective_base_url = None
        if (
            not effective_base_url
            and effective_provider == normalize_llm_provider(settings.llm_provider)
        ):
            effective_base_url = settings.llm_base_url
        if not effective_base_url and credential and credential.base_url:
            effective_base_url = credential.base_url

        if effective_provider == "anthropic":
            api_key = api_key or (
                settings.anthropic_direct_api_key or settings.anthropic_api_key
            )
            effective_base_url = effective_base_url or "https://api.anthropic.com"
            clean_base = effective_base_url.rstrip("/")
            models_url = (
                f"{clean_base}/models"
                if clean_base.endswith("/v1")
                else f"{clean_base}/v1/models"
            )
        elif is_openai_compatible_provider(effective_provider):
            api_key = api_key or resolve_provider_api_key(
                effective_provider,
                settings=settings,
            )
            effective_base_url = effective_base_url or resolve_provider_base_url(
                effective_provider,
                settings=settings,
            )
            if not effective_base_url:
                return []
            models_url = f"{effective_base_url.rstrip('/')}/models"
        else:
            return []

        if (
            not api_key
            and provider_requires_api_key(effective_provider)
            and not base_url_allows_no_api_key(effective_base_url)
        ):
            return []
        if not api_key:
            api_key = "not-needed"

        headers = (
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
            if effective_provider == "anthropic"
            else {"Authorization": f"Bearer {api_key}"}
        )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(models_url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            result = []
            for model in sorted(data.get("data", []), key=lambda item: item.get("id", "")):
                model_id = model.get("id", "")
                if not model_id:
                    continue
                model_name = model.get("name") or model_id
                metadata = extract_model_metadata(model)
                register_model_metadata(
                    model_id=model_id,
                    name=model_name,
                    context_length=metadata["context_length"],
                    max_completion_tokens=metadata["max_completion_tokens"],
                    input_modalities=set(metadata["input_modalities"]),
                    supported_parameters=set(metadata["supported_parameters"]),
                    default_temperature=metadata["default_temperature"],
                    default_top_p=metadata["default_top_p"],
                    default_frequency_penalty=metadata["default_frequency_penalty"],
                    pricing_prompt=metadata["pricing_prompt"],
                    pricing_completion=metadata["pricing_completion"],
                    tokenizer=metadata["tokenizer"],
                )
                result.append({
                    "id": model_id,
                    "name": model_name,
                    "owned_by": model.get("owned_by", ""),
                    "created": model.get("created"),
                    **metadata,
                })
            return result
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to fetch models from %s: %s", models_url, e)
            return []


class CommandService:
    """Registry and dispatcher for Nymeria slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, CommandDefinition] = {}
        self._path_index: dict[tuple[str, ...], str] = {}
        self._aliases: dict[tuple[str, ...], str] = {}
        self._register_defaults()
        self.validate_registry()

    def register(
        self,
        name: str,
        handler: Any = None,
        *,
        description: str,
        category: str,
        id: str | None = None,
        usage: str | None = None,
        aliases: tuple[str | tuple[str, ...], ...] = (),
        subcommands: tuple[str, ...] = (),
        scope: CommandScope = "global",
        surfaces: tuple[CommandSurface, ...] = DEFAULT_GLOBAL_SURFACES,
        agent_allowed: bool = True,
        requires_thread: bool = False,
        requires_admin: bool = False,
        mutates_state: bool = False,
        danger_level: CommandDangerLevel = "safe",
        execution_kind: CommandExecutionKind = "command",
        note: str | None = None,
        hidden: bool = False,
    ) -> None:
        del handler
        path = _normalize_path(name)
        if not path:
            raise ValueError("Command path cannot be empty")
        command_id = (id or _id_for_path(path)).lower()
        if command_id in self._commands:
            raise ValueError(f"Duplicate command id: {command_id}")
        if path in self._path_index:
            existing = self._path_index[path]
            raise ValueError(
                f"Duplicate command path: /{_display_path(path)} "
                f"({existing} and {command_id})"
            )

        alias_paths = tuple(_normalize_path(alias) for alias in aliases)
        for alias_path in alias_paths:
            if not alias_path:
                raise ValueError(f"Empty alias for command {command_id}")
            existing_path_id = self._path_index.get(alias_path)
            if existing_path_id and existing_path_id != command_id:
                raise ValueError(
                    f"Alias /{_display_path(alias_path)} for {command_id} "
                    f"conflicts with command path {existing_path_id}"
                )
            existing_alias_id = self._aliases.get(alias_path)
            if existing_alias_id and existing_alias_id != command_id:
                raise ValueError(
                    f"Alias /{_display_path(alias_path)} for {command_id} "
                    f"already points to {existing_alias_id}"
                )

        definition = CommandDefinition(
            id=command_id,
            path=path,
            description=description,
            usage=usage or _usage_for_path(path),
            category=category,
            aliases=alias_paths,
            subcommands=subcommands,
            scope=scope,
            surfaces=surfaces,
            agent_allowed=agent_allowed,
            requires_thread=requires_thread,
            requires_admin=requires_admin,
            mutates_state=mutates_state,
            danger_level=danger_level,
            execution_kind=execution_kind,
            note=note,
            hidden=hidden,
        )
        self._commands[command_id] = definition
        self._path_index[path] = command_id
        for alias_path in alias_paths:
            self._aliases[alias_path] = command_id

    def _register_defaults(self) -> None:
        self.register("help", description="Show available commands", category="General", aliases=("h",))
        self.register(
            "status",
            description="Model, context, tools, and task summary",
            category="Status",
            requires_thread=True,
        )
        self.register(
            "thread",
            description="Show active thread context usage",
            category="Status",
            requires_thread=True,
        )
        self.register(
            "context",
            description="Detailed context and tool breakdown",
            category="Status",
            requires_thread=True,
        )
        self.register(
            "tasks",
            description="Scheduled tasks overview",
            category="TODOs",
            usage="/tasks [active|pending|in_progress|done|all]",
        )
        self.register(
            "model",
            description="Show or change the model",
            category="LLM",
            usage="/model [name] [global|thread]",
            mutates_state=True,
            danger_level="normal",
        )
        self.register("models", description="List available provider models", category="LLM")
        self.register(
            "think",
            description="Show or change thinking mode",
            category="LLM",
            usage="/think [off|on|low|medium|high]",
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "config show",
            description="Show server settings",
            category="Settings",
            aliases=("config_show",),
        )
        self.register(
            "config get",
            description="Show one server setting",
            category="Settings",
            usage="/config get <key>",
            aliases=("config_get",),
        )
        self.register(
            "config set",
            description="Change a server setting",
            category="Settings",
            usage="/config set <key> <value>",
            aliases=("config_set",),
            requires_admin=True,
            mutates_state=True,
            danger_level="dangerous",
        )
        self.register(
            "env show",
            description="Show environment variables",
            category="Settings",
            aliases=("env_show",),
            requires_admin=True,
        )
        self.register(
            "env get",
            description="Show one unmasked environment variable",
            category="Settings",
            usage="/env get <key>",
            aliases=("env_get",),
            requires_admin=True,
        )
        self.register(
            "env set",
            description="Change an environment variable",
            category="Settings",
            usage="/env set <key> <value>",
            aliases=("env_set",),
            requires_admin=True,
            mutates_state=True,
            danger_level="dangerous",
        )
        self.register(
            "tools core",
            description="Show core tools",
            category="Tools",
            aliases=("tools_core", "tools list_core"),
        )
        self.register(
            "tools optional",
            description="Show optional tools",
            category="Tools",
            aliases=("tools_optional",),
            requires_thread=True,
        )
        self.register(
            "tools enabled",
            description="Show enabled tools for this thread",
            category="Tools",
            aliases=("tools_enabled",),
            requires_thread=True,
        )
        self.register(
            "tools category",
            description="Show tools in a category",
            category="Tools",
            usage="/tools category <name>",
            aliases=("tools_category",),
            requires_thread=True,
        )
        self.register(
            "tools enable",
            description="Enable a tool or category on this thread",
            category="Tools",
            usage="/tools enable <tool_or_category>",
            aliases=("tools_enable",),
            requires_thread=True,
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "tools disable",
            description="Disable a tool or category on this thread",
            category="Tools",
            usage="/tools disable <tool_or_category>",
            aliases=("tools_disable",),
            requires_thread=True,
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "skill",
            description="Use a markdown-only skill with an optional prompt",
            category="Skills",
            usage="/skill <name> [prompt]",
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "kit",
            description="Use a Skill Kit with optional TTL and prompt",
            category="Skills",
            usage="/kit <name> [ttl] [prompt]",
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "skills",
            description="List skills visible on this thread",
            category="Skills",
            usage="/skills",
            requires_thread=True,
        )
        self.register(
            "skills list",
            description="List skills visible on this thread",
            category="Skills",
            usage="/skills list",
            aliases=("skills_list",),
            requires_thread=True,
        )
        self.register(
            "skills show",
            description="Show a skill's markdown body without activating it",
            category="Skills",
            usage="/skills show <name>",
            aliases=("skills_show",),
        )
        self.register(
            "skills off all",
            description="Deactivate every visible skill active on this thread",
            category="Skills",
            usage="/skills off all",
            aliases=("skills_off_all",),
            requires_thread=True,
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "skills search",
            description="Search a skills marketplace for installable skills",
            category="Skills",
            usage="/skills search [query] [--source <source>]",
            aliases=("skills_search",),
        )
        self.register(
            "skills install",
            description="Install a skill from a marketplace",
            category="Skills",
            usage="/skills install <name> [--source <source>] [--scope user|global]",
            aliases=("skills_install",),
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "skills enable",
            description="Enable a skill on this thread (default) or globally",
            category="Skills",
            usage="/skills enable [--global] <name>",
            aliases=("skills_enable",),
            mutates_state=True,
        )
        self.register(
            "skills disable",
            description="Disable a skill on this thread (default) or globally",
            category="Skills",
            usage="/skills disable [--global] <name>",
            aliases=("skills_disable",),
            mutates_state=True,
        )
        self.register(
            "skills inspect",
            description="Show full skill details (metadata, scope, tools, references)",
            category="Skills",
            usage="/skills inspect <name>",
            aliases=("skills_inspect",),
        )
        self.register(
            "mcp",
            description="MCP server management commands",
            category="MCP",
            usage="/mcp list|status|logs|discover|test|remove|retry [...]",
            requires_admin=True,
        )
        self.register(
            "mcp list",
            description="List configured MCP servers",
            category="MCP",
            usage="/mcp list",
            aliases=("mcp_list",),
            requires_admin=True,
        )
        self.register(
            "mcp status",
            description="Show MCP server status, with errors if any",
            category="MCP",
            usage="/mcp status [server-id]",
            aliases=("mcp_status",),
            requires_admin=True,
        )
        self.register(
            "mcp logs",
            description="Show install logs for an MCP server",
            category="MCP",
            usage="/mcp logs <server-id> [limit]",
            aliases=("mcp_logs",),
            requires_admin=True,
        )
        self.register(
            "mcp discover",
            description="Force tool rediscovery for an MCP server",
            category="MCP",
            usage="/mcp discover <server-id>",
            aliases=("mcp_discover",),
            requires_admin=True,
            mutates_state=True,
        )
        self.register(
            "mcp test",
            description="Test connectivity to an MCP server",
            category="MCP",
            usage="/mcp test <server-id>",
            aliases=("mcp_test",),
            requires_admin=True,
        )
        self.register(
            "mcp remove",
            description="Remove an MCP server and its tools",
            category="MCP",
            usage="/mcp remove <server-id>",
            aliases=("mcp_remove", "mcp_rm", "mcp_delete"),
            requires_admin=True,
            mutates_state=True,
            danger_level="dangerous",
        )
        self.register(
            "mcp retry",
            description="Retry setup for a draft or failed MCP server",
            category="MCP",
            usage="/mcp retry <server-id>",
            aliases=("mcp_retry",),
            requires_admin=True,
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "triggers",
            description="Event-trigger automation commands",
            category="Automation",
            usage="/triggers list|enable|disable|delete|history [...]",
        )
        self.register(
            "triggers list",
            description="List configured event triggers",
            category="Automation",
            usage="/triggers list [--enabled-only] [--thread <id>]",
            aliases=("triggers_list",),
        )
        self.register(
            "triggers enable",
            description="Enable an event trigger",
            category="Automation",
            usage="/triggers enable <trigger-id>",
            aliases=("triggers_enable",),
            mutates_state=True,
        )
        self.register(
            "triggers disable",
            description="Disable an event trigger",
            category="Automation",
            usage="/triggers disable <trigger-id>",
            aliases=("triggers_disable",),
            mutates_state=True,
        )
        self.register(
            "triggers delete",
            description="Delete an event trigger permanently",
            category="Automation",
            usage="/triggers delete <trigger-id>",
            aliases=("triggers_delete",),
            mutates_state=True,
            danger_level="dangerous",
        )
        self.register(
            "triggers history",
            description="Show recent trigger execution history",
            category="Automation",
            usage="/triggers history [trigger-id] [--limit N]",
            aliases=("triggers_history",),
        )
        self.register(
            "account",
            description="Inspect account, tokens, and linked platforms",
            category="Personal",
            usage="/account current|tokens|platforms",
            aliases=("acct",),
        )
        self.register(
            "account current",
            description="Show details for the current user",
            category="Personal",
            usage="/account current",
            aliases=("account_current", "account_me"),
        )
        self.register(
            "account tokens",
            description="List API tokens for the current user",
            category="Personal",
            usage="/account tokens",
            aliases=("account_tokens",),
        )
        self.register(
            "account tokens issue",
            description="Issue a new API token for the current user",
            category="Personal",
            usage="/account tokens issue [label]",
            aliases=("account_tokens_issue",),
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "account tokens revoke",
            description="Revoke an API token by hash prefix",
            category="Personal",
            usage="/account tokens revoke <hash-prefix>",
            aliases=("account_tokens_revoke",),
            mutates_state=True,
            danger_level="dangerous",
        )
        self.register(
            "account platforms",
            description="List chat platforms linked to the current user",
            category="Personal",
            usage="/account platforms",
            aliases=("account_platforms",),
        )
        self.register(
            "activity",
            description="Show recent activity and notifications",
            category="Personal",
            usage="/activity list|notifications",
        )
        self.register(
            "activity list",
            description="Show the most recent activity entries",
            category="Personal",
            usage="/activity list [limit] [--type TYPE] [--thread ID]",
            aliases=("activity_list", "activity_recent"),
        )
        self.register(
            "activity notifications",
            description="Show notifications and unread count",
            category="Personal",
            usage="/activity notifications",
            aliases=("activity_notifications",),
        )
        self.register(
            "doctor",
            description="Run server-side diagnostics (auth + model)",
            category="System",
            usage="/doctor [auth|model]",
        )
        self.register(
            "doctor auth",
            description="Show the resolved identity for the current request",
            category="System",
            usage="/doctor auth",
            aliases=("doctor_auth",),
        )
        self.register(
            "doctor model",
            description="Show LLM provider/model diagnostics",
            category="System",
            usage="/doctor model",
            aliases=("doctor_model",),
        )
        self.register(
            "memory list",
            description="List saved memories",
            category="Memory",
            aliases=("memory_list",),
        )
        self.register(
            "memory save",
            description="Save a memory",
            category="Memory",
            usage="/memory save <key> <value>",
            aliases=("memory_save",),
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "memory forget",
            description="Forget a memory",
            category="Memory",
            usage="/memory forget <key>",
            aliases=("memory_forget",),
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "memory search",
            description="Search saved memories",
            category="Memory",
            usage="/memory search <query>",
            aliases=("memory_search",),
        )
        self.register(
            "todos list",
            description="List TODOs",
            category="TODOs",
            usage="/todos list [active|pending|in_progress|done|all]",
            aliases=("todos_list",),
        )
        self.register(
            "todos add",
            description="Add a TODO",
            category="TODOs",
            usage="/todos add <task> [| <schedule>] [| <repeat>] [| <notes>]",
            aliases=("todos_add",),
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "todos complete",
            description="Complete a TODO",
            category="TODOs",
            usage="/todos complete <todo_id>",
            aliases=("todos_complete",),
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "todos delete",
            description="Delete a TODO",
            category="TODOs",
            usage="/todos delete <todo_id>",
            aliases=("todos_delete",),
            mutates_state=True,
            danger_level="dangerous",
        )
        self.register(
            "notepad read",
            description="Read this thread's notepad",
            category="Thread",
            aliases=("notepad_read",),
            requires_thread=True,
        )
        self.register(
            "notepad write",
            description="Write this thread's notepad",
            category="Thread",
            usage="/notepad write [append:|replace:]<content>",
            aliases=("notepad_write",),
            requires_thread=True,
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "notepad clear",
            description="Clear this thread's notepad",
            category="Thread",
            aliases=("notepad_clear",),
            requires_thread=True,
            mutates_state=True,
            danger_level="dangerous",
        )
        self.register(
            "compact",
            description="Compact the active chat context",
            category="Thread",
            usage="/compact",
            agent_allowed=False,
            requires_thread=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "stop",
            description="Abort the running turn on this thread",
            category="Thread",
            usage="/stop",
            agent_allowed=False,
            requires_thread=True,
            mutates_state=True,
            danger_level="normal",
        )
        self.register(
            "clear",
            description="Clear conversation history (preserves notepad + tool config)",
            category="Thread",
            usage="/clear",
            agent_allowed=False,
            requires_thread=True,
            mutates_state=True,
            danger_level="dangerous",
        )
        self.register(
            "restart api",
            description="Restart the API server process (admin-only)",
            category="System",
            usage="/restart api",
            agent_allowed=False,
            requires_admin=True,
            mutates_state=True,
            danger_level="dangerous",
        )
        self.register(
            "orchestrate",
            description=(
                "Decompose a goal into tasks and delegate execution to forked "
                "worker threads. Activates the 'orchestrate' skill kit on this thread."
            ),
            category="Orchestration",
            usage="/orchestrate <objective>",
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "orchestrate clear",
            description="Exit orchestrate mode and evict its tools from this thread.",
            category="Orchestration",
            aliases=("orchestrate_clear",),
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "orchestrate status",
            description="Show orchestrate mode status and active worker threads.",
            category="Orchestration",
            aliases=("orchestrate_status",),
            requires_thread=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "goal",
            description=(
                "Start a supervised goal. The worker (this thread) decomposes "
                "the objective into tasks; a separate supervisor thread holds "
                "the authority to mark tasks complete after the user approves."
            ),
            category="Goals",
            usage="/goal <objective>",
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "goal approve",
            description=(
                "Approve the proposed task list and spawn the supervisor thread."
            ),
            category="Goals",
            aliases=("goal_approve",),
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "goal cancel",
            description="Abort a goal that is still pending approval.",
            category="Goals",
            aliases=("goal_cancel",),
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "goal clear",
            description=(
                "Abort an active goal, deactivate the worker kit, and delete "
                "the supervisor thread."
            ),
            category="Goals",
            aliases=("goal_clear",),
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "goal pause",
            description="Pause an active goal; the loop stops until resumed.",
            category="Goals",
            aliases=("goal_pause",),
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "goal resume",
            description="Resume a paused goal and continue from where it stopped.",
            category="Goals",
            aliases=("goal_resume",),
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "goal status",
            description=(
                "Show the active goal's status, task list, and circuit-breaker counters."
            ),
            category="Goals",
            aliases=("goal_status",),
            requires_thread=True,
            execution_kind="chat_stream",
            note="Handled by the chat stream endpoint.",
        )
        self.register(
            "goal edit",
            description="(reserved) Replace the proposed task list.",
            category="Goals",
            aliases=("goal_edit",),
            requires_thread=True,
            mutates_state=True,
            execution_kind="chat_stream",
            note="Reserved; not yet implemented.",
        )

    def validate_registry(self) -> None:
        seen_ids: set[str] = set()
        seen_paths: dict[tuple[str, ...], str] = {}
        seen_aliases: dict[tuple[str, ...], str] = {}
        for command_id, cmd in self._commands.items():
            if command_id in seen_ids:
                raise ValueError(f"Duplicate command id: {command_id}")
            seen_ids.add(command_id)
            existing_path = seen_paths.get(cmd.path)
            if existing_path and existing_path != command_id:
                raise ValueError(
                    f"Duplicate command path: /{cmd.name} "
                    f"({existing_path} and {command_id})"
                )
            seen_paths[cmd.path] = command_id
            if cmd.path[0] in AGENT_BLOCKED and cmd.agent_allowed:
                raise ValueError(f"Agent-blocked command is agent-allowed: {command_id}")
            if cmd.requires_admin and cmd.danger_level == "dangerous" and not cmd.mutates_state:
                raise ValueError(f"Dangerous admin command must declare mutates_state: {command_id}")
            for alias_path in cmd.aliases:
                path_conflict = seen_paths.get(alias_path)
                if path_conflict and path_conflict != command_id:
                    raise ValueError(
                        f"Alias /{_display_path(alias_path)} for {command_id} "
                        f"conflicts with command path {path_conflict}"
                    )
                alias_conflict = seen_aliases.get(alias_path)
                if alias_conflict and alias_conflict != command_id:
                    raise ValueError(
                        f"Alias /{_display_path(alias_path)} for {command_id} "
                        f"conflicts with alias for {alias_conflict}"
                    )
                seen_aliases[alias_path] = command_id

    def _resolve_agent(self, agent: Any | None = None) -> Any | None:
        if agent is not None:
            return agent
        try:
            from .agent import get_current_agent

            return get_current_agent()
        except Exception:  # noqa: BLE001
            return None

    def _skill_manager(self, agent: Any | None = None) -> Any | None:
        resolved = self._resolve_agent(agent)
        if resolved is None:
            return None
        return getattr(resolved, "skill_manager", None)

    def _visible_slash_skills(
        self,
        user_id: str | None,
        *,
        agent: Any | None = None,
        include_internal: bool = False,
    ) -> list[Any]:
        skill_manager = self._skill_manager(agent)
        if skill_manager is None:
            return []
        try:
            skills = skill_manager.list_installed(user_id=user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to list installed skills for slash commands: %s", e)
            return []
        return [
            skill
            for skill in skills
            if include_internal or not getattr(skill, "is_internal", False)
        ]

    def list_commands(
        self,
        source: str | None = "user",
        *,
        actor: str | None = None,
        surface: str | None = None,
        is_admin: bool | None = None,
        include_hidden: bool = False,
        user_id: str | None = None,
        agent: Any | None = None,
    ) -> list[CommandInfo]:
        effective_actor = (actor or _actor_from_source(source)).lower()
        effective_surface = surface or _surface_from_source(source)
        definitions = list(self._commands.values())
        return [
            self._to_info(cmd)
            for cmd in sorted(definitions, key=lambda c: (c.category, c.name))
            if self._is_visible(
                cmd,
                actor=effective_actor,
                surface=effective_surface,
                is_admin=is_admin,
                include_hidden=include_hidden,
            )
        ]

    def _is_visible(
        self,
        cmd: CommandDefinition,
        *,
        actor: str,
        surface: str | None,
        is_admin: bool | None,
        include_hidden: bool,
    ) -> bool:
        if cmd.hidden and not include_hidden:
            return False
        if actor == "agent" and not cmd.agent_allowed:
            return False
        if surface and surface not in cmd.surfaces:
            return False
        if cmd.requires_admin and is_admin is False:
            return False
        return True

    def _to_info(self, cmd: CommandDefinition) -> CommandInfo:
        return CommandInfo(
            name=cmd.name,
            description=cmd.description,
            usage=cmd.usage,
            category=cmd.category,
            subcommands=list(cmd.subcommands or self._subcommands_for_path(cmd.path)),
            id=cmd.id,
            path=list(cmd.path),
            aliases=[_alias_display(alias) for alias in cmd.aliases],
            scope=cmd.scope,
            surfaces=list(cmd.surfaces),
            agent_allowed=cmd.agent_allowed,
            requires_thread=cmd.requires_thread,
            requires_admin=cmd.requires_admin,
            mutates_state=cmd.mutates_state,
            danger_level=cmd.danger_level,
            execution_kind=cmd.execution_kind,
            note=cmd.note,
        )

    def _subcommands_for_path(self, path: tuple[str, ...]) -> list[str]:
        if len(path) != 1:
            return []
        prefix = path[0]
        return sorted(
            {
                cmd.path[1]
                for cmd in self._commands.values()
                if len(cmd.path) > 1 and cmd.path[0] == prefix
            }
        )

    def _help_markdown(
        self,
        source: str | None,
        *,
        actor: str | None = None,
        surface: str | None = None,
        is_admin: bool | None = None,
        user_id: str | None = None,
        agent: Any | None = None,
    ) -> str:
        commands = self.list_commands(
            source,
            actor=actor,
            surface=surface,
            is_admin=is_admin,
            user_id=user_id,
            agent=agent,
        )
        lines = ["## Nymeria Slash Commands", ""]
        categories = sorted({cmd.category for cmd in commands})
        for category in categories:
            lines.append(f"### {category}")
            lines.append("")
            lines.append("| Command | Usage | Description |")
            lines.append("| --- | --- | --- |")
            for cmd in [c for c in commands if c.category == category]:
                desc = cmd.description
                if cmd.aliases:
                    desc = f"{desc}. Alias: {cmd.aliases[0]}"
                if cmd.note:
                    desc = f"{desc}. {cmd.note}"
                lines.append(f"| `/{cmd.name}` | `{cmd.usage}` | {desc} |")
            lines.append("")
        lines.append("Values with spaces can be quoted, for example `/memory save color \"deep blue\"`.")
        return "\n".join(lines).strip()

    def _parse_for_registry(self, raw: str) -> ParsedCommand:
        command_text = raw.strip()
        if command_text.startswith("/"):
            command_text = command_text[1:].lstrip()
        if not command_text:
            return ParsedCommand((), (), [], "", None)

        try:
            parts = shlex.split(command_text, posix=True)
        except ValueError:
            parts = command_text.split()
        tokens = tuple(_normalize_token(part) for part in parts if _normalize_token(part))
        if not tokens:
            return ParsedCommand((), (), [], "", None)

        max_len = min(len(tokens), max((len(path) for path in self._path_index), default=1))
        for prefix_len in range(max_len, 0, -1):
            candidate = tokens[:prefix_len]
            command_id = self._path_index.get(candidate) or self._aliases.get(candidate)
            if command_id is None:
                continue
            definition = self._commands[command_id]
            rest = _split_rest_after_tokens(command_text, prefix_len)
            return ParsedCommand(
                tokens=tokens,
                path=definition.path,
                args=_split_args(rest),
                rest=rest,
                definition=definition,
                matched_input_len=prefix_len,
            )

        rest = _split_rest_after_tokens(command_text, 1)
        return ParsedCommand(
            tokens=tokens,
            path=(tokens[0],),
            args=_split_args(rest),
            rest=rest,
            definition=None,
            matched_input_len=1,
        )

    def _prefix_subcommands(self, prefix: str) -> list[str]:
        return sorted(
            {
                path[1]
                for path in self._path_index
                if len(path) > 1 and path[0] == prefix
            }
        )

    def _unknown_or_group_error(self, parsed: ParsedCommand) -> CommandResult:
        if not parsed.tokens:
            return CommandResult(False, "**Error:** Empty command. Try `/help`.", "", level="error")

        root = parsed.tokens[0]
        valid_subcommands = self._prefix_subcommands(root)
        if valid_subcommands and len(parsed.tokens) == 1:
            valid = ", ".join(valid_subcommands)
            return CommandResult(
                False,
                f"**Error:** `/{root}` requires a subcommand. Valid: {valid}.",
                root,
                level="error",
            )
        if valid_subcommands and len(parsed.tokens) > 1:
            valid = ", ".join(valid_subcommands)
            command_label = f"{root} {parsed.tokens[1]}".strip()
            return CommandResult(
                False,
                f"**Error:** Unknown subcommand `{parsed.tokens[1]}` for `/{root}`. Valid: {valid}.",
                command_label,
                level="error",
            )

        return CommandResult(
            False,
            f"**Error:** Unknown command `/{root}`. Use `/help`.",
            root,
            level="error",
        )

    async def execute(
        self,
        ctx: CommandContext,
        raw_command: str,
        *,
        api: Any | None = None,
    ) -> CommandResult:
        parsed = self._parse_for_registry(raw_command)
        if parsed.definition is None:
            return self._unknown_or_group_error(parsed)

        definition = parsed.definition
        command_label = definition.name
        actor = ctx.effective_actor

        if actor == "agent" and definition.path[0] in AGENT_BLOCKED:
            return CommandResult(
                False,
                (
                    f"**Error:** Command `/{definition.path[0]}` is disabled for the agent "
                    "because it would interrupt or destroy the current conversation."
                ),
                command_label,
                level="error",
            )

        if actor == "agent" and not definition.agent_allowed:
            return CommandResult(
                False,
                f"**Error:** Command `/{definition.name}` is not available to the agent.",
                command_label,
                level="error",
            )
        if definition.requires_admin and ctx.is_admin is False:
            return CommandResult(
                False,
                f"**Error:** Command `/{definition.name}` requires an admin user.",
                command_label,
                level="error",
            )
        if definition.requires_thread and not ctx.thread_id:
            return CommandResult(
                False,
                "**Error:** This command requires an active thread. Send a message first.",
                command_label,
                level="error",
            )
        if not definition.executable:
            return CommandResult(
                False,
                (
                    f"**Error:** `/{definition.name}` is handled outside the command service. "
                    f"{definition.note or ''}"
                ).strip(),
                command_label,
                level="error",
            )

        if definition.id == "help":
            return CommandResult(
                True,
                self._help_markdown(
                    ctx.source,
                    actor=actor,
                    surface=ctx.effective_surface,
                    is_admin=ctx.is_admin,
                    user_id=ctx.user_id,
                    agent=getattr(api, "agent", None),
                ),
                command_label,
                level="info",
            )

        owns_api = api is None
        client = api
        if client is None:
            try:
                client = CommandBackendClient.from_context(ctx)
            except RuntimeError:
                try:
                    client = CommandHttpClient.from_service_token()
                except RuntimeError as exc:
                    return CommandResult(False, f"**Error:** {exc}", command_label, level="error")

        executor = _CommandExecutor(api=client, thread_id=ctx.thread_id, user_id=ctx.user_id)

        method_name = "_cmd_" + "_".join(definition.path)
        method = getattr(executor, method_name, None)

        if method is None:
            return CommandResult(
                False,
                f"**Error:** Command `/{definition.name}` is registered but has no executor.",
                command_label,
                level="error",
            )

        try:
            raw_output = await method(parsed.args, parsed.rest)
            success, markdown = _format_legacy_output(raw_output)
            limit = SKILL_SHOW_MAX_CHARS if definition.id == "skills.show" else 4000
            return CommandResult(
                success,
                _truncate(markdown, limit=limit),
                command_label,
                level="success" if success else "error",
            )
        except httpx.HTTPStatusError as e:
            return CommandResult(False, f"**Error:** {http_error_detail(e)}", command_label, level="error")
        except Exception as e:
            logger.exception("command dispatch failed for /%s", definition.name)
            return CommandResult(False, f"**Error:** {e}", command_label, level="error")
        finally:
            if owns_api and hasattr(client, "close"):
                await client.close()


_COMMAND_SERVICE: CommandService | None = None


def get_command_service() -> CommandService:
    global _COMMAND_SERVICE
    if _COMMAND_SERVICE is None:
        _COMMAND_SERVICE = CommandService()
    return _COMMAND_SERVICE


# ─── Skill kit activation/deactivation from slash commands ─────────────────
#
# Skill kits are skills with `required_tools` in their frontmatter; activating
# one binds those tools into `ThreadConfig.temporary_tools` with the kit's
# `tool_ttl`. The standard path is the agent invoking the `Skill` meta-tool,
# but slash commands (e.g. `/orchestrate`, `/goal`) also need to activate kits.
# These helpers are shared by every slash command that wants kit semantics —
# they mirror what `skills/meta_tool.py` does for the agent-driven path.


def activate_skill_kit(
    *,
    agent: Any,
    thread_id: str,
    user_id: str,
    skill_name: str,
    reason: str = "slash command activation",
    ttl_override: str | None = None,
) -> tuple[bool, str]:
    """Activate a skill or Skill Kit on a thread from a slash command.

    Adds the skill to ``ThreadConfig.enabled_skills``. If the skill declares
    ``required_tools``, those tools are also bound into
    ``ThreadConfig.temporary_tools`` with the kit's ``tool_ttl`` or a one-shot
    override. ``bind_tools_for_thread`` queues the graph rebuild on success,
    so the next agent turn picks up the new tools automatically.

    Returns ``(ok, message)``.
    """
    if not thread_id:
        return False, "[Error]: No active thread; cannot activate skill."

    skill_manager = getattr(agent, "skill_manager", None)
    if skill_manager is None:
        return False, "[Error]: Skill manager unavailable; cannot activate skill."

    try:
        skill = skill_manager.get(skill_name, user_id=user_id)
    except Exception as e:
        return False, f"[Error]: Skill manager lookup failed: {e}"

    if skill is None:
        return False, f"[Error]: Skill '{skill_name}' not found."

    try:
        from ..tools.skill_config import _activate_skill_on_thread

        _activate_skill_on_thread(agent, thread_id, skill_name)
    except Exception as e:
        return False, f"[Error]: Failed to add skill to thread: {e}"

    if not skill.is_skill_kit:
        return True, f"[Success]: Skill '{skill_name}' activated."

    try:
        from ..tools.tool_search import bind_tools_for_thread
    except Exception as e:
        return False, f"[Error]: tool_search unavailable: {e}"

    binding = bind_tools_for_thread(
        list(skill.required_tools),
        "",  # category not used; we pass explicit tool names
        thread_id,
        user_id,
        ttl=ttl_override or skill.tool_ttl,
        strict=True,
        source="slash_command",
        skill_name=skill_name,
        reason=reason,
    )
    if not binding.ok:
        return False, (
            f"[Error]: Skill '{skill_name}' was added to enabled_skills, "
            f"but tool binding failed:\n{binding.text}"
        )

    return True, (
        f"[Success]: Skill kit '{skill_name}' activated.\n{binding.text}"
    )


def deactivate_skill_kit(
    *,
    agent: Any,
    thread_id: str,
    user_id: str | None = None,
    skill_name: str,
) -> tuple[bool, str]:
    """Deactivate a skill or Skill Kit on a thread.

    Removes the skill from ``ThreadConfig.enabled_skills``. For Skill Kits,
    evicts required tools from ``ThreadConfig.temporary_tools``. The graph
    rebuilds on the next turn naturally as the tool set has changed.

    Returns ``(ok, message)``.
    """
    if not thread_id:
        return False, "[Error]: No active thread; cannot deactivate skill."

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        return False, f"[Error]: No thread config for {thread_id}."

    changed = False
    if skill_name in tc.enabled_skills:
        tc.enabled_skills = [n for n in tc.enabled_skills if n != skill_name]
        changed = True

    evicted: list[str] = []
    skill = None
    skill_manager = getattr(agent, "skill_manager", None)
    if skill_manager is not None:
        try:
            skill = skill_manager.get(skill_name, user_id=user_id)
        except Exception:
            skill = None

    if skill is not None and skill.is_skill_kit:
        for tool_name in skill.required_tools:
            if tool_name in tc.temporary_tools:
                del tc.temporary_tools[tool_name]
                evicted.append(tool_name)
                changed = True

    if changed:
        if not agent.thread_config_manager.save_config(tc):
            return False, "[Error]: Failed to save thread config after deactivate."
        if hasattr(agent, "invalidate_thread_config_cache"):
            try:
                agent.invalidate_thread_config_cache(thread_id)
            except Exception:
                logger.debug(
                    "invalidate_thread_config_cache failed after deactivate",
                    exc_info=True,
                )

    label = "Skill kit" if skill is not None and skill.is_skill_kit else "Skill"
    msg_parts = [f"[Success]: {label} '{skill_name}' deactivated."]
    if evicted:
        msg_parts.append(f"Evicted tools: {', '.join(evicted)}.")
    elif not changed:
        msg_parts = [f"[Info]: {label} '{skill_name}' was not active."]
    return True, " ".join(msg_parts)


def build_skill_slash_prompt(
    *,
    skill: Any,
    user_prompt: str,
    has_attachments: bool = False,
) -> str:
    """Build the model-facing prompt for `/skill` and `/kit` chat routing."""
    prompt = user_prompt.strip()
    if not prompt and has_attachments:
        prompt = "Use the attached files/images as the user request."
    elif not prompt:
        prompt = "Use this skill for the current turn."

    attachment_note = (
        "\n\nThe user attached files/images to this request. Treat them as part "
        "of the prompt and inspect them as needed."
        if has_attachments
        else ""
    )
    body = str(getattr(skill, "body", "") or "").strip() or "(No skill body.)"
    return (
        f"[Skill slash command: {skill.name}]\n\n"
        "Apply this SKILL.md body for the current turn:\n\n"
        f"{body}\n\n"
        "---\n\n"
        "User request:\n"
        f"{prompt}{attachment_note}"
    )


def prepare_skill_slash_command(
    *,
    agent: Any,
    thread_id: str,
    user_id: str,
    mode: Literal["skill", "kit"],
    rest: str,
    has_attachments: bool = False,
) -> SkillSlashResult:
    """Prepare `/skill` or `/kit` chat-stream handling.

    Returns ``should_stream=True`` when the caller should pass ``message`` into
    the normal agent stream. Returns ``should_stream=False`` for usage errors
    and deactivation responses that should be emitted directly.
    """
    args = _split_args(rest)
    if not args:
        usage = (
            "/skill <name> [prompt]"
            if mode == "skill"
            else "/kit <name> [ttl] [prompt]"
        )
        return SkillSlashResult(
            False,
            False,
            f"[Error]: Usage: `{usage}`.",
        )

    skill_name = args[0].strip().lower()
    tail = _split_rest_after_tokens(rest, 1)
    tail_args = _split_args(tail)

    skill_manager = getattr(agent, "skill_manager", None)
    if skill_manager is None:
        return SkillSlashResult(
            False,
            False,
            "[Error]: Skill manager unavailable.",
            skill_name,
        )
    try:
        skill = skill_manager.get(skill_name, user_id=user_id)
    except Exception as e:  # noqa: BLE001
        return SkillSlashResult(
            False,
            False,
            f"[Error]: Skill manager lookup failed: {e}",
            skill_name,
        )
    if skill is None or getattr(skill, "is_internal", False):
        noun = "Skill Kit" if mode == "kit" else "Skill"
        return SkillSlashResult(
            False,
            False,
            f"[Error]: {noun} '{skill_name}' not found.",
            skill_name,
        )

    is_kit = bool(getattr(skill, "is_skill_kit", False))
    if mode == "skill" and is_kit:
        return SkillSlashResult(
            False,
            False,
            f"[Error]: '{skill_name}' is a Skill Kit. Use `/kit {skill_name}`.",
            skill_name,
        )
    if mode == "kit" and not is_kit:
        return SkillSlashResult(
            False,
            False,
            f"[Error]: '{skill_name}' is a markdown-only skill. Use `/skill {skill_name}`.",
            skill_name,
        )

    if len(tail_args) == 1 and tail_args[0].lower() == "off":
        ok, msg = deactivate_skill_kit(
            agent=agent,
            thread_id=thread_id,
            user_id=user_id,
            skill_name=skill_name,
        )
        return SkillSlashResult(ok, False, msg, skill_name)

    ttl_override: str | None = None
    prompt = tail
    if mode == "kit" and tail_args:
        try:
            ttl_key, _ = parse_tool_ttl(tail_args[0])
            ttl_override = ttl_key
            prompt = _split_rest_after_tokens(tail, 1)
        except ValueError:
            ttl_override = None
            prompt = tail

    ok, msg = activate_skill_kit(
        agent=agent,
        thread_id=thread_id,
        user_id=user_id,
        skill_name=skill_name,
        reason=f"/{mode} {skill_name}",
        ttl_override=ttl_override,
    )
    if not ok:
        return SkillSlashResult(False, False, msg, skill_name)

    return SkillSlashResult(
        True,
        True,
        build_skill_slash_prompt(
            skill=skill,
            user_prompt=prompt,
            has_attachments=has_attachments,
        ),
        skill_name,
    )


class _CommandExecutor:
    """Per-request command executor with the migrated command bodies."""

    def __init__(self, api: Any, thread_id: str | None, user_id: str):
        self.api = api
        self.thread_id = thread_id or ""
        self.user_id = user_id

    def _require_thread(self) -> str | None:
        if self.thread_id:
            return None
        return "[Error]: This command requires an active thread. Send a message first."

    def _agent(self) -> Any | None:
        agent = getattr(self.api, "agent", None)
        if agent is not None:
            return agent
        return get_command_service()._resolve_agent()

    # ── Help ──────────────────────────────────────────────────────────────

    async def _cmd_help(self, args: list[str], rest: str) -> str:
        return get_command_service()._help_markdown(
            "user",
            user_id=self.user_id,
            agent=self._agent(),
        )

    # ── Skills ────────────────────────────────────────────────────────────

    async def _cmd_skills(self, args: list[str], rest: str) -> str:
        if not args or args == ["list"]:
            return await self._cmd_skills_list([], "")
        if args[0] == "show":
            return await self._cmd_skills_show(args[1:], rest)
        if args == ["off", "all"]:
            return await self._cmd_skills_off_all([], "")
        return (
            "[Error]: Usage: `/skills`, `/skills list`, `/skills show <name>`, "
            "`/skills off all`, `/skills search [query]`, "
            "`/skills install <name>`, `/skills enable [--global] <name>`, "
            "`/skills disable [--global] <name>`, or `/skills inspect <name>`."
        )

    async def _cmd_skills_list(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error

        agent = self._agent()
        if agent is None:
            return "[Error]: No current NymeriaAgent is available for skill commands."

        service = get_command_service()
        skills = service._visible_slash_skills(self.user_id, agent=agent)
        if not skills:
            return "[Info]: No user-activatable skills are installed."

        tc = agent.thread_config_manager.get_config(self.thread_id)
        active = set(tc.enabled_skills or []) if tc is not None else set()

        lines = ["Skills on this thread:", ""]
        for skill in skills:
            status = "active" if skill.name in active else "inactive"
            kind = "kit" if skill.is_skill_kit else "skill"
            ttl = f"; ttl: `{skill.tool_ttl}`" if skill.is_skill_kit else ""
            lines.append(f"- `{skill.name}` - {kind}, {status}{ttl}; {skill.description}")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_skills_show(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: `/skills show <name>`."

        agent = self._agent()
        service = get_command_service()
        skill_manager = service._skill_manager(agent)
        if skill_manager is None:
            return "[Error]: Skill manager unavailable."

        skill_name = args[0].strip().lower()
        try:
            skill = skill_manager.get(skill_name, user_id=self.user_id)
        except Exception as e:  # noqa: BLE001
            return f"[Error]: Skill manager lookup failed: {e}"
        if skill is None:
            return f"[Error]: Skill '{skill_name}' not found."

        return skill.body.strip() or f"# {skill.name}\n\n(No body.)"

    async def _cmd_skills_off_all(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error

        agent = self._agent()
        if agent is None:
            return "[Error]: No current NymeriaAgent is available for skill commands."

        service = get_command_service()
        skills = service._visible_slash_skills(self.user_id, agent=agent)
        tc = agent.thread_config_manager.get_config(self.thread_id)
        active = set(tc.enabled_skills or []) if tc is not None else set()
        active_skills = [skill for skill in skills if skill.name in active]
        if not active_skills:
            return "[Info]: No visible skills are active on this thread."

        lines: list[str] = []
        had_error = False
        for skill in active_skills:
            ok, msg = deactivate_skill_kit(
                agent=agent,
                thread_id=self.thread_id,
                user_id=self.user_id,
                skill_name=skill.name,
            )
            had_error = had_error or not ok
            lines.append(f"- `{skill.name}`: {msg}")

        prefix = "[Error]:" if had_error else "[Success]:"
        return prefix + " Deactivated skills:\n" + "\n".join(lines)

    async def _cmd_skills_search(self, args: list[str], rest: str) -> str:
        source, remaining, error = _consume_option(args, "--source", default="anthropic")
        if error:
            return f"[Error]: {error}"
        query = " ".join(remaining).strip() or None

        from ..skills.marketplace import MarketplaceError, get_fetcher

        try:
            fetcher = get_fetcher(source)
        except (NotImplementedError, MarketplaceError) as exc:
            return f"[Error]: {exc}"

        try:
            entries = fetcher.list(query)
        except MarketplaceError as exc:
            return f"[Error]: Marketplace search failed: {exc}"
        except Exception as exc:  # noqa: BLE001 - surface fetcher errors
            return f"[Error]: Marketplace search failed: {exc}"

        if not entries:
            return f"[Info]: No marketplace skills matched on source '{source}'."

        lines = [f"Marketplace Skills ({source}): {len(entries)} found", ""]
        for entry in entries[:50]:
            description = (entry.description or "").strip().splitlines()
            short = description[0] if description else ""
            lines.append(f"- `{entry.name}` — {short}")
        if len(entries) > 50:
            lines.append(f"... and {len(entries) - 50} more")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_skills_install(self, args: list[str], rest: str) -> str:
        scope, args, error = _consume_option(args, "--scope", default="user")
        if error:
            return f"[Error]: {error}"
        source, args, error = _consume_option(args, "--source", default="anthropic")
        if error:
            return f"[Error]: {error}"
        if not args:
            return (
                "[Error]: Usage: /skills install <name> "
                "[--source <source>] [--scope user|global]"
            )
        if scope not in ("user", "global"):
            return "[Error]: --scope must be 'user' or 'global'."
        name = args[0]

        agent = self._agent()
        if agent is None:
            return "[Error]: No current NymeriaAgent is available for skill commands."
        skill_manager = getattr(agent, "skill_manager", None)
        if skill_manager is None:
            return "[Error]: Skill manager unavailable."

        from ..skills.marketplace import MarketplaceError, get_fetcher

        try:
            fetcher = get_fetcher(source)
        except (NotImplementedError, MarketplaceError) as exc:
            return f"[Error]: {exc}"

        target_dir = skill_manager.target_dir(
            scope,
            user_id=self.user_id if scope == "user" else None,
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            skill = fetcher.fetch(name, target_dir)
        except MarketplaceError as exc:
            return f"[Error]: Install failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            return f"[Error]: Install failed: {exc}"

        skill_manager.reload()
        return f"[Success]: Installed skill '{skill.name}' (scope: {scope})."

    async def _cmd_skills_enable(self, args: list[str], rest: str) -> str:
        return await self._set_skill_state(args, enabled=True)

    async def _cmd_skills_disable(self, args: list[str], rest: str) -> str:
        return await self._set_skill_state(args, enabled=False)

    async def _set_skill_state(self, args: list[str], *, enabled: bool) -> str:
        global_scope, args = _consume_flag(args, "--global")
        verb = "enable" if enabled else "disable"
        if not args:
            return f"[Error]: Usage: /skills {verb} [--global] <name>"
        name = args[0]

        agent = self._agent()
        if agent is None:
            return "[Error]: No current NymeriaAgent is available for skill commands."

        if global_scope:
            profile_manager = getattr(agent, "profile_manager", None)
            if profile_manager is None:
                return "[Error]: Profile manager unavailable."
            profile = profile_manager.get_profile(self.user_id)
            current = list(getattr(profile, "enabled_global_skills", []) or [])
            if enabled:
                if name not in current:
                    current.append(name)
            else:
                current = [item for item in current if item != name]
            profile.enabled_global_skills = sorted(set(current))
            profile_manager.save_profile(profile)
            action = "Enabled globally" if enabled else "Disabled globally"
            return f"[Success]: {action}: {name}"

        thread_error = self._require_thread()
        if thread_error:
            return thread_error

        tc = await self.api.get_thread_config(self.thread_id)
        enabled_skills = list((tc or {}).get("enabled_skills", []) or [])
        disabled_skills = list((tc or {}).get("disabled_skills", []) or [])
        if enabled:
            if name not in enabled_skills:
                enabled_skills.append(name)
            disabled_skills = [item for item in disabled_skills if item != name]
        else:
            if name not in disabled_skills:
                disabled_skills.append(name)
            enabled_skills = [item for item in enabled_skills if item != name]

        await self.api.update_thread_config(
            self.thread_id,
            user_id=self.user_id,
            enabled_skills=enabled_skills,
            disabled_skills=disabled_skills,
        )
        action = "Enabled" if enabled else "Disabled"
        return f"[Success]: {action}: {name}"

    async def _cmd_skills_inspect(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /skills inspect <name>"

        agent = self._agent()
        service = get_command_service()
        skill_manager = service._skill_manager(agent)
        if skill_manager is None:
            return "[Error]: Skill manager unavailable."

        skill_name = args[0].strip().lower()
        try:
            skill = skill_manager.get(skill_name, user_id=self.user_id)
        except Exception as exc:  # noqa: BLE001
            return f"[Error]: Skill manager lookup failed: {exc}"
        if skill is None:
            return f"[Error]: Skill '{skill_name}' not found."

        scripts = skill.list_scripts() if hasattr(skill, "list_scripts") else []
        references = skill.list_references() if hasattr(skill, "list_references") else []
        rows = [
            ("Name", skill.name),
            ("Scope", skill.scope),
            ("Kit", "yes" if getattr(skill, "is_skill_kit", False) else "no"),
            ("Required tools", ", ".join(skill.required_tools) or "—"),
            ("Allowed tools", ", ".join(skill.allowed_tools) or "—"),
            ("Scripts", ", ".join(scripts) or "—"),
            ("References", ", ".join(references) or "—"),
            ("Path", str(skill.path)),
        ]
        width = max(len(label) for label, _ in rows)
        lines = ["Skill", ""]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        description = (skill.description or "").strip()
        if description:
            lines.append("")
            lines.append(f"Description: {description}")
        return "[Info]: " + "\n".join(lines)

    # ── MCP servers ───────────────────────────────────────────────────────

    async def _cmd_mcp(self, args: list[str], rest: str) -> str:
        if not args or args == ["list"]:
            return await self._cmd_mcp_list([], "")
        return (
            "[Error]: Usage: /mcp list|status|logs|discover|test|remove|retry [...]"
        )

    async def _cmd_mcp_list(self, args: list[str], rest: str) -> str:
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        servers = registry.get_all_servers()
        if not servers:
            return "[Info]: No MCP servers configured."

        lines = [
            f"MCP Servers: {len(servers)} configured",
            "",
            "| ID | State | Tools | Name |",
            "|---|---|---|---|",
        ]
        for server in sorted(servers, key=lambda s: s.id):
            state = server.install_status or ("enabled" if server.enabled else "disabled")
            tool_count = len(server.discovered_tools or [])
            name = server.name or server.id
            lines.append(f"| `{server.id}` | {state} | {tool_count} | {name} |")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_mcp_status(self, args: list[str], rest: str) -> str:
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        if args:
            server = registry.get_server(args[0])
            if server is None:
                return f"[Error]: MCP server '{args[0]}' not found."
            rows = [
                ("ID", server.id),
                ("Name", server.name or ""),
                ("Enabled", "yes" if server.enabled else "no"),
                ("Install status", server.install_status or ""),
                ("Tools", len(server.discovered_tools or [])),
                ("Last error", (server.last_error or "")[:200]),
            ]
            width = max(len(label) for label, _ in rows)
            lines = [f"MCP Server `{server.id}`", ""]
            for label, value in rows:
                lines.append(f"  {label:<{width}}  {value}")
            recent_logs = list(server.install_logs or [])[-5:]
            if recent_logs:
                lines.append("")
                lines.append("Recent logs:")
                lines.extend(f"  {line}" for line in recent_logs)
            return "[Info]: " + "\n".join(lines)

        servers = registry.get_all_servers()
        if not servers:
            return "[Info]: No MCP servers configured."

        lines = ["MCP Servers", ""]
        for server in sorted(servers, key=lambda s: s.id):
            state = server.install_status or ("enabled" if server.enabled else "disabled")
            err = f" — error: {server.last_error}" if server.last_error else ""
            lines.append(f"- `{server.id}`: {state}{err}")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_mcp_logs(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /mcp logs <server-id> [limit]"

        from ..core.mcp_servers import get_mcp_server_registry

        server_id = args[0]
        try:
            limit = max(1, int(args[1])) if len(args) > 1 else 20
        except (TypeError, ValueError):
            limit = 20

        registry = get_mcp_server_registry()
        server = registry.get_server(server_id)
        if server is None:
            return f"[Error]: MCP server '{server_id}' not found."

        logs = list(server.install_logs or [])
        if not logs:
            return f"[Info]: No install logs for `{server_id}`."
        selected = logs[-limit:]
        lines = [f"Install logs for `{server_id}` (last {len(selected)} of {len(logs)})", ""]
        lines.extend(f"- {line}" for line in selected)
        return "[Info]: " + "\n".join(lines)

    async def _cmd_mcp_discover(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /mcp discover <server-id>"

        from ..core.mcp_servers import get_mcp_server_registry

        server_id = args[0]
        registry = get_mcp_server_registry()
        server = registry.get_server(server_id)
        if server is None:
            return f"[Error]: MCP server '{server_id}' not found."

        agent = self._agent()
        try:
            discovered = registry.discover_tools(server_id)
        except Exception as exc:  # noqa: BLE001 - discovery errors surface as markdown
            server.install_status = "failed"
            server.enabled = False
            server.last_error = str(exc)
            registry.save_server(server)
            if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
                agent.reload_mcp_server_tools()
            return f"[Error]: Tool discovery failed for `{server_id}`: {exc}"

        if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
            agent.reload_mcp_server_tools()
        count = len(discovered)
        return f"[Success]: Discovered {count} tool{'s' if count != 1 else ''} for `{server_id}`."

    async def _cmd_mcp_test(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /mcp test <server-id>"

        from ..core.mcp_servers import get_mcp_server_registry

        server_id = args[0]
        registry = get_mcp_server_registry()
        server = registry.get_server(server_id)
        if server is None:
            return f"[Error]: MCP server '{server_id}' not found."

        try:
            result = registry.test_connection(server_id)
        except Exception as exc:  # noqa: BLE001
            return f"[Error]: MCP test failed for `{server_id}`: {exc}"

        result_dict = result if isinstance(result, dict) else {}
        status = str(result_dict.get("status") or "ok")
        tools_count = result_dict.get("tools_count") or result_dict.get("toolsCount") or ""
        error = str(result_dict.get("error") or "")
        if status.casefold() in {"ok", "success", "connected"} and not error:
            suffix = f" ({tools_count} tools)" if str(tools_count) else ""
            return f"[Success]: MCP test passed: `{server_id}`{suffix}"
        return f"[Error]: MCP test failed for `{server_id}`: {error or result}"

    async def _cmd_mcp_remove(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /mcp remove <server-id>"

        from ..core.mcp_servers import get_mcp_server_registry

        server_id = args[0]
        registry = get_mcp_server_registry()
        if not registry.delete_server(server_id):
            return f"[Error]: MCP server '{server_id}' not found."

        agent = self._agent()
        if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
            agent.reload_mcp_server_tools()
        return f"[Success]: Removed MCP server `{server_id}`."

    async def _cmd_mcp_retry(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /mcp retry <server-id>"

        from ..core.mcp_runtime import MCPInstallPlan
        from ..core.mcp_servers import get_mcp_server_registry

        server_id = args[0]
        registry = get_mcp_server_registry()
        server = registry.get_server(server_id)
        if server is None:
            return f"[Error]: MCP server '{server_id}' not found."
        if not server.install_plan:
            return f"[Error]: MCP server `{server_id}` has no install plan to retry."

        # The full retry flow uses _run_mcp_install on the API router with
        # admin-confirmation, credential bindings, and thread auto-enable.
        # Surface a guidance message rather than re-implementing it half-way
        # here; the desktop UI exposes the rich retry flow.
        plan = MCPInstallPlan.from_dict(server.install_plan)
        try:
            discovered = registry.discover_tools(server_id)
        except Exception as exc:  # noqa: BLE001
            return f"[Error]: Retry failed for `{server_id}`: {exc}"
        server.install_status = "ready" if discovered else "draft"
        server.last_error = ""
        registry.save_server(server)
        agent = self._agent()
        if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
            agent.reload_mcp_server_tools()
        return (
            f"[Success]: Retried `{server_id}` "
            f"(runtime: {plan.runtime_type}); "
            f"discovered {len(discovered)} tool(s)."
        )

    # ── Event triggers ────────────────────────────────────────────────────

    def _trigger_manager(self) -> Any:
        from ..tools.triggers import _get_trigger_manager

        return _get_trigger_manager()

    async def _cmd_triggers(self, args: list[str], rest: str) -> str:
        if not args or args == ["list"]:
            return await self._cmd_triggers_list([], "")
        return (
            "[Error]: Usage: /triggers list|enable|disable|delete|history [...]"
        )

    async def _cmd_triggers_list(self, args: list[str], rest: str) -> str:
        enabled_only, args = _consume_flag(args, "--enabled-only")
        thread_id, args, error = _consume_option(args, "--thread", default="")
        if error:
            return f"[Error]: {error}"
        if thread_id == "current":
            thread_id = self.thread_id

        manager = self._trigger_manager()
        triggers = manager.get_triggers(self.user_id) or []

        if enabled_only:
            triggers = [t for t in triggers if t.enabled]
        if thread_id:
            triggers = [t for t in triggers if t.thread_id == thread_id]
        if not triggers:
            return "[Info]: No triggers found."

        lines = [
            f"Triggers: {len(triggers)} total",
            "",
            "| ID | Status | Source | Action | Name |",
            "|---|---|---|---|---|",
        ]
        for t in sorted(triggers, key=lambda item: item.id):
            status = "enabled" if t.enabled else "disabled"
            action_type = getattr(t.action, "type", "?") if t.action else "?"
            lines.append(
                f"| `{t.id}` | {status} | {t.source_type} | {action_type} | {t.name} |"
            )
        return "[Info]: " + "\n".join(lines)

    async def _cmd_triggers_enable(self, args: list[str], rest: str) -> str:
        return await self._set_trigger_enabled(args, enabled=True)

    async def _cmd_triggers_disable(self, args: list[str], rest: str) -> str:
        return await self._set_trigger_enabled(args, enabled=False)

    async def _set_trigger_enabled(self, args: list[str], *, enabled: bool) -> str:
        verb = "enable" if enabled else "disable"
        if not args:
            return f"[Error]: Usage: /triggers {verb} <trigger-id>"
        trigger_id = args[0]
        manager = self._trigger_manager()
        if not manager.update_trigger(self.user_id, trigger_id, enabled=enabled):
            return f"[Error]: Trigger '{trigger_id}' not found."
        action = "Enabled" if enabled else "Disabled"
        return f"[Success]: {action} trigger `{trigger_id}`."

    async def _cmd_triggers_delete(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /triggers delete <trigger-id>"
        trigger_id = args[0]
        manager = self._trigger_manager()
        if not manager.delete_trigger(self.user_id, trigger_id):
            return f"[Error]: Trigger '{trigger_id}' not found."
        manager.delete_executions_for_triggers(self.user_id, [trigger_id])
        return f"[Success]: Deleted trigger `{trigger_id}`."

    async def _cmd_triggers_history(self, args: list[str], rest: str) -> str:
        limit_str, remaining, error = _consume_option(args, "--limit", default="20")
        if error:
            return f"[Error]: {error}"
        try:
            limit = max(1, int(limit_str))
        except (TypeError, ValueError):
            limit = 20
        trigger_id = remaining[0] if remaining else None

        manager = self._trigger_manager()
        executions = manager.get_executions(
            self.user_id,
            trigger_id=trigger_id,
            limit=limit,
        )
        if not executions:
            scope = f" for `{trigger_id}`" if trigger_id else ""
            return f"[Info]: No trigger executions found{scope}."

        lines = [
            f"Trigger executions: {len(executions)}"
            + (f" for `{trigger_id}`" if trigger_id else ""),
            "",
        ]
        for entry in executions:
            timestamp = entry.get("triggered_at") or entry.get("timestamp") or ""
            tid = entry.get("trigger_id") or "?"
            status = entry.get("status") or entry.get("result") or "?"
            summary = (entry.get("summary") or entry.get("message") or "")[:120]
            lines.append(f"- {timestamp} `{tid}` — {status}: {summary}")
        return "[Info]: " + "\n".join(lines)

    # ── Account ───────────────────────────────────────────────────────────

    def _accounts_repo(self) -> Any | None:
        agent = self._agent()
        if agent is None:
            return None
        return getattr(agent, "accounts_repo", None)

    async def _cmd_account(self, args: list[str], rest: str) -> str:
        if not args or args == ["current"]:
            return await self._cmd_account_current([], "")
        return "[Error]: Usage: /account current|tokens|platforms"

    async def _cmd_account_current(self, args: list[str], rest: str) -> str:
        repo = self._accounts_repo()
        if repo is None:
            return "[Error]: Account repository unavailable."
        user = repo.get_user_by_id(self.user_id)
        if user is None:
            return f"[Error]: User '{self.user_id}' not found."
        rows = [
            ("Selected user", self.user_id),
            ("ID", getattr(user, "id", "")),
            ("Email", getattr(user, "email", "")),
            ("Display name", getattr(user, "display_name", "")),
            ("Role", getattr(user, "role", "")),
        ]
        width = max(len(label) for label, _ in rows)
        lines = ["Current account", ""]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_account_tokens(self, args: list[str], rest: str) -> str:
        repo = self._accounts_repo()
        if repo is None:
            return "[Error]: Account repository unavailable."
        tokens = repo.list_tokens_for_user(self.user_id) or []
        if not tokens:
            return "[Info]: No API tokens issued."
        lines = [
            f"API tokens: {len(tokens)}",
            "",
            "| Prefix | Label | Created | Last used | Revoked |",
            "|---|---|---|---|---|",
        ]
        for token in tokens:
            prefix = getattr(token, "token_hash_prefix", "")
            label = getattr(token, "label", "") or ""
            created = getattr(token, "created_at", "") or ""
            last_used = getattr(token, "last_used_at", "") or ""
            revoked = getattr(token, "revoked_at", "") or ""
            lines.append(f"| `{prefix}` | {label} | {created} | {last_used} | {revoked} |")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_account_tokens_issue(self, args: list[str], rest: str) -> str:
        repo = self._accounts_repo()
        if repo is None:
            return "[Error]: Account repository unavailable."
        label = " ".join(args).strip() or None
        try:
            raw_token = repo.issue_token(self.user_id, label=label)
        except Exception as exc:  # noqa: BLE001
            return f"[Error]: Could not issue token: {exc}"
        prefix = raw_token[:8] if isinstance(raw_token, str) else ""
        lines = [
            f"[Success]: Issued API token (prefix `{prefix}`).",
            "",
            "Raw token (shown only once — save it now):",
            "",
            "```",
            str(raw_token),
            "```",
        ]
        return "\n".join(lines)

    async def _cmd_account_tokens_revoke(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /account tokens revoke <hash-prefix>"
        prefix = args[0]
        repo = self._accounts_repo()
        if repo is None:
            return "[Error]: Account repository unavailable."
        revoked = bool(repo.revoke_token(self.user_id, prefix))
        if not revoked:
            return f"[Error]: No matching token for prefix `{prefix}`."
        return f"[Success]: Revoked token `{prefix}`."

    async def _cmd_account_platforms(self, args: list[str], rest: str) -> str:
        repo = self._accounts_repo()
        if repo is None:
            return "[Error]: Account repository unavailable."
        platforms = repo.list_platforms_for_user(self.user_id) or []
        if not platforms:
            return "[Info]: No linked chat platforms."
        lines = [
            f"Linked platforms: {len(platforms)}",
            "",
            "| Provider | Provider user ID | Created |",
            "|---|---|---|",
        ]
        for p in platforms:
            provider = getattr(p, "provider", "")
            puid = getattr(p, "provider_user_id", "")
            created = getattr(p, "created_at", "") or ""
            lines.append(f"| {provider} | `{puid}` | {created} |")
        return "[Info]: " + "\n".join(lines)

    # ── Activity / notifications ──────────────────────────────────────────

    async def _cmd_activity(self, args: list[str], rest: str) -> str:
        if not args or args in (["list"], ["recent"]):
            return await self._cmd_activity_list([], "")
        if args == ["notifications"]:
            return await self._cmd_activity_notifications([], "")
        return (
            "[Error]: Usage: /activity list [limit] [--type TYPE] [--thread ID] "
            "or /activity notifications"
        )

    async def _cmd_activity_list(self, args: list[str], rest: str) -> str:
        from ..core.activity_log import ActivityType, get_activity_log

        limit = 20
        activity_type_str: str | None = None
        thread_id: str | None = None
        index = 0
        while index < len(args):
            arg = args[index]
            if arg.isdigit():
                limit = max(1, int(arg))
            elif arg == "--type" and index + 1 < len(args):
                activity_type_str = args[index + 1]
                index += 1
            elif arg == "--thread" and index + 1 < len(args):
                value = args[index + 1]
                thread_id = self.thread_id if value.casefold() in {"current", "."} else value
                index += 1
            else:
                return f"[Error]: Unknown activity option: {arg}"
            index += 1

        type_filter = None
        if activity_type_str:
            try:
                type_filter = ActivityType(activity_type_str)
            except ValueError:
                return f"[Error]: Invalid activity type: {activity_type_str}"

        log = get_activity_log()
        entries = log.get_entries(
            self.user_id,
            limit=limit,
            activity_type=type_filter,
            thread_id=thread_id,
        )
        if not entries:
            return "[Info]: No recent activity."

        lines = [
            f"Recent activity: {len(entries)}",
            "",
            "| Time | Type | Thread | Message |",
            "|---|---|---|---|",
        ]
        for entry in entries:
            ts = str(getattr(entry, "timestamp", "") or "")[:19]
            etype = getattr(entry.type, "value", str(entry.type)) if entry.type else ""
            tid = (getattr(entry, "thread_id", "") or "")[:8]
            msg = (getattr(entry, "message", "") or "").replace("\n", " ")[:80]
            lines.append(f"| {ts} | {etype} | `{tid}` | {msg} |")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_activity_notifications(self, args: list[str], rest: str) -> str:
        from ..core.notifications import get_notification_store

        store = get_notification_store()
        notifications = store.get_all(self.user_id, limit=50) or []
        unread = store.get_unread_count(self.user_id)
        if not notifications:
            return f"[Info]: No notifications. (Unread: {unread})"

        lines = [
            f"Notifications ({unread} unread): {len(notifications)} total",
            "",
            "| ID | Read | Summary |",
            "|---|---|---|",
        ]
        for n in notifications:
            nid = (getattr(n, "id", "") or "")[:8]
            read = "yes" if getattr(n, "read", False) else "no"
            summary = (getattr(n, "summary", "") or "").replace("\n", " ")[:80]
            lines.append(f"| `{nid}` | {read} | {summary} |")
        return "[Info]: " + "\n".join(lines)

    # ── Doctor (server-side diagnostics) ──────────────────────────────────

    async def _cmd_doctor(self, args: list[str], rest: str) -> str:
        if args == ["auth"]:
            return await self._cmd_doctor_auth([], "")
        if args == ["model"]:
            return await self._cmd_doctor_model([], "")
        if args:
            return "[Error]: Usage: /doctor [auth|model]"
        auth = await self._cmd_doctor_auth([], "")
        model = await self._cmd_doctor_model([], "")
        return f"{auth}\n\n{model}"

    async def _cmd_doctor_auth(self, args: list[str], rest: str) -> str:
        repo = self._accounts_repo()
        if repo is None:
            return "[Info]: Auth\n  Selected user  " + self.user_id
        user = repo.get_user_by_id(self.user_id)
        rows = [
            ("Selected user", self.user_id),
            ("Resolved ID", getattr(user, "id", "") if user else ""),
            ("Display name", getattr(user, "display_name", "") if user else ""),
            ("Role", getattr(user, "role", "") if user else ""),
        ]
        width = max(len(label) for label, _ in rows)
        lines = ["Auth", ""]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_doctor_model(self, args: list[str], rest: str) -> str:
        try:
            settings = await self.api.get_settings(user_id=self.user_id)
        except Exception as exc:  # noqa: BLE001
            return f"[Error]: Could not load settings: {exc}"
        settings_dict = settings if isinstance(settings, dict) else {}
        rows = [
            ("Provider", settings_dict.get("llm_provider", "")),
            ("Model", settings_dict.get("llm_model", "")),
            ("Base URL", str(settings_dict.get("llm_base_url", ""))[:80]),
            ("Status", "available"),
        ]
        width = max(len(label) for label, _ in rows)
        lines = ["Model", ""]
        for label, value in rows:
            lines.append(f"  {label:<{width}}  {value}")
        return "[Info]: " + "\n".join(lines)

    # ── Status / inspection ───────────────────────────────────────────────

    async def _cmd_status(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        import asyncio
        settings, ctx, tools_data, todos = await asyncio.gather(
            self.api.get_settings(),
            self.api.get_context_stats(self.thread_id),
            self.api.get_default_tools(self.user_id),
            self.api.list_todos(self.user_id),
            return_exceptions=True,
        )
        if isinstance(settings, Exception):
            settings = {}
        if isinstance(ctx, Exception):
            ctx = {}
        if isinstance(tools_data, Exception):
            tools_data = {}
        if isinstance(todos, Exception):
            todos = []

        model = settings.get("llm_model", "?")
        provider = settings.get("llm_provider", "?")
        base_url = settings.get("llm_base_url")
        if base_url and "cli-proxy" in base_url:
            provider = f"{provider} (via CLIProxy)"
        thinking = settings.get("llm_extended_thinking", False)
        effort = settings.get("llm_reasoning_effort")
        think_str = "off"
        if thinking:
            think_str = f"on ({effort})" if effort else "on"

        total = ctx.get("total_tokens", 0)
        limit = ctx.get("context_limit", 0)
        pct = ctx.get("usage_percentage", 0)
        compactions = ctx.get("compaction_count", 0)
        ctx_mode = ctx.get("context_management") or settings.get("context_management", "?")

        default_count = len(tools_data.get("default_tools", []))
        available_count = len(tools_data.get("available_tools", []))

        task_parts = []
        if todos:
            t_pending = sum(1 for t in todos if t.get("status") == "pending")
            t_in_prog = sum(1 for t in todos if t.get("status") == "in_progress")
            if t_pending:
                task_parts.append(f"{t_pending} pending")
            if t_in_prog:
                task_parts.append(f"{t_in_prog} in progress")
        tasks_str = " / ".join(task_parts) if task_parts else "none"

        lines = [
            "Nymeria Status",
            "",
            "Model",
            f"  {model} | {provider} | thinking: {think_str}",
            "",
            "Context",
            f"  {fmt_tokens(total)} / {fmt_tokens(limit)} tokens ({pct}%)",
            f"  mode: {ctx_mode}"
            + (f" | {compactions} compaction{'s' if compactions != 1 else ''}" if compactions else ""),
            "",
            "Tools",
            f"  {default_count} core / {available_count} available",
            "",
            "Tasks",
            f"  {tasks_str}",
            "",
            f"thread: {self.thread_id}",
            f"user:   {self.user_id}",
        ]
        return "[Info]: " + "\n".join(lines)

    async def _cmd_thread(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        stats = await self.api.get_context_stats(self.thread_id)
        lines = [
            "Thread Info",
            f"  thread id: {self.thread_id}",
            f"  tokens: {fmt_tokens(stats.get('total_tokens', 0))} / "
            f"{fmt_tokens(stats.get('context_limit', 0))} "
            f"({stats.get('usage_percentage', 0)}%)",
            f"  compactions: {stats.get('compaction_count', 0)}",
            f"  mode: {stats.get('context_management', 'unknown')}",
        ]
        return "[Info]: " + "\n".join(lines)

    async def _cmd_context(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        import asyncio
        ctx, thread_cfg, settings, categories, tools_data = await asyncio.gather(
            self.api.get_context_stats(self.thread_id),
            self.api.get_thread_config(self.thread_id),
            self.api.get_settings(),
            self.api.get_tool_categories(),
            self.api.get_default_tools(self.user_id),
            return_exceptions=True,
        )
        if isinstance(ctx, Exception):
            ctx = {}
        if isinstance(thread_cfg, Exception):
            thread_cfg = None
        if isinstance(settings, Exception):
            settings = {}
        if isinstance(categories, Exception):
            categories = {}
        if isinstance(tools_data, Exception):
            tools_data = {}

        effective_model = ctx.get("model") or settings.get("llm_model", "?")
        provider = settings.get("llm_provider", "?")
        base_url = settings.get("llm_base_url")
        if base_url and "cli-proxy" in base_url:
            provider = f"{provider} (via CLIProxy)"

        lines = ["Context Breakdown", "", "Model", f"  {effective_model} | {provider}"]
        if thread_cfg:
            llm_cfg = thread_cfg.get("llm_config") or {}
            tm = llm_cfg.get("model")
            if tm and tm != settings.get("llm_model"):
                lines.append(f"  thread override: model={tm}")

        total = ctx.get("total_tokens", 0)
        limit = ctx.get("context_limit", 0)
        pct = ctx.get("usage_percentage", 0)
        compactions = ctx.get("compaction_count", 0)
        ctx_mode = ctx.get("context_management") or settings.get("context_management", "?")
        cumulative = ctx.get("cumulative_tokens", 0)

        lines.append("")
        lines.append("Context Window")
        token_line = f"  {fmt_tokens(total)} / {fmt_tokens(limit)} tokens ({pct}%)"
        if cumulative:
            token_line += f" (cumulative: {fmt_tokens(cumulative)})"
        lines.append(token_line)
        if compactions:
            lines.append(f"  {compactions} compaction{'s' if compactions != 1 else ''}")
        mode_line = f"  mode: {ctx_mode}"
        threshold = settings.get("compact_threshold")
        if threshold and ctx_mode == "auto_compact":
            mode_line += f" (threshold {int(threshold * 100)}%)"
        lines.append(mode_line)

        default_tools = set(tools_data.get("default_tools", []))
        available_tools = tools_data.get("available_tools", [])
        cats = categories.get("categories", {}) if isinstance(categories, dict) else {}
        disabled = set()
        extra_enabled = set()
        if thread_cfg:
            disabled = set(thread_cfg.get("disabled_tools") or [])
            extra_enabled = set(thread_cfg.get("enabled_tools") or [])
        effective = (default_tools - disabled) | extra_enabled

        lines.append("")
        lines.append("Tools")
        lines.append(f"  {len(effective)} enabled (of {len(available_tools)} available)")
        for cat_name in sorted(cats.keys()):
            cat_tools = set(cats[cat_name])
            enabled_in_cat = len(cat_tools & effective)
            total_in_cat = len(cat_tools)
            if enabled_in_cat == total_in_cat:
                lines.append(f"  {cat_name}: {total_in_cat}")
            else:
                lines.append(f"  {cat_name}: {enabled_in_cat}/{total_in_cat}")

        lines.append("")
        lines.append("Thread Overrides")
        override_lines: list[str] = []
        if thread_cfg:
            instructions = thread_cfg.get("instructions")
            if instructions:
                override_lines.append(f"  instructions: {len(instructions)} chars")
            if disabled:
                override_lines.append(f"  disabled: {', '.join(sorted(disabled))}")
            if extra_enabled:
                override_lines.append(f"  enabled: {', '.join(sorted(extra_enabled))}")
        if not override_lines:
            override_lines.append("  none (using global defaults)")
        lines.extend(override_lines)

        lines.append("")
        lines.append(f"thread: {self.thread_id}")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_tasks(self, args: list[str], rest: str) -> str:
        filter_val = args[0].lower() if args else "active"
        items = await self.api.list_todos(self.user_id)
        if filter_val == "active":
            items = [i for i in items if i.get("status") != "done"]
        elif filter_val != "all":
            items = [i for i in items if i.get("status") == filter_val]

        if not items:
            return f"[Info]: No {filter_val} tasks."

        lines = [f"Scheduled Tasks ({filter_val}): {len(items)} items"]
        for item in items[:25]:
            st = item.get("status", "pending")
            task = item.get("task", "")[:80]
            todo_id = item.get("id", "")[:8]
            detail_parts = [f"id={todo_id}", f"status={st}"]
            scheduled = item.get("scheduled_for")
            if scheduled:
                detail_parts.append(f"fires={scheduled[:16]}")
            recurrence = item.get("recurrence")
            if recurrence:
                detail_parts.append(f"repeat={recurrence}")
            lines.append(f"- {task}")
            lines.append("  " + " | ".join(detail_parts))
        if len(items) > 25:
            lines.append(f"(showing 25 of {len(items)})")
        return "[Info]: " + "\n".join(lines)

    # ── Model / thinking ──────────────────────────────────────────────────

    async def _cmd_model(self, args: list[str], rest: str) -> str:
        if not args:
            settings = await self.api.get_settings()
            tc = await self.api.get_thread_config(self.thread_id) if self.thread_id else None
            llm_cfg = (tc or {}).get("llm_config") or {}
            thread_model = llm_cfg.get("model")
            lines = [
                f"global: {settings.get('llm_model', '?')} ({settings.get('llm_provider', '?')})"
            ]
            if thread_model:
                lines.append(f"this thread: {thread_model} (override)")
            else:
                lines.append("this thread: using global default")
            return "[Info]: " + "\n".join(lines)

        name = args[0]
        scope = args[1].lower() if len(args) > 1 else "global"
        if scope == "thread":
            thread_error = self._require_thread()
            if thread_error:
                return thread_error
            await self.api.update_thread_config(
                self.thread_id, user_id=self.user_id, llm_config={"model": name}
            )
            return f"[Success]: Model for this thread set to {name}."
        if scope == "global":
            result = await self.api.update_settings(user_id=self.user_id, llm_model=name)
            msg = f"[Success]: Global model set to {name}."
            if result.get("restart_required"):
                msg += " (restart required to take effect)"
            return msg
        return "[Error]: scope must be 'global' or 'thread'."

    async def _cmd_models(self, args: list[str], rest: str) -> str:
        models = await self.api.list_available_models()
        settings = await self.api.get_settings()
        current = settings.get("llm_model", "")
        if not models:
            return "[Info]: No models returned from provider."
        lines = [
            f"Available Models: {len(models)} from {settings.get('llm_provider', '?')}"
        ]
        for m in models[:25]:
            model_id = m.get("id") or m.get("name", "?")
            ctx_len = m.get("context_length") or m.get("context_window")
            ctx_str = f" | {fmt_tokens(ctx_len)} ctx" if ctx_len else ""
            marker = " (current)" if model_id == current else ""
            lines.append(f"- {model_id}{marker}{ctx_str}")
        if len(models) > 25:
            lines.append(f"(showing 25 of {len(models)})")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_think(self, args: list[str], rest: str) -> str:
        if not args:
            settings = await self.api.get_settings()
            thinking = settings.get("llm_extended_thinking", False)
            effort = settings.get("llm_reasoning_effort")
            if not thinking:
                return "[Info]: Thinking is off."
            if effort:
                return f"[Info]: Thinking is on (effort: {effort})."
            return "[Info]: Thinking is on."
        value = args[0].lower()
        if value == "off":
            await self.api.update_settings(
                user_id=self.user_id,
                llm_extended_thinking=False,
                llm_reasoning_effort=None,
            )
            return "[Success]: Thinking disabled."
        if value == "on":
            await self.api.update_settings(
                user_id=self.user_id, llm_extended_thinking=True
            )
            return "[Success]: Thinking enabled."
        if value in ("low", "medium", "high"):
            await self.api.update_settings(
                user_id=self.user_id,
                llm_extended_thinking=True,
                llm_reasoning_effort=value,
            )
            return f"[Success]: Thinking enabled, effort: {value}."
        return "[Error]: Usage: /think [off|on|low|medium|high]"

    # ── Config ────────────────────────────────────────────────────────────

    async def _cmd_config_show(self, args: list[str], rest: str) -> str:
        settings = await self.api.get_settings()
        effort = settings.get("llm_reasoning_effort")
        base_url = settings.get("llm_base_url")

        lines = ["Settings", "", "LLM"]
        lines.append(f"  provider: {settings.get('llm_provider', '?')}")
        lines.append(f"  model: {settings.get('llm_model', '?')}")
        lines.append(f"  temperature: {settings.get('llm_temperature', '?')}")
        lines.append(f"  thinking: {'on' if settings.get('llm_extended_thinking') else 'off'}")
        if effort:
            lines.append(f"  reasoning effort: {effort}")
        if base_url:
            lines.append(f"  base url: {base_url}")

        lines.append("")
        lines.append("Context")
        lines.append(f"  mode: {settings.get('context_management', '?')}")
        threshold = settings.get("compact_threshold", 0) or 0
        lines.append(f"  compact threshold: {int(threshold * 100)}%")
        lines.append(f"  keep messages: {settings.get('compact_keep_messages', '?')}")
        compact_model = settings.get("compact_model")
        if compact_model:
            lines.append(f"  compact model: {compact_model}")

        lines.append("")
        lines.append("System")
        lines.append(f"  log level: {settings.get('log_level', '?')}")
        lines.append(f"  watchdog: {'on' if settings.get('watchdog_enabled') else 'off'}")
        if settings.get("watchdog_enabled"):
            lines.append(f"  watchdog interval: {settings.get('watchdog_interval_minutes', '?')}m")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_config_get(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /config get <key>"
        key = args[0]
        settings = await self.api.get_settings()
        if key in settings:
            return f"[Info]: {key} = {settings[key]}"
        available = ", ".join(sorted(settings.keys())[:30])
        return f"[Error]: Unknown setting '{key}'. Available: {available}"

    async def _cmd_config_set(self, args: list[str], rest: str) -> str:
        if len(args) < 2:
            return "[Error]: Usage: /config set <key> <value>"
        key = args[0]
        value_str = " ".join(args[1:])
        parsed = coerce_value(value_str)
        result = await self.api.update_settings(user_id=self.user_id, **{key: parsed})
        msg = f"[Success]: {key} set to {parsed}."
        if result.get("restart_required"):
            msg += " (restart required to take effect)"
        return msg

    # ── Env ───────────────────────────────────────────────────────────────

    async def _cmd_env_show(self, args: list[str], rest: str) -> str:
        data = await self.api.get_env_vars(user_id=self.user_id)
        entries = data.get("entries", [])
        by_cat: dict[str, list] = {}
        for e in entries:
            by_cat.setdefault(e["category"], []).append(e)
        total_set = sum(1 for e in entries if e["is_set"])
        lines = [f"Environment Variables: {len(entries)} total ({total_set} set)"]
        for cat, items in by_cat.items():
            lines.append("")
            lines.append(f"{cat}")
            for e in items:
                if e["is_set"]:
                    mark = "[secret]" if e["is_secret"] else "[set]   "
                    lines.append(f"  {mark} {e['name']} = {e['value']}")
                else:
                    lines.append(f"  [unset]  {e['name']}")
        lines.append("")
        lines.append("Use /env get <key> for unmasked values.")
        return _truncate("[Info]: " + "\n".join(lines))

    async def _cmd_env_get(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /env get <key>"
        key = args[0]
        try:
            data = await self.api.get_env_var(key, user_id=self.user_id)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return f"[Error]: Unknown variable '{key}'."
            raise
        val = data.get("value")
        name = data.get("name", key)
        if val:
            return f"[Info]: {name} = {val}"
        return f"[Info]: {name} is not set."

    async def _cmd_env_set(self, args: list[str], rest: str) -> str:
        if len(args) < 2:
            return "[Error]: Usage: /env set <key> <value>"
        key = args[0]
        value_str = " ".join(args[1:])
        parsed = coerce_value(value_str)
        # Mirror telegram: env set uses update_settings; the /settings model
        # maps env-var keys through.
        result = await self.api.update_settings(user_id=self.user_id, **{key: parsed})
        msg = f"[Success]: {key} set to {parsed}."
        if result.get("restart_required"):
            msg += " (restart required to take effect)"
        return msg

    # ── Tools ─────────────────────────────────────────────────────────────

    async def _resolve_tool_names(self, name: str):
        name_key = name.lower().strip().replace("-", "_")
        cat_data = await self.api.get_tool_categories()
        categories = cat_data.get("categories", {})
        if name_key in categories:
            return (categories[name_key], True, name_key, None)
        data = await self.api.get_default_tools(self.user_id)
        available = data.get("available_tools", [])
        all_names = {t["name"] for t in available}
        if name_key in all_names:
            return ([name_key], False, None, None)
        cat_list = ", ".join(sorted(categories))
        return ([], False, None, f"Unknown tool or category '{name}'. Categories: {cat_list}")

    async def _cmd_tools_core(self, args: list[str], rest: str) -> str:
        data = await self.api.get_default_tools(self.user_id)
        default_names = set(data.get("default_tools", []))
        available = data.get("available_tools", [])
        lines = [f"Core Tools: {len(default_names)} tools"]
        for t in available:
            if t.get("name") in default_names:
                desc = (t.get("description") or "").split("\n")[0][:60]
                if desc:
                    lines.append(f"  {t['name']}: {desc}")
                else:
                    lines.append(f"  {t['name']}")
        return "[Info]: " + "\n".join(lines)

    async def _cmd_tools_optional(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        data = await self.api.get_default_tools(self.user_id)
        default_names = set(data.get("default_tools", []))
        available = data.get("available_tools", [])
        tc = await self.api.get_thread_config(self.thread_id)
        thread_extras = set(tc.get("enabled_tools", [])) if tc else set()

        cats: dict[str, list] = {}
        for t in available:
            if t.get("name") not in default_names:
                cat = t.get("category", "other")
                cats.setdefault(cat, []).append(t)

        total = sum(len(v) for v in cats.values())
        lines = [f"Optional Tools: {total} tools in {len(cats)} categories"]
        for cat_name in sorted(cats):
            entries = cats[cat_name]
            active = sum(1 for t in entries if t["name"] in thread_extras)
            names = ", ".join(t["name"] for t in entries)
            tag = f" ({active} enabled on this thread)" if active else ""
            lines.append("")
            lines.append(f"{cat_name} ({len(entries)}){tag}")
            lines.append(f"  {names}")
        return _truncate("[Info]: " + "\n".join(lines))

    async def _cmd_tools_enabled(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        data = await self.api.get_default_tools(self.user_id)
        default_names = set(data.get("default_tools", []))
        available = data.get("available_tools", [])
        tc = await self.api.get_thread_config(self.thread_id)
        thread_extras = set(tc.get("enabled_tools", [])) if tc else set()
        thread_disabled = set(tc.get("disabled_tools", [])) if tc else set()
        all_enabled = (default_names | thread_extras) - thread_disabled

        lines = [f"Enabled Tools on this thread: {len(all_enabled)} active"]
        core_active = sorted(n for n in all_enabled if n in default_names)
        lines.append("")
        lines.append(f"Core ({len(core_active)}):")
        lines.append(f"  {', '.join(core_active) if core_active else 'none'}")

        disabled_core = sorted(thread_disabled & default_names)
        if disabled_core:
            lines.append("")
            lines.append(f"Core disabled on this thread ({len(disabled_core)}):")
            lines.append(f"  {', '.join(disabled_core)}")

        optional_active = sorted(n for n in all_enabled if n not in default_names)
        if optional_active:
            avail_by_name = {t["name"]: t for t in available}
            lines.append("")
            lines.append(f"Optional enabled ({len(optional_active)}):")
            for name in optional_active:
                t = avail_by_name.get(name, {})
                desc = (t.get("description") or "").split("\n")[0][:60]
                if desc:
                    lines.append(f"  {name}: {desc}")
                else:
                    lines.append(f"  {name}")
        else:
            lines.append("")
            lines.append("Optional enabled: none")
        return _truncate("[Info]: " + "\n".join(lines))

    async def _cmd_tools_category(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        if not args:
            return "[Error]: Usage: /tools category <name>"
        cat_name = args[0]
        data = await self.api.get_default_tools(self.user_id)
        default_names = set(data.get("default_tools", []))
        available = data.get("available_tools", [])
        tc = await self.api.get_thread_config(self.thread_id)
        thread_extras = set(tc.get("enabled_tools", [])) if tc else set()
        thread_disabled = set(tc.get("disabled_tools", [])) if tc else set()
        all_enabled = (default_names | thread_extras) - thread_disabled

        cat_key = cat_name.lower().strip().replace("-", "_")
        cats: dict[str, list] = {}
        for t in available:
            cat = t.get("category", "other")
            cats.setdefault(cat, []).append(t)
        if cat_key not in cats:
            return f"[Error]: Unknown category '{cat_name}'. Available: {', '.join(sorted(cats))}"

        entries = cats[cat_key]
        lines = [f"Tools in category '{cat_key}': {len(entries)} tools"]
        for t in entries:
            tool_name = t["name"]
            enabled = tool_name in all_enabled
            is_default = tool_name in default_names
            mark = "[on] " if enabled else "[off]"
            tag = " (core)" if is_default else ""
            desc = (t.get("description") or "").split("\n")[0][:60]
            if desc:
                lines.append(f"  {mark} {tool_name}{tag}: {desc}")
            else:
                lines.append(f"  {mark} {tool_name}{tag}")
        return _truncate("[Info]: " + "\n".join(lines))

    async def _cmd_tools_enable(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        if not args:
            return "[Error]: Usage: /tools enable <tool_or_category>"
        name = args[0]
        tool_names, is_category, cat_name, error = await self._resolve_tool_names(name)
        if error:
            return f"[Error]: {error}"
        tc = await self.api.get_thread_config(self.thread_id)
        current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
        current_disabled = set(tc.get("disabled_tools", [])) if tc else set()
        new_enabled = current_enabled | set(tool_names)
        new_disabled = current_disabled - set(tool_names)
        await self.api.update_thread_config(
            self.thread_id,
            user_id=self.user_id,
            enabled_tools=sorted(new_enabled),
            disabled_tools=sorted(new_disabled),
        )
        if is_category:
            return f"[Success]: Enabled category '{cat_name}' ({len(tool_names)} tools)."
        return f"[Success]: Enabled tool '{tool_names[0]}'."

    async def _cmd_tools_disable(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        if not args:
            return "[Error]: Usage: /tools disable <tool_or_category>"
        name = args[0]
        tool_names, is_category, cat_name, error = await self._resolve_tool_names(name)
        if error:
            return f"[Error]: {error}"
        tc = await self.api.get_thread_config(self.thread_id)
        current_enabled = set(tc.get("enabled_tools", [])) if tc else set()
        current_disabled = set(tc.get("disabled_tools", [])) if tc else set()
        new_enabled = current_enabled - set(tool_names)
        new_disabled = current_disabled | set(tool_names)
        await self.api.update_thread_config(
            self.thread_id,
            user_id=self.user_id,
            enabled_tools=sorted(new_enabled),
            disabled_tools=sorted(new_disabled),
        )
        if is_category:
            return f"[Success]: Disabled category '{cat_name}' ({len(tool_names)} tools)."
        return f"[Success]: Disabled tool '{tool_names[0]}'."

    # ── Memory ────────────────────────────────────────────────────────────

    async def _cmd_memory_list(self, args: list[str], rest: str) -> str:
        memories = await self.api.list_memories(self.user_id)
        if not memories:
            return "[Info]: No memories saved yet."
        lines = [f"Memories: {len(memories)} stored"]
        for mem in memories[:25]:
            value = mem.get("value", "")
            preview = (value[:200] + "...") if len(value) > 200 else value
            lines.append(f"  {mem.get('key', '?')}: {preview}")
        if len(memories) > 25:
            lines.append(f"(showing 25 of {len(memories)})")
        return _truncate("[Info]: " + "\n".join(lines))

    async def _cmd_memory_save(self, args: list[str], rest: str) -> str:
        if len(args) < 2:
            return "[Error]: Usage: /memory save <key> <value>"
        key = args[0]
        value = " ".join(args[1:])
        await self.api.save_memory(self.user_id, key, value)
        return f"[Success]: Saved memory '{key}'."

    async def _cmd_memory_forget(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /memory forget <key>"
        key = args[0]
        try:
            await self.api.forget_memory(self.user_id, key)
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code == 404:
                return f"[Error]: No memory found with key '{key}'."
            raise
        return f"[Success]: Forgot memory '{key}'."

    async def _cmd_memory_search(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /memory search <query>"
        query = " ".join(args)
        results = await self.api.search_memories(self.user_id, query)
        if not results:
            return f"[Info]: No memories matching '{query}'."
        lines = [f"Memory search for '{query}': {len(results)} results"]
        for mem in results[:25]:
            value = mem.get("value", "")
            preview = (value[:200] + "...") if len(value) > 200 else value
            lines.append(f"  {mem.get('key', '?')}: {preview}")
        return _truncate("[Info]: " + "\n".join(lines))

    # ── TODOs ─────────────────────────────────────────────────────────────

    async def _cmd_todos_list(self, args: list[str], rest: str) -> str:
        filter_val = args[0].lower() if args else "active"
        items = await self.api.list_todos(self.user_id)
        if filter_val == "active":
            items = [i for i in items if i.get("status") != "done"]
        elif filter_val != "all":
            items = [i for i in items if i.get("status") == filter_val]
        if not items:
            return f"[Info]: No {filter_val} TODOs."
        lines = [f"TODOs ({filter_val}): {len(items)} items"]
        for item in items[:25]:
            st = item.get("status", "pending")
            task = item.get("task", "")[:80]
            todo_id = item.get("id", "")[:8]
            parts = [f"id={todo_id}", f"status={st}"]
            if item.get("scheduled_for"):
                parts.append(f"fires={item['scheduled_for'][:16]}")
            if item.get("recurrence"):
                parts.append(f"repeat={item['recurrence']}")
            lines.append(f"- {task}")
            lines.append("  " + " | ".join(parts))
        if len(items) > 25:
            lines.append(f"(showing 25 of {len(items)})")
        return _truncate("[Info]: " + "\n".join(lines))

    async def _cmd_todos_add(self, args: list[str], rest: str) -> str:
        if not rest.strip():
            return (
                "[Error]: Usage: /todos add <task> [| <schedule>] [| <repeat>] [| <notes>]\n"
                "Example: /todos add Check logs | 2h | daily"
            )
        parts = [p.strip() for p in rest.split("|")]
        task = parts[0]
        schedule = parts[1] if len(parts) > 1 and parts[1] else "1d"
        recurrence = parts[2] if len(parts) > 2 and parts[2] else None
        notes = parts[3] if len(parts) > 3 and parts[3] else None

        result = await self.api.add_todo(
            user_id=self.user_id,
            task=task,
            scheduled_for=schedule,
            notes=notes,
            recurrence=recurrence,
            thread_id=self.thread_id,
        )
        todo_id = result.get("id", "")[:8]
        scheduled = result.get("scheduled_for", "")
        out = [f"Created TODO {todo_id}: {task}"]
        if scheduled:
            out.append(f"Fires: {scheduled[:16]}")
        if recurrence:
            out.append(f"Repeats: {recurrence}")
        return "[Success]: " + "\n".join(out)

    async def _cmd_todos_complete(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /todos complete <todo_id>"
        todo_id = args[0]
        items = await self.api.list_todos(self.user_id)
        match = next((i for i in items if i.get("id", "").startswith(todo_id)), None)
        if not match:
            return f"[Error]: No TODO found matching '{todo_id}'."
        result = await self.api.complete_todo(self.user_id, match["id"])
        task = match.get("task", "")
        recurrence = result.get("recurrence")
        if recurrence and result.get("status") == "pending":
            next_fire = result.get("scheduled_for", "")[:16]
            return f"[Success]: Completed '{task}'. Rescheduled ({recurrence}): next fire {next_fire}."
        return f"[Success]: Completed '{task}'."

    async def _cmd_todos_delete(self, args: list[str], rest: str) -> str:
        if not args:
            return "[Error]: Usage: /todos delete <todo_id>"
        todo_id = args[0]
        items = await self.api.list_todos(self.user_id)
        match = next((i for i in items if i.get("id", "").startswith(todo_id)), None)
        if not match:
            return f"[Error]: No TODO found matching '{todo_id}'."
        await self.api.delete_todo(self.user_id, match["id"])
        return f"[Success]: Deleted '{match.get('task', '')}'."

    # ── Notepad ───────────────────────────────────────────────────────────

    async def _cmd_notepad_read(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        from ..tools.thread_notes import read_notepad
        content = read_notepad(self.thread_id)
        if not content:
            return "[Info]: Notepad is empty."
        return f"[Info]: Notepad ({len(content)} chars):\n\n{content}"

    async def _cmd_notepad_write(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        raw = rest
        if not raw:
            return "[Error]: Usage: /notepad write <content>  (or: replace:<content>)"
        from ..tools.thread_notes import _notepad_path, MAX_NOTEPAD_SIZE
        if raw.lower().startswith("replace:"):
            write_mode = "replace"
            content = raw[8:].strip()
        else:
            write_mode = "append"
            content = raw
        path = _notepad_path(self.thread_id)
        if write_mode == "append":
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            new_content = (existing.rstrip() + "\n\n" + content) if existing else content
        else:
            new_content = content
        if len(new_content.encode("utf-8")) > MAX_NOTEPAD_SIZE:
            return f"[Error]: Notepad would exceed {MAX_NOTEPAD_SIZE // 1024}KB limit."
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_content, encoding="utf-8")
        return f"[Success]: Notepad updated ({write_mode}): {len(new_content.encode('utf-8'))} bytes."

    async def _cmd_notepad_clear(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        from ..tools.thread_notes import delete_notepad
        if delete_notepad(self.thread_id):
            return "[Success]: Notepad cleared."
        return "[Info]: Notepad was already empty."

    # ── Thread lifecycle ──────────────────────────────────────────────────

    async def _cmd_stop(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        result = await self.api.stop_thread(self.thread_id)
        if result.get("status") == "stopping":
            holder = result.get("holder") or "current turn"
            held = result.get("held_seconds", 0)
            return (
                f"[Success]: Stop requested. {holder} has been running for "
                f"{held:.0f}s; will halt at the next iteration boundary."
            )
        return "[Info]: Thread is idle; nothing to stop."

    async def _cmd_clear(self, args: list[str], rest: str) -> str:
        thread_error = self._require_thread()
        if thread_error:
            return thread_error
        await self.api.clear_thread(self.thread_id)
        return "[Success]: Conversation history cleared. Notepad and tool config preserved."

    async def _cmd_restart_api(self, args: list[str], rest: str) -> str:
        await self.api.restart_api()
        return "[Success]: Restarting API server..."
