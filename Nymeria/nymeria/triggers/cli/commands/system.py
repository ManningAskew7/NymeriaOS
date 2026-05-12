"""System commands: /help, /clear, /exit, /history, /settings."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any, List, TYPE_CHECKING

from rich.panel import Panel
from rich.text import Text

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult

if TYPE_CHECKING:
    from ..state import CLIState


SAFE_SETTINGS_PATCH_FIELDS = {
    "activity_retention_hours",
    "compact_keep_messages",
    "compact_model",
    "compact_threshold",
    "context_management",
    "llm_base_url",
    "llm_extended_thinking",
    "llm_fallback_models",
    "llm_fast_model",
    "llm_frequency_penalty",
    "llm_max_tokens",
    "llm_model",
    "llm_presence_penalty",
    "llm_provider",
    "llm_reasoning_effort",
    "llm_stream_max_retries",
    "llm_stream_retry_initial_delay",
    "llm_stream_retry_max_delay",
    "llm_temperature",
    "llm_top_k",
    "llm_top_p",
    "llm_use_model_defaults",
    "log_level",
    "openai_api_mode",
    "sliding_window_cycles",
    "stt_base_url",
    "stt_language",
    "stt_model",
    "stt_provider",
    "todo_staleness_minutes",
    "tool_output_max_chars",
    "tts_base_url",
    "tts_model",
    "tts_output_format",
    "tts_provider",
    "tts_speed",
    "tts_voice",
    "voice_default_thread_id",
    "watchdog_enabled",
    "watchdog_interval_minutes",
}

CONFIRM_FLAGS = {"--yes", "-y"}


class CommandClientMethodUnavailable(RuntimeError):
    """Raised when the current command transport cannot perform an operation."""

    def __init__(self, method_name: str) -> None:
        super().__init__(method_name)
        self.method_name = method_name


def _handle_help(state: "CLIState", args: List[str]) -> None:
    """Show categorized help."""
    sections = {
        "Thread": [
            ("/threads, /t", "List all threads"),
            ("/threads switch <id>, /t s <id>", "Switch thread (partial ID)"),
            ("/threads new [name], /t n", "Create new thread"),
            ("/threads delete <id>", "Delete a thread"),
            ("/threads info", "Current thread details"),
            ("/threads rename <title>", "Rename current thread"),
            ("/branch [title], /fork", "Create a branched thread"),
        ],
        "Model": [
            ("/model", "Show effective model"),
            ("/model set <model-id>", "Set per-thread model override"),
            ("/provider [list|set|test|switch]", "Manage LLM providers"),
            ("/fallback [list|add|remove|clear|set]", "Manage model fallbacks"),
            ("/fast [prompt]", "Toggle or use the fast model for one turn"),
            ("/reasoning [on|off|low|medium|high]", "Toggle extended thinking"),
        ],
        "Tools": [
            ("/tools", "List tools for current thread"),
            ("/tools enable <name>", "Enable an optional tool"),
            ("/tools disable <name>", "Disable a tool"),
            ("/tools optional", "List all optional tools"),
        ],
        "TODOs": [
            ("/todos", "List all TODOs"),
            ("/todo add <task>", "Add a TODO"),
            ("/todo done <id>", "Complete a TODO"),
            ("/todo delete <id>", "Delete a TODO"),
        ],
        "Memory": [
            ("/memory list", "List saved memories"),
            ("/memory save <key> <value>", "Save a memory"),
            ("/memory forget <key>", "Remove a memory"),
        ],
        "Context": [
            ("/context", "Show context window stats"),
            ("/usage [session]", "Token usage and cost statistics"),
            ("/compact", "Trigger manual compaction"),
        ],
        "Session": [
            ("/export [json|md|jsonl]", "Export thread to file"),
            ("/import <file>", "Import thread from JSON file"),
            ("/copy [N | code]", "Copy response to clipboard"),
        ],
        "Conversation": [
            ("/retry [new prompt]", "Re-send last message for a new response"),
            ("/undo [--yes]", "Remove last user+assistant exchange"),
        ],
        "System": [
            ("/login [api-url], /connect", "Connect to a Nymeria API"),
            ("/logout", "Disconnect and remove saved CLI token"),
            ("/help, /h", "Show this help"),
            ("/settings", "Show global settings"),
            ("/history", "Show conversation history"),
            ("/clear", "Clear screen"),
            ("/exit, /quit", "Exit the CLI"),
        ],
    }

    body = Text()
    for section_name, cmds in sections.items():
        body.append(f"\n {section_name}\n", style="bold")
        for cmd, desc in cmds:
            body.append(f"  {cmd:<38}", style="cyan")
            body.append(f" {desc}\n", style="dim")

    state.console.print(Panel(body, title="Commands", border_style="green", padding=(0, 1)))


def _handle_clear(state: "CLIState", args: List[str]) -> None:
    """Clear terminal and re-render welcome."""
    from ..rendering.welcome import render_welcome

    state.console.clear()
    render_welcome(state)


def _handle_exit(state: "CLIState", args: List[str]) -> None:
    """Exit the CLI."""
    state.running = False
    state.console.print("[dim]Goodbye![/dim]")


def _handle_history(state: "CLIState", args: List[str]) -> None:
    """Show conversation history."""
    history = state.agent.get_conversation_history(state.thread_id)
    if not history:
        state.console.print("[dim]No conversation history.[/dim]")
        return

    for msg in history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if role == "human":
            text = content if isinstance(content, str) else str(content)
            state.console.print(f"\n[bold cyan]You:[/bold cyan] {text[:200]}")
        elif role == "assistant":
            if isinstance(content, str):
                preview = content[:200]
            elif isinstance(content, list):
                # Collect text from content blocks
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                preview = " ".join(parts)[:200]
            else:
                preview = str(content)[:200]
            if preview:
                state.console.print(f"[bold green]Nymeria:[/bold green] {preview}...")

        # Show tool calls inline
        tool_calls = msg.get("toolCalls", [])
        for tc in tool_calls:
            name = tc.get("name", "unknown")
            state.console.print(f"  [yellow]> {name}[/yellow]")


def _handle_settings(state: "CLIState", args: List[str]) -> None:
    """Display current global settings."""
    s = state.settings

    lines = [
        f"  Provider        {s.llm_provider}",
        f"  Model           {s.llm_model}",
        f"  Fast model      {s.llm_fast_model or 'auto'}",
        f"  Fallbacks       {getattr(s, 'llm_fallback_models', '') or 'none'}",
        f"  Temperature     {s.llm_temperature}",
        f"  Extended think  {s.llm_extended_thinking}",
        f"  Context mgmt    {s.context_management}",
        f"  Compact at      {int(s.compact_threshold * 100)}%",
        f"  Database        {s.database_backend}",
        f"  Watchdog        {'on' if s.watchdog_enabled else 'off'} (every {s.watchdog_interval_minutes}m)",
        f"  Timezone        {s.user_timezone}",
    ]

    if s.llm_base_url:
        lines.insert(2, f"  Base URL        {s.llm_base_url}")

    body = "\n".join(lines)
    state.console.print(Panel(body, title="Settings", border_style="dim", padding=(0, 1)))


def _method_owner(context: CommandContext, method_name: str) -> Any | None:
    """Return the object that owns a client method for API-first commands."""

    client = context.client
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


def has_client_method(context: CommandContext, method_name: str) -> bool:
    """Return whether the active transport exposes ``method_name``."""

    return _method_owner(context, method_name) is not None


async def call_client_method(
    context: CommandContext,
    method_name: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call a method on ``context.client`` or its wrapped API client."""

    owner = _method_owner(context, method_name)
    if owner is None:
        raise CommandClientMethodUnavailable(method_name)
    result = getattr(owner, method_name)(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def unsupported_transport_result(
    command: str,
    *,
    method_name: str | None = None,
) -> CommandResult:
    """Return a consistent unsupported-transport error."""

    suffix = f" Missing client method: {method_name}." if method_name else ""
    return CommandResult.failed(
        f"{command} is not available with the current transport. "
        f"Run /login to connect to a Nymeria API if needed.{suffix}",
        error_code="unsupported_transport",
        payload={"method": method_name} if method_name else None,
    )


def strip_confirmation_flags(args: Sequence[str]) -> tuple[list[str], bool]:
    """Remove explicit confirmation flags from a command argument list."""

    remaining: list[str] = []
    confirmed = False
    for arg in args:
        if str(arg).casefold() in CONFIRM_FLAGS:
            confirmed = True
        else:
            remaining.append(arg)
    return remaining, confirmed


async def confirmation_granted(
    context: CommandContext,
    prompt: str,
    *,
    explicitly_confirmed: bool = False,
) -> bool:
    """Return whether a destructive command has user confirmation."""

    if explicitly_confirmed:
        return True
    return await context.confirm(prompt, default=False)


def confirmation_required_result(command: str) -> CommandResult:
    """Return the standard confirmation-required warning."""

    return CommandResult.completed(
        CommandMessage(
            f"Confirmation required for {command}. Re-run with --yes to proceed.",
            level="warning",
        )
    )


def normalize_thread_id(thread: Mapping[str, Any]) -> str:
    """Read a thread ID from either API or local transport payload shapes."""

    return str(thread.get("thread_id") or thread.get("id") or "")


def thread_title(thread: Mapping[str, Any]) -> str:
    """Return a readable thread title."""

    title = str(thread.get("title") or "").strip()
    return title or "New Chat"


def compact_id(value: Any, *, width: int = 8) -> str:
    """Return a short display ID."""

    text = str(value or "")
    return text[:width] if len(text) > width else text


def format_bool(value: Any) -> str:
    """Format booleans for compact command output."""

    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def parse_scalar(value: str) -> Any:
    """Parse simple CLI scalar values for settings patches."""

    raw = str(value).strip()
    lowered = raw.casefold()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"none", "null"}:
        return None
    try:
        if "." not in raw and "e" not in lowered:
            return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def parse_key_values(args: Sequence[str]) -> tuple[dict[str, Any], str]:
    """Parse ``key=value`` pairs or one ``key value`` pair."""

    if not args:
        return {}, "Usage: /settings patch <key=value> [key=value...] [--yes]"

    if len(args) >= 2 and "=" not in args[0]:
        key = args[0].strip()
        value = " ".join(args[1:]).strip()
        if not key:
            return {}, "Setting key cannot be blank."
        return {key: parse_scalar(value)}, ""

    values: dict[str, Any] = {}
    for arg in args:
        if "=" not in arg:
            return {}, f"Expected key=value, got {arg!r}."
        key, value = arg.split("=", 1)
        key = key.strip()
        if not key:
            return {}, "Setting key cannot be blank."
        values[key] = parse_scalar(value)
    return values, ""


def mapping_get(data: Mapping[str, Any] | Any, key: str, default: Any = None) -> Any:
    """Read a key from a mapping-like or object-like payload."""

    if isinstance(data, Mapping):
        return data.get(key, default)
    return getattr(data, key, default)


def one_line(value: Any, *, limit: int = 200) -> str:
    """Collapse a value to one bounded display line."""

    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def format_settings_view(settings: Mapping[str, Any]) -> str:
    """Format the settings fields most useful in a terminal."""

    rows = [
        ("Provider", settings.get("llm_provider", "")),
        ("Model", settings.get("llm_model", "")),
        ("Fast model", settings.get("llm_fast_model", "") or "auto"),
        ("Fallbacks", _fallback_label(settings.get("llm_fallback_models", ""))),
        ("Temperature", settings.get("llm_temperature", "")),
        ("Max tokens", settings.get("llm_max_tokens", "")),
        ("Extended thinking", format_bool(settings.get("llm_extended_thinking", ""))),
        ("Model defaults", format_bool(settings.get("llm_use_model_defaults", ""))),
        ("Context mgmt", settings.get("context_management", "")),
        ("Compact at", _percent(settings.get("compact_threshold"))),
        ("Keep messages", settings.get("compact_keep_messages", "")),
        ("Tool output chars", settings.get("tool_output_max_chars", "")),
        ("Watchdog", format_bool(settings.get("watchdog_enabled", ""))),
        ("Watchdog interval", settings.get("watchdog_interval_minutes", "")),
        ("Log level", settings.get("log_level", "")),
    ]
    if settings.get("llm_base_url"):
        rows.insert(2, ("Base URL", settings.get("llm_base_url")))
    return "\n".join(_aligned_rows(rows, title="Settings"))


def _aligned_rows(
    rows: Sequence[tuple[str, Any]],
    *,
    title: str | None = None,
) -> list[str]:
    width = max((len(label) for label, _value in rows), default=0)
    lines = [title] if title else []
    for label, value in rows:
        rendered = "" if value is None else str(value)
        lines.append(f"  {label:<{width}}  {rendered}")
    return lines


def _fallback_label(value: Any) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return " -> ".join(items) if items else "none"
    text = str(value or "").strip()
    return text or "none"


def _percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return str(value or "")


async def _handle_history_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Show conversation history through the active client."""

    if context.legacy_state is not None:
        _handle_history(context.legacy_state, args)
        return CommandResult.completed()

    thread_id = context.thread_id
    if not thread_id:
        return CommandResult.failed("No active thread is selected.")

    include_internal = False
    limit = 20
    remaining: list[str] = []
    for arg in args:
        normalized = arg.casefold()
        if normalized in {"--internal", "--include-internal"}:
            include_internal = True
        elif normalized.startswith("--limit="):
            limit = _parse_limit(normalized.split("=", 1)[1], default=limit)
        else:
            remaining.append(arg)
    if remaining:
        limit = _parse_limit(remaining[0], default=limit)

    try:
        history = await call_client_method(
            context,
            "get_history",
            thread_id,
            user_id=context.user_id,
            include_internal=include_internal,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/history", method_name=exc.method_name)

    messages = _history_messages(history)
    if not messages:
        return CommandResult.completed(CommandMessage("No conversation history.", level="warning"))

    selected = messages[-limit:] if limit > 0 else messages
    lines = [f"History ({len(selected)} of {len(messages)} messages)"]
    for message in selected:
        lines.extend(_format_history_message(message))
    return CommandResult.completed(CommandMessage("\n".join(lines), title="History"))


def _parse_limit(value: str, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _history_messages(history: Any) -> list[Mapping[str, Any]]:
    if isinstance(history, Mapping):
        raw = history.get("messages", [])
    else:
        raw = history
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _format_history_message(message: Mapping[str, Any]) -> list[str]:
    role = str(message.get("role") or message.get("type") or "unknown")
    label = {
        "human": "You",
        "user": "You",
        "assistant": "Nymeria",
        "ai": "Nymeria",
        "system": "System",
        "tool": "Tool",
    }.get(role.casefold(), role.title())
    content = _message_content_text(message.get("content"))
    lines = [f"{label}: {one_line(content, limit=200)}" if content else f"{label}:"]

    tool_calls = message.get("toolCalls") or message.get("tool_calls") or []
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes)):
        for tool_call in tool_calls:
            if not isinstance(tool_call, Mapping):
                continue
            name = tool_call.get("name") or tool_call.get("function", {}).get("name")
            if name:
                lines.append(f"  > {name}")
    return lines


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            elif block is not None:
                parts.append(str(block))
        return " ".join(parts)
    return "" if content is None else str(content)


async def _handle_settings_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Display settings through the active client."""

    if context.legacy_state is not None:
        _handle_settings(context.legacy_state, args)
        return CommandResult.completed()

    if args:
        return CommandResult.failed(
            "Usage: /settings view or /settings patch <key=value> [--yes]",
            error_code="usage_error",
        )

    return await _handle_settings_view_context(context, args)


async def _handle_settings_view_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        settings = await call_client_method(
            context,
            "get_settings",
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/settings view", method_name=exc.method_name)

    if not isinstance(settings, Mapping):
        return CommandResult.failed("Settings response was not a mapping.")
    return CommandResult.completed(
        CommandMessage(format_settings_view(settings), title="Settings"),
        json_payload=dict(settings),
    )


async def _handle_settings_patch_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    args, explicit_confirmation = strip_confirmation_flags(args)
    values, error = parse_key_values(args)
    if error:
        return CommandResult.failed(error, error_code="usage_error")

    unsafe = sorted(set(values) - SAFE_SETTINGS_PATCH_FIELDS)
    if unsafe:
        allowed = ", ".join(sorted(SAFE_SETTINGS_PATCH_FIELDS))
        return CommandResult.failed(
            "Refusing to patch unsupported or secret settings: "
            f"{', '.join(unsafe)}\nSafe fields: {allowed}",
            error_code="unsafe_settings_patch",
        )

    confirmed = await confirmation_granted(
        context,
        f"Patch server settings: {', '.join(sorted(values))}?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return confirmation_required_result("/settings patch")

    try:
        result = await call_client_method(
            context,
            "update_settings",
            user_id=context.user_id,
            **values,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/settings patch", method_name=exc.method_name)

    updated = mapping_get(result, "updated", sorted(values))
    restart_required = bool(mapping_get(result, "restart_required", False))
    suffix = " Restart required." if restart_required else ""
    return CommandResult.completed(
        CommandMessage(
            f"Updated settings: {', '.join(str(item) for item in updated)}.{suffix}",
            level="success",
        ),
        payload={"updated": tuple(updated), "restart_required": restart_required},
    )


async def _handle_redraw_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        context.legacy_state.console.clear()
        from ..rendering.welcome import render_welcome

        render_welcome(context.legacy_state)
        return CommandResult.completed()

    await context.dispatch({"type": "redraw"})
    return CommandResult.completed("Redrawn.")


async def _handle_verbose_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) > 1 or (args and args[0] not in {"on", "off", "status"}):
        return CommandResult.failed(
            "Usage: /verbose on|off|status",
            error_code="usage_error",
        )

    enabled = bool(context.metadata.get("transcript_verbose", False))
    mode = args[0] if args else "status"
    if mode == "status":
        return CommandResult.completed(
            f"Transcript verbosity is {'on' if enabled else 'off'}.",
            payload={"suppress_transcript": True},
        )

    next_enabled = mode == "on"
    await context.dispatch(
        {"type": "set_transcript_verbose", "enabled": next_enabled}
    )
    return CommandResult.completed(
        f"Transcript verbosity is {'on' if next_enabled else 'off'}.",
        payload={"suppress_transcript": True},
    )


def register(registry: CommandRegistry) -> None:
    """Register all system commands."""
    registry.register(Command(
        name="help",
        aliases=["/h"],
        description="Show help",
        handler=_handle_help,
    ))
    registry.register(Command(
        name="clear",
        aliases=[],
        description="Clear screen",
        handler=_handle_clear,
    ))
    registry.register(Command(
        name="exit",
        aliases=["/quit", "/q"],
        description="Exit the CLI",
        handler=_handle_exit,
    ))
    registry.register(Command(
        name="history",
        aliases=[],
        description="Show conversation history",
        usage="/history [--internal] [limit]",
        handler=_handle_history_context,
        handler_mode="context",
        category="System",
    ))
    registry.register(Command(
        name="settings",
        aliases=[],
        description="View or patch global settings",
        usage="/settings view",
        handler=_handle_settings_context,
        handler_mode="context",
        category="System",
        subcommands={
            "view": Command(
                name="view",
                description="Show global settings",
                usage="view",
                handler=_handle_settings_view_context,
                handler_mode="context",
                category="System",
            ),
            "patch": Command(
                name="patch",
                description="Patch safe global settings",
                usage="patch <key=value> [key=value...] [--yes]",
                handler=_handle_settings_patch_context,
                handler_mode="context",
                category="System",
            ),
        },
    ))
    registry.register(Command(
        name="redraw",
        aliases=[],
        description="Redraw the CLI",
        usage="/redraw",
        handler=_handle_redraw_context,
        handler_mode="context",
        category="System",
    ))
    registry.register(Command(
        name="verbose",
        aliases=[],
        description="Toggle full-screen transcript verbosity",
        usage="/verbose on|off|status",
        handler=_handle_verbose_context,
        handler_mode="context",
        category="System",
    ))
