"""Plain-text slash command dispatcher for the `slash_command` agent tool.

Mirrors the user-facing slash commands exposed by the Discord and Telegram
bots, but returns plain text suitable for an agent tool (no HTML, no embeds,
no per-chat UI state). Thin wrapper over `NymeriaAPIClient`.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, Optional

import httpx

from .discord_api_client import NymeriaAPIClient

logger = logging.getLogger(__name__)


HELP_TEXT = """Nymeria slash commands (agent tool)

Status / inspection
  /help                              Show this help
  /status                            Model, context, tools, tasks summary
  /thread                            Thread id + context usage
  /context                           Detailed context + tool breakdown
  /tasks [active|pending|in_progress|done|all]  Scheduled tasks overview

LLM / thinking
  /model                             Show current model (global + thread)
  /model <name> [global|thread]      Change model (default scope: global)
  /models                            List available models from provider
  /think                             Show thinking status
  /think off|on|low|medium|high      Set thinking mode / reasoning effort

Settings
  /config show                       All settings
  /config get <key>                  One setting
  /config set <key> <value>          Update setting (types auto-coerced)

Environment variables
  /env show                          All env vars (secrets masked)
  /env get <key>                     One env var (unmasked)
  /env set <key> <value>             Set env var (persists to .env)

Tools (per-thread enable/disable)
  /tools core                        Tools enabled by default
  /tools optional                    Optional tool categories
  /tools enabled                     Tools active on this thread right now
  /tools category <name>             List tools in a category
  /tools enable <tool_or_category>   Enable for this thread
  /tools disable <tool_or_category>  Disable for this thread

Memory (user-scoped)
  /memory list                       All saved memories
  /memory save <key> <value>         Save/update memory
  /memory forget <key>               Remove memory
  /memory search <query>             Keyword search

TODOs (user-scoped)
  /todos list [filter]               filter: active|pending|in_progress|done|all
  /todos add <task> [| sched] [| repeat] [| notes]
                                     Example: /todos add Check logs | 2h | daily
  /todos complete <id>               Mark done (id prefix ok)
  /todos delete <id>                 Delete permanently

Notepad (this thread)
  /notepad read                      Read notepad
  /notepad write <content>           Append
  /notepad write replace:<content>   Overwrite
  /notepad clear                     Clear notepad

Notes:
- Pass commands with or without the leading /
- Values with spaces can be quoted: /memory save color "deep blue"
- Destructive commands (/ask /stop /clear /compact /restart) are disabled
  for the agent
"""


def _coerce(value_str: str) -> Any:
    """Auto-convert str values to bool/None/int/float/str.

    Matches the logic used in telegram_bot._cmd_config_set.
    """
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


def _truncate(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 80] + f"\n\n[Info]: output truncated (was {len(text)} chars)"


def _fmt_tokens(n: Optional[int]) -> str:
    if not n:
        return "0"
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def _http_error_detail(exc: httpx.HTTPStatusError) -> str:
    resp = exc.response
    if resp is not None:
        try:
            body = resp.json()
            detail = body.get("detail")
            if detail:
                return str(detail)
        except Exception:
            pass
        return f"HTTP {resp.status_code}"
    return str(exc)


class SlashCommandDispatcher:
    """Dispatch parsed slash commands against NymeriaAPIClient, return plain text."""

    # Commands the agent is not allowed to invoke on its own thread.
    BLOCKED = {"ask", "stop", "clear", "restart", "compact", "start"}

    def __init__(self, api: NymeriaAPIClient, thread_id: str, user_id: str):
        self.api = api
        self.thread_id = thread_id
        self.user_id = user_id

    async def dispatch(
        self,
        command: str,
        subcommand: Optional[str],
        args: list[str],
        rest: str,
    ) -> str:
        """Route to the matching _cmd_* method.

        `args` is shlex-split tokens after the subcommand.
        `rest` is the raw remainder after the subcommand (preserves pipes etc).
        """
        if command in self.BLOCKED:
            return (
                f"[Error]: Command '/{command}' is disabled for the agent "
                f"(would interrupt or destroy the current conversation). "
                f"Use /help to see allowed commands."
            )

        method_name = f"_cmd_{command}"
        if subcommand:
            method_name += f"_{subcommand}"
        method = getattr(self, method_name, None)

        if method is None:
            # Check whether the command itself is known but subcommand wasn't
            group_exists = any(
                m.startswith(f"_cmd_{command}_")
                for m in dir(self)
            )
            if group_exists and subcommand:
                valid = sorted(
                    m[len(f"_cmd_{command}_"):]
                    for m in dir(self)
                    if m.startswith(f"_cmd_{command}_")
                )
                return (
                    f"[Error]: Unknown subcommand '{subcommand}' for /{command}. "
                    f"Valid: {', '.join(valid)}."
                )
            if group_exists and not subcommand:
                valid = sorted(
                    m[len(f"_cmd_{command}_"):]
                    for m in dir(self)
                    if m.startswith(f"_cmd_{command}_")
                )
                return (
                    f"[Error]: /{command} requires a subcommand. "
                    f"Valid: {', '.join(valid)}."
                )
            return f"[Error]: Unknown command '/{command}'. Use /help."

        try:
            return await method(args, rest)
        except httpx.HTTPStatusError as e:
            return f"[Error]: {_http_error_detail(e)}"
        except Exception as e:
            logger.exception("slash_command dispatch failed for /%s %s", command, subcommand)
            return f"[Error]: {e}"

    # ── Help ──────────────────────────────────────────────────────────────

    async def _cmd_help(self, args: list[str], rest: str) -> str:
        return HELP_TEXT

    # ── Status / inspection ───────────────────────────────────────────────

    async def _cmd_status(self, args: list[str], rest: str) -> str:
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
            f"  {_fmt_tokens(total)} / {_fmt_tokens(limit)} tokens ({pct}%)",
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
        stats = await self.api.get_context_stats(self.thread_id)
        lines = [
            "Thread Info",
            f"  thread id: {self.thread_id}",
            f"  tokens: {_fmt_tokens(stats.get('total_tokens', 0))} / "
            f"{_fmt_tokens(stats.get('context_limit', 0))} "
            f"({stats.get('usage_percentage', 0)}%)",
            f"  compactions: {stats.get('compaction_count', 0)}",
            f"  mode: {stats.get('context_management', 'unknown')}",
        ]
        return "[Info]: " + "\n".join(lines)

    async def _cmd_context(self, args: list[str], rest: str) -> str:
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
        token_line = f"  {_fmt_tokens(total)} / {_fmt_tokens(limit)} tokens ({pct}%)"
        if cumulative:
            token_line += f" (cumulative: {_fmt_tokens(cumulative)})"
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
            lines.append(f"  " + " | ".join(detail_parts))
        if len(items) > 25:
            lines.append(f"(showing 25 of {len(items)})")
        return "[Info]: " + "\n".join(lines)

    # ── Model / thinking ──────────────────────────────────────────────────

    async def _cmd_model(self, args: list[str], rest: str) -> str:
        if not args:
            settings = await self.api.get_settings()
            tc = await self.api.get_thread_config(self.thread_id)
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
            ctx_str = f" | {_fmt_tokens(ctx_len)} ctx" if ctx_len else ""
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
        parsed = _coerce(value_str)
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
        parsed = _coerce(value_str)
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
        from ..tools.thread_notes import read_notepad
        content = read_notepad(self.thread_id)
        if not content:
            return "[Info]: Notepad is empty."
        return f"[Info]: Notepad ({len(content)} chars):\n\n{content}"

    async def _cmd_notepad_write(self, args: list[str], rest: str) -> str:
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
        from ..tools.thread_notes import delete_notepad
        if delete_notepad(self.thread_id):
            return "[Success]: Notepad cleared."
        return "[Info]: Notepad was already empty."
