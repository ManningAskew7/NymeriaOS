"""CLI doctor diagnostics."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    CommandClientMethodUnavailable,
    call_client_method,
    format_bool,
    mapping_get,
    one_line,
    unsupported_transport_result,
)
from ..rendering.details import render_details
from ..state import CLIUIState


async def _handle_doctor_root(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /doctor terminal|api|auth|model",
            error_code="usage_error",
        )
    checks = [
        await _handle_doctor_terminal(context, []),
        await _handle_doctor_api(context, []),
        await _handle_doctor_auth(context, []),
        await _handle_doctor_model(context, []),
    ]
    lines = ["Doctor"]
    for result in checks:
        if result.messages:
            lines.append("")
            lines.append(result.messages[0].content)
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Doctor"))


async def _handle_doctor_terminal(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    capabilities = context.metadata.get("capabilities")
    if capabilities is None:
        return CommandResult.completed(
            CommandMessage("Terminal\n  Capabilities unavailable.", level="warning")
        )

    data = asdict(capabilities) if is_dataclass(capabilities) else vars(capabilities)
    rows = [
        ("Renderer", data.get("renderer")),
        ("Reason", data.get("renderer_reason")),
        ("TTY stdin", format_bool(data.get("stdin_isatty"))),
        ("TTY stdout", format_bool(data.get("stdout_isatty"))),
        ("TERM", data.get("term")),
        ("CI", format_bool(data.get("ci"))),
        ("Color", f"{format_bool(data.get('color_enabled'))} depth={data.get('color_depth')}"),
        ("Unicode", format_bool(data.get("unicode_enabled"))),
        ("Alt screen", format_bool(data.get("alt_screen_enabled"))),
        ("Animation", format_bool(data.get("animation_enabled"))),
        ("Mouse", format_bool(data.get("mouse_enabled"))),
        ("Size", f"{data.get('width')}x{data.get('height')}"),
    ]
    return CommandResult.completed(CommandMessage(_rows("Terminal", rows), title="Doctor"))


async def _handle_doctor_api(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        healthy = await call_client_method(context, "health")
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/doctor api", method_name=exc.method_name)
    label = getattr(context.client, "connection_label", "unknown")
    status = "ok" if healthy else "failed"
    return CommandResult.completed(
        CommandMessage(
            _rows("API", [("Connection", label), ("Health", status)]),
            level="success" if healthy else "error",
            title="Doctor",
        )
    )


async def _handle_doctor_auth(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        try:
            me = await call_client_method(context, "get_me", act_as=context.user_id)
        except TypeError:
            me = await call_client_method(context, "get_me", context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/doctor auth", method_name=exc.method_name)

    rows = [
        ("Selected user", context.user_id),
        ("Resolved ID", mapping_get(me, "id", "")),
        ("Display name", mapping_get(me, "display_name", "")),
        ("Role", mapping_get(me, "role", "")),
    ]
    return CommandResult.completed(CommandMessage(_rows("Auth", rows), title="Doctor"))


async def _handle_doctor_model(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        diagnostics = await call_client_method(
            context,
            "get_llm_runtime_diagnostics",
            user_id=context.user_id,
        )
    except TypeError:
        diagnostics = await call_client_method(
            context,
            "get_llm_runtime_diagnostics",
            context.user_id,
        )
    except CommandClientMethodUnavailable:
        try:
            settings = await call_client_method(context, "get_settings", user_id=context.user_id)
        except CommandClientMethodUnavailable as exc:
            return unsupported_transport_result("/doctor model", method_name=exc.method_name)
        diagnostics = {
            "provider": mapping_get(settings, "llm_provider", ""),
            "model": mapping_get(settings, "llm_model", ""),
        }

    rows = [
        ("Provider", mapping_get(diagnostics, "provider", mapping_get(diagnostics, "llm_provider", ""))),
        ("Model", mapping_get(diagnostics, "model", mapping_get(diagnostics, "llm_model", ""))),
        ("Mode", mapping_get(diagnostics, "mode", "")),
        ("Base URL", one_line(mapping_get(diagnostics, "base_url", ""), limit=80)),
        ("Status", mapping_get(diagnostics, "status", "available")),
    ]
    return CommandResult.completed(CommandMessage(_rows("Model", rows), title="Doctor"))


def _rows(title: str, rows: list[tuple[str, Any]]) -> str:
    width = max((len(label) for label, _value in rows), default=0)
    lines = [title]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {'' if value is None else value}")
    return "\n".join(lines)


async def _handle_details(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    state = context.metadata.get("ui_state")
    target = "recent"
    ref = ""
    limit = 4000
    remaining: list[str] = []
    for arg in args:
        if arg == "--full":
            limit = 0
        elif arg.startswith("--limit="):
            try:
                limit = max(100, int(arg.split("=", 1)[1]))
            except ValueError:
                limit = 4000
        else:
            remaining.append(arg)
    if remaining:
        target = remaining[0]
    if len(remaining) > 1:
        ref = remaining[1]
    content = render_details(
        state if isinstance(state, CLIUIState) else None,
        target=target,
        ref=ref,
        limit=limit,
    )
    return CommandResult.completed(CommandMessage(content, title="Details"))


def register(registry: CommandRegistry) -> None:
    """Register doctor commands."""
    registry.register(Command(
        name="details",
        aliases=[],
        description="Inspect recent hidden transcript details",
        usage="/details [tool|thinking|artifact|error] [id-or-index]",
        handler=_handle_details,
        handler_mode="context",
        category="Personal",
    ))
    registry.register(Command(
        name="doctor",
        aliases=[],
        description="Run CLI diagnostics",
        usage="/doctor terminal",
        handler=_handle_doctor_root,
        handler_mode="context",
        category="System",
        subcommands={
            "terminal": Command(
                name="terminal",
                description="Show terminal capability diagnostics",
                usage="terminal",
                handler=_handle_doctor_terminal,
                handler_mode="context",
                category="System",
            ),
            "api": Command(
                name="api",
                description="Check API connectivity",
                usage="api",
                handler=_handle_doctor_api,
                handler_mode="context",
                category="System",
            ),
            "auth": Command(
                name="auth",
                description="Check API auth identity",
                usage="auth",
                handler=_handle_doctor_auth,
                handler_mode="context",
                category="System",
            ),
            "model": Command(
                name="model",
                description="Show model/provider diagnostics",
                usage="model",
                handler=_handle_doctor_model,
                handler_mode="context",
                category="System",
            ),
        },
    ))
