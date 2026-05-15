"""Central markdown slash-command service.

The service owns the command registry and parses a raw slash-command string
once. Command bodies currently keep the working REST loopback behavior from
the original trigger-layer dispatcher; future work can replace individual
calls with direct service calls behind this same interface.
"""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import quote

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

CommandSource = Literal["user", "agent", "cli"]

GROUPED = {"config", "env", "tools", "memory", "notepad", "todos"}
AGENT_BLOCKED = {"ask", "stop", "clear", "restart", "compact", "start"}


@dataclass(frozen=True)
class CommandContext:
    user_id: str
    thread_id: str | None = None
    source: CommandSource = "user"


@dataclass(frozen=True)
class CommandResult:
    success: bool
    markdown: str
    command: str


@dataclass(frozen=True)
class CommandInfo:
    name: str
    description: str
    usage: str
    category: str
    subcommands: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _CommandDefinition:
    name: str
    description: str
    usage: str
    category: str
    aliases: tuple[str, ...] = ()
    subcommands: tuple[str, ...] = ()
    agent_allowed: bool = True
    executable: bool = True
    note: str | None = None


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
        pass
    try:
        return float(value_str)
    except ValueError:
        pass
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


def _resolve_base_url() -> str:
    explicit = os.environ.get("NYMERIA_API_URL")
    if explicit:
        return explicit.rstrip("/")
    if Path("/.dockerenv").exists():
        return "http://api:8000"
    return "http://localhost:8000"


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
    """Small REST loopback client used by CommandService.

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


class CommandService:
    """Registry and dispatcher for Nymeria slash commands."""

    def __init__(self) -> None:
        self._commands: dict[str, _CommandDefinition] = {}
        self._aliases: dict[str, str] = {}
        self._register_defaults()

    def register(
        self,
        name: str,
        handler: Any = None,
        *,
        description: str,
        category: str,
        usage: str | None = None,
        aliases: tuple[str, ...] = (),
        subcommands: tuple[str, ...] = (),
        agent_allowed: bool = True,
        executable: bool = True,
        note: str | None = None,
    ) -> None:
        del handler
        normalized = name.lower().lstrip("/")
        self._commands[normalized] = _CommandDefinition(
            name=normalized,
            description=description,
            usage=usage or f"/{normalized}",
            category=category,
            aliases=aliases,
            subcommands=subcommands,
            agent_allowed=agent_allowed,
            executable=executable,
            note=note,
        )
        for alias in aliases:
            self._aliases[alias.lower().lstrip("/")] = normalized

    def _register_defaults(self) -> None:
        self.register("help", description="Show available commands", category="General", aliases=("h",))
        self.register("status", description="Model, context, tools, and task summary", category="Status")
        self.register("thread", description="Show active thread context usage", category="Status")
        self.register("context", description="Detailed context and tool breakdown", category="Status")
        self.register("tasks", description="Scheduled tasks overview", category="TODOs", usage="/tasks [active|pending|in_progress|done|all]")
        self.register("model", description="Show or change the model", category="LLM", usage="/model [name] [global|thread]")
        self.register("models", description="List available provider models", category="LLM")
        self.register("think", description="Show or change thinking mode", category="LLM", usage="/think [off|on|low|medium|high]")
        self.register("config", description="Manage server settings", category="Settings", usage="/config <show|get|set> [args]", subcommands=("show", "get", "set"))
        self.register("env", description="Manage environment variables", category="Settings", usage="/env <show|get|set> [args]", subcommands=("show", "get", "set"))
        self.register("tools", description="Inspect or change thread tools", category="Tools", usage="/tools <core|optional|enabled|category|enable|disable> [args]", subcommands=("core", "optional", "enabled", "category", "enable", "disable"))
        self.register("memory", description="Manage saved memories", category="Memory", usage="/memory <list|save|forget|search> [args]", subcommands=("list", "save", "forget", "search"))
        self.register("todos", description="Manage TODOs", category="TODOs", usage="/todos <list|add|complete|delete> [args]", subcommands=("list", "add", "complete", "delete"))
        self.register("notepad", description="Manage this thread's notepad", category="Thread", usage="/notepad <read|write|clear> [args]", subcommands=("read", "write", "clear"))
        self.register(
            "compact",
            description="Compact the active chat context",
            category="Thread",
            usage="/compact",
            agent_allowed=False,
            executable=False,
            note="Handled by the chat stream endpoint.",
        )

    def list_commands(self, source: str = "user") -> list[CommandInfo]:
        return [
            CommandInfo(
                name=cmd.name,
                description=cmd.description,
                usage=cmd.usage,
                category=cmd.category,
                subcommands=list(cmd.subcommands),
            )
            for cmd in sorted(self._commands.values(), key=lambda c: (c.category, c.name))
            if source != "agent" or cmd.agent_allowed
        ]

    def _resolve_name(self, name: str) -> str:
        return self._aliases.get(name, name)

    def _help_markdown(self, source: str) -> str:
        commands = self.list_commands(source)
        lines = ["## Nymeria Slash Commands", ""]
        categories = sorted({cmd.category for cmd in commands})
        for category in categories:
            lines.append(f"### {category}")
            lines.append("")
            lines.append("| Command | Usage | Description |")
            lines.append("| --- | --- | --- |")
            for cmd in [c for c in commands if c.category == category]:
                desc = cmd.description
                definition = self._commands.get(cmd.name)
                if definition and definition.note:
                    desc = f"{desc}. {definition.note}"
                lines.append(f"| `/{cmd.name}` | `{cmd.usage}` | {desc} |")
            lines.append("")
        lines.append("Values with spaces can be quoted, for example `/memory save color \"deep blue\"`.")
        return "\n".join(lines).strip()

    async def execute(
        self,
        ctx: CommandContext,
        raw_command: str,
        *,
        api: Any | None = None,
    ) -> CommandResult:
        command, subcommand, args, rest = parse_command(raw_command)
        if command is None:
            return CommandResult(False, "**Error:** Empty command. Try `/help`.", "")

        command = self._resolve_name(command)
        command_label = f"{command} {subcommand}".strip() if subcommand else command

        if ctx.source == "agent" and command in AGENT_BLOCKED:
            return CommandResult(
                False,
                (
                    f"**Error:** Command `/{command}` is disabled for the agent "
                    "because it would interrupt or destroy the current conversation."
                ),
                command_label,
            )

        definition = self._commands.get(command)
        if definition is None:
            return CommandResult(False, f"**Error:** Unknown command `/{command}`. Use `/help`.", command_label)
        if ctx.source == "agent" and not definition.agent_allowed:
            return CommandResult(False, f"**Error:** Command `/{command}` is not available to the agent.", command_label)
        if not definition.executable:
            return CommandResult(
                False,
                f"**Error:** `/{command}` is handled outside the command service. {definition.note or ''}".strip(),
                command_label,
            )

        if command == "help":
            return CommandResult(True, self._help_markdown(ctx.source), command_label)

        owns_api = api is None
        client = api
        if client is None:
            try:
                client = CommandHttpClient.from_service_token()
            except RuntimeError as exc:
                return CommandResult(False, f"**Error:** {exc}", command_label)

        executor = _CommandExecutor(api=client, thread_id=ctx.thread_id, user_id=ctx.user_id)

        method_name = f"_cmd_{command}"
        if subcommand:
            method_name += f"_{subcommand}"
        method = getattr(executor, method_name, None)

        if method is None:
            if definition.subcommands and subcommand:
                valid = ", ".join(definition.subcommands)
                return CommandResult(
                    False,
                    f"**Error:** Unknown subcommand `{subcommand}` for `/{command}`. Valid: {valid}.",
                    command_label,
                )
            if definition.subcommands and not subcommand:
                valid = ", ".join(definition.subcommands)
                return CommandResult(
                    False,
                    f"**Error:** `/{command}` requires a subcommand. Valid: {valid}.",
                    command_label,
                )
            return CommandResult(False, f"**Error:** Unknown command `/{command}`. Use `/help`.", command_label)

        try:
            raw_output = await method(args, rest)
            success, markdown = _format_legacy_output(raw_output)
            return CommandResult(success, _truncate(markdown), command_label)
        except httpx.HTTPStatusError as e:
            return CommandResult(False, f"**Error:** {http_error_detail(e)}", command_label)
        except Exception as e:
            logger.exception("command dispatch failed for /%s %s", command, subcommand)
            return CommandResult(False, f"**Error:** {e}", command_label)
        finally:
            if owns_api and hasattr(client, "close"):
                await client.close()


_COMMAND_SERVICE: CommandService | None = None


def get_command_service() -> CommandService:
    global _COMMAND_SERVICE
    if _COMMAND_SERVICE is None:
        _COMMAND_SERVICE = CommandService()
    return _COMMAND_SERVICE

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

    # ── Help ──────────────────────────────────────────────────────────────

    async def _cmd_help(self, args: list[str], rest: str) -> str:
        return get_command_service()._help_markdown("user")

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
