"""Tool commands: /tools list, enable, disable, defaults, test."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, TYPE_CHECKING

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    CommandClientMethodUnavailable,
    call_client_method,
    compact_id,
    mapping_get,
    one_line,
    unsupported_transport_result,
)
from ..rendering.tables import render_tools_table

if TYPE_CHECKING:
    from ..state import CLIState


def _user_role(state: "CLIState") -> str:
    try:
        user = state.agent.accounts_repo.get_user_by_id(state.user_id)
        return user.role if user else "user"
    except Exception:
        return "user"


def _get_loaded_tools(state: "CLIState") -> list[dict[str, str]]:
    """Get tools currently loaded for this thread."""
    from ....tools import ALL_TOOLS, OPTIONAL_TOOLS

    tc = state.thread_config_manager.get_config(state.thread_id)
    disabled = set(tc.disabled_tools) if tc else set()
    enabled = set(tc.enabled_tools) if tc else set()

    tools = []
    for tool in ALL_TOOLS:
        status = "disabled" if tool.name in disabled else "enabled"
        tools.append({"name": tool.name, "category": "core", "status": status})

    for name in sorted(enabled):
        if name in OPTIONAL_TOOLS:
            tools.append({"name": name, "category": "optional", "status": "enabled"})

    callable_threads = state.thread_config_manager.list_callable_threads()
    for ct in callable_threads:
        if ct.callable_name and ct.callable_name not in disabled:
            tools.append({
                "name": ct.callable_name,
                "category": "callable",
                "status": "enabled",
            })

    return tools


def _handle_tools(state: "CLIState", _args: list[str]) -> None:
    """List tools loaded for the current thread."""
    render_tools_table(state.console, _get_loaded_tools(state))


def _handle_tools_enable(state: "CLIState", args: list[str]) -> None:
    """Enable an optional tool for this thread."""
    if not args:
        state.console.print("[red]Usage: /tools enable <name>[/red]")
        return

    tool_name = args[0]

    from ....tools import OPTIONAL_TOOLS, filter_developer_only_tools

    if tool_name not in OPTIONAL_TOOLS:
        state.console.print(f"[red]'{tool_name}' is not an optional tool.[/red]")
        state.console.print("[dim]Use /tools optional to see available tools.[/dim]")
        return
    _, blocked = filter_developer_only_tools([tool_name], _user_role(state))
    if blocked:
        state.console.print(f"[red]'{tool_name}' is developer-only.[/red]")
        return

    tc = state.thread_config_manager.get_config(state.thread_id)
    if tc is None:
        from ....core.thread_config import ThreadConfig

        tc = ThreadConfig(thread_id=state.thread_id)

    if tool_name not in tc.enabled_tools:
        tc.enabled_tools.append(tool_name)
    if tool_name in tc.disabled_tools:
        tc.disabled_tools.remove(tool_name)

    state.thread_config_manager.save_config(tc)
    state.agent.invalidate_thread_config_cache(state.thread_id)
    state.console.print(f"[green]Enabled: {tool_name}[/green]")


def _handle_tools_disable(state: "CLIState", args: list[str]) -> None:
    """Disable a tool for this thread."""
    if not args:
        state.console.print("[red]Usage: /tools disable <name>[/red]")
        return

    tool_name = args[0]
    tc = state.thread_config_manager.get_config(state.thread_id)
    if tc is None:
        from ....core.thread_config import ThreadConfig

        tc = ThreadConfig(thread_id=state.thread_id)

    if tool_name not in tc.disabled_tools:
        tc.disabled_tools.append(tool_name)
    if tool_name in tc.enabled_tools:
        tc.enabled_tools.remove(tool_name)

    state.thread_config_manager.save_config(tc)
    state.agent.invalidate_thread_config_cache(state.thread_id)
    state.console.print(f"[green]Disabled: {tool_name}[/green]")


def _handle_tools_optional(state: "CLIState", _args: list[str]) -> None:
    """List all optional tools with their status for this thread."""
    from ....tools import OPTIONAL_TOOLS, filter_discoverable_optional_tool_names

    tc = state.thread_config_manager.get_config(state.thread_id)
    enabled = set(tc.enabled_tools) if tc else set()
    visible_optional = filter_discoverable_optional_tool_names(
        OPTIONAL_TOOLS.keys(),
        _user_role(state),
    )

    tools = []
    for name in sorted(visible_optional):
        status = "enabled" if name in enabled else "disabled"
        tools.append({"name": name, "category": "optional", "status": status})

    render_tools_table(state.console, tools, title="Optional Tools")


async def _handle_tools_root_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        _handle_tools(context.legacy_state, args)
        return CommandResult.completed()
    if args:
        return CommandResult.failed(
            "Usage: /tools list|enable|disable|defaults|test",
            error_code="usage_error",
        )
    return await _handle_tools_list_context(context, [])


async def _handle_tools_list_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        _handle_tools(context.legacy_state, [])
        return CommandResult.completed()

    config = await _thread_config_or_empty(context)
    try:
        tools = await _list_tool_entries(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/tools list", method_name=exc.method_name)

    if not tools:
        return CommandResult.completed(
            CommandMessage("No tools found.", level="warning"),
            json_payload=[],
        )
    return CommandResult.completed(
        CommandMessage(_format_tools_table(tools, config), title="Tools"),
        json_payload=_tools_json_entries(tools, config),
    )


async def _handle_tools_optional_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        _handle_tools_optional(context.legacy_state, [])
        return CommandResult.completed()

    try:
        tools = await call_client_method(
            context,
            "get_optional_tools",
            user_id=context.user_id,
        )
    except TypeError:
        tools = await call_client_method(context, "get_optional_tools", context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/tools optional", method_name=exc.method_name)

    entries = _mapping_sequence(tools)
    if not entries:
        return CommandResult.completed(
            CommandMessage("No optional tools are available.", level="warning")
        )
    lines = ["Optional Tools", "  Name                           Description"]
    for tool in sorted(entries, key=lambda item: _tool_name(item)):
        lines.append(
            f"  {compact_id(_tool_name(tool), width=30):<30} "
            f"{one_line(tool.get('description', ''), limit=80)}"
        )
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Tools"))


async def _handle_tools_enable_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        _handle_tools_enable(context.legacy_state, args)
        return CommandResult.completed()
    if not args:
        return CommandResult.failed(
            "Usage: /tools enable <tool-id>",
            error_code="usage_error",
        )
    return await _set_thread_tool_state(context, args[0], enabled=True)


async def _handle_tools_disable_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        _handle_tools_disable(context.legacy_state, args)
        return CommandResult.completed()
    if not args:
        return CommandResult.failed(
            "Usage: /tools disable <tool-id>",
            error_code="usage_error",
        )
    return await _set_thread_tool_state(context, args[0], enabled=False)


async def _set_thread_tool_state(
    context: CommandContext,
    tool_name: str,
    *,
    enabled: bool,
) -> CommandResult:
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    config = await _thread_config_or_empty(context)
    enabled_tools = _string_list(config.get("enabled_tools"))
    disabled_tools = _string_list(config.get("disabled_tools"))

    if enabled:
        if tool_name not in enabled_tools:
            enabled_tools.append(tool_name)
        disabled_tools = [name for name in disabled_tools if name != tool_name]
    else:
        if tool_name not in disabled_tools:
            disabled_tools.append(tool_name)
        enabled_tools = [name for name in enabled_tools if name != tool_name]

    try:
        await call_client_method(
            context,
            "update_thread_config",
            context.thread_id,
            user_id=context.user_id,
            enabled_tools=enabled_tools,
            disabled_tools=disabled_tools,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(
            f"/tools {'enable' if enabled else 'disable'}",
            method_name=exc.method_name,
        )

    action = "Enabled" if enabled else "Disabled"
    await context.dispatch({
        "type": "thread_config_updated",
        "thread_id": context.thread_id,
    })
    return CommandResult.completed(
        CommandMessage(f"{action}: {tool_name}", level="success"),
        payload={"thread_id": context.thread_id, "tool": tool_name, "enabled": enabled},
    )


async def _handle_tools_defaults_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        return CommandResult.failed(
            "/tools defaults is only available in the new command layer.",
            error_code="legacy_command_unavailable",
        )

    if not args or args[0].casefold() in {"list", "show"}:
        return await _show_default_tools(context)

    action = args[0].casefold()
    names = [arg for arg in args[1:] if arg.strip()]

    if action in {"add", "enable"}:
        if not names:
            return CommandResult.failed(
                "Usage: /tools defaults add <tool-id> [tool-id...]",
                error_code="usage_error",
            )
        current = await _default_tool_names(context)
        return await _set_default_tools(context, sorted(set(current).union(names)))

    if action in {"remove", "disable", "rm"}:
        if not names:
            return CommandResult.failed(
                "Usage: /tools defaults remove <tool-id> [tool-id...]",
                error_code="usage_error",
            )
        remove = set(names)
        current = await _default_tool_names(context)
        return await _set_default_tools(
            context,
            [name for name in current if name not in remove],
        )

    if action == "set":
        if not names:
            return CommandResult.failed(
                "Usage: /tools defaults set <tool-id> [tool-id...]",
                error_code="usage_error",
            )
        return await _set_default_tools(context, names)

    if action == "reset":
        try:
            result = await call_client_method(
                context,
                "reset_default_tools",
                user_id=context.user_id,
            )
        except TypeError:
            result = await call_client_method(
                context,
                "reset_default_tools",
                context.user_id,
            )
        except CommandClientMethodUnavailable as exc:
            return unsupported_transport_result(
                "/tools defaults reset",
                method_name=exc.method_name,
            )
        defaults = _string_list(mapping_get(result, "default_tools", []))
        await context.dispatch({"type": "tools_updated"})
        suffix = f" ({len(defaults)} tools)" if defaults else ""
        return CommandResult.completed(
            CommandMessage(f"Default tools reset{suffix}.", level="success"),
            payload={"default_tools": tuple(defaults)},
        )

    return CommandResult.failed(
        "Usage: /tools defaults [list|add|remove|set|reset]",
        error_code="usage_error",
    )


async def _show_default_tools(context: CommandContext) -> CommandResult:
    try:
        data = await call_client_method(
            context,
            "get_default_tools",
            user_id=context.user_id,
        )
    except TypeError:
        data = await call_client_method(context, "get_default_tools", context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/tools defaults", method_name=exc.method_name)

    default_names = set(_string_list(mapping_get(data, "default_tools", [])))
    available = _mapping_sequence(mapping_get(data, "available_tools", []))
    if not available and default_names:
        available = [{"name": name, "is_default": True} for name in sorted(default_names)]

    lines = ["Default Tools", "  Name                           Kind          Status"]
    for tool in sorted(available, key=lambda item: _tool_name(item)):
        name = _tool_name(tool)
        status = "default" if (tool.get("is_default") or name in default_names) else "available"
        kind = str(tool.get("category") or tool.get("tool_type") or "")
        lines.append(f"  {compact_id(name, width=30):<30} {kind:<13} {status}")
    if not available:
        lines.append("  None")
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Tools"))


async def _set_default_tools(
    context: CommandContext,
    tool_names: Sequence[str],
) -> CommandResult:
    try:
        result = await call_client_method(
            context,
            "set_default_tools",
            list(tool_names),
            user_id=context.user_id,
        )
    except TypeError:
        result = await call_client_method(
            context,
            "set_default_tools",
            list(tool_names),
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(
            "/tools defaults",
            method_name=exc.method_name,
        )

    defaults = _string_list(mapping_get(result, "default_tools", tool_names))
    await context.dispatch({"type": "tools_updated"})
    return CommandResult.completed(
        CommandMessage(
            f"Default tools saved ({len(defaults)} tools).",
            level="success",
        ),
        payload={"default_tools": tuple(defaults)},
    )


async def _default_tool_names(context: CommandContext) -> list[str]:
    data = await call_client_method(context, "get_default_tools", user_id=context.user_id)
    return _string_list(mapping_get(data, "default_tools", []))


async def _handle_tools_test_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        return CommandResult.failed(
            "/tools test is only available in the new command layer.",
            error_code="legacy_command_unavailable",
        )
    if not args:
        return CommandResult.failed(
            "Usage: /tools test <custom-tool-id> [json-or-key=value...]",
            error_code="usage_error",
        )

    tool_id = args[0]
    params, error = _parse_params(args[1:])
    if error:
        return CommandResult.failed(error, error_code="usage_error")

    try:
        result = await call_client_method(context, "test_custom_tool", tool_id, params)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/tools test", method_name=exc.method_name)

    status = str(mapping_get(result, "status", "ok"))
    success = status == "ok" or bool(mapping_get(result, "success", False))
    output = mapping_get(result, "result", "")
    error_text = mapping_get(result, "error", "")
    if success:
        detail = one_line(output if output != "" else result, limit=300)
        message = f"Tool test passed: {tool_id}"
        if detail:
            message += f"\n  {detail}"
        return CommandResult.completed(
            CommandMessage(message, level="success"),
            payload={"tool_id": tool_id, "status": status},
        )
    return CommandResult.failed(
        f"Tool test failed: {tool_id}\n  {one_line(error_text or result, limit=300)}",
        error_code="tool_test_failed",
        payload={"tool_id": tool_id, "status": status},
    )


async def _thread_config_or_empty(context: CommandContext) -> Mapping[str, Any]:
    if not context.thread_id:
        return {}
    try:
        config = await call_client_method(
            context,
            "get_thread_config",
            context.thread_id,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable:
        return {}
    return config if isinstance(config, Mapping) else {}


async def _list_tool_entries(context: CommandContext) -> list[Mapping[str, Any]]:
    try:
        data = await call_client_method(
            context,
            "get_unified_tools",
            user_id=context.user_id,
        )
    except TypeError:
        data = await call_client_method(context, "get_unified_tools", context.user_id)
    except CommandClientMethodUnavailable:
        try:
            data = await call_client_method(context, "list_all_tools", context.user_id)
        except CommandClientMethodUnavailable:
            data = await call_client_method(context, "get_tools")

    if isinstance(data, Mapping):
        raw = data.get("tools") or data.get("data") or []
    else:
        raw = data
    return _mapping_sequence(raw)


def _format_tools_table(
    tools: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> str:
    enabled = set(_string_list(config.get("enabled_tools")))
    disabled = set(_string_list(config.get("disabled_tools")))
    lines = ["Tools", "  Name                           Kind          Status     Description"]
    seen: set[str] = set()

    for tool in sorted(tools, key=lambda item: _tool_name(item)):
        name = _tool_name(tool)
        if not name or name in seen:
            continue
        seen.add(name)
        kind = str(
            tool.get("tool_type")
            or tool.get("category")
            or tool.get("implementation_type")
            or ""
        )
        if name in disabled:
            status = "disabled"
        elif name in enabled:
            status = "enabled"
        elif tool.get("enabled") is False:
            status = "available"
        else:
            status = str(tool.get("enabled_reason") or "enabled")
        lines.append(
            f"  {compact_id(name, width=30):<30} "
            f"{compact_id(kind, width=12):<13} "
            f"{compact_id(status, width=10):<10} "
            f"{one_line(tool.get('description', ''), limit=70)}"
        )

    for name in sorted((enabled | disabled) - seen):
        status = "disabled" if name in disabled else "enabled"
        lines.append(f"  {compact_id(name, width=30):<30} {'thread':<13} {status:<10}")
    return "\n".join(lines)


def _tools_json_entries(
    tools: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    enabled = set(_string_list(config.get("enabled_tools")))
    disabled = set(_string_list(config.get("disabled_tools")))
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()

    for tool in sorted(tools, key=lambda item: _tool_name(item)):
        name = _tool_name(tool)
        if not name or name in seen:
            continue
        seen.add(name)
        kind = str(
            tool.get("tool_type")
            or tool.get("category")
            or tool.get("implementation_type")
            or ""
        )
        if name in disabled:
            status = "disabled"
        elif name in enabled:
            status = "enabled"
        elif tool.get("enabled") is False:
            status = "available"
        else:
            status = str(tool.get("enabled_reason") or "enabled")
        entry = dict(tool)
        entry.update({"name": name, "kind": kind, "status": status})
        entries.append(entry)

    for name in sorted((enabled | disabled) - seen):
        status = "disabled" if name in disabled else "enabled"
        entries.append({"name": name, "kind": "thread", "status": status})
    return entries


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item)]


def _tool_name(tool: Mapping[str, Any]) -> str:
    return str(tool.get("id") or tool.get("name") or tool.get("tool_id") or "")


def _parse_params(args: Sequence[str]) -> tuple[dict[str, Any], str]:
    if not args:
        return {}, ""

    raw = " ".join(args).strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, f"Invalid JSON params: {exc}"
        if not isinstance(parsed, dict):
            return {}, "JSON params must be an object."
        return parsed, ""

    params: dict[str, Any] = {}
    for arg in args:
        if "=" not in arg:
            return {}, f"Expected key=value parameter, got {arg!r}."
        key, value = arg.split("=", 1)
        key = key.strip()
        if not key:
            return {}, "Parameter keys cannot be blank."
        params[key] = _parse_scalar(value)
    return params, ""


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
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


def register(registry: CommandRegistry) -> None:
    """Register tool commands."""
    registry.register(Command(
        name="tools",
        aliases=[],
        description="List/manage tools",
        usage="/tools list",
        handler=_handle_tools_root_context,
        handler_mode="context",
        category="Tools",
        subcommands={
            "list": Command(
                name="list",
                description="List tools",
                usage="list",
                handler=_handle_tools_list_context,
                handler_mode="context",
                category="Tools",
            ),
            "enable": Command(
                name="enable",
                aliases=["on"],
                description="Enable a tool for this thread",
                usage="enable <tool-id>",
                handler=_handle_tools_enable_context,
                handler_mode="context",
                category="Tools",
            ),
            "disable": Command(
                name="disable",
                aliases=["off"],
                description="Disable a tool for this thread",
                usage="disable <tool-id>",
                handler=_handle_tools_disable_context,
                handler_mode="context",
                category="Tools",
            ),
            "optional": Command(
                name="optional",
                description="List optional tools",
                usage="optional",
                handler=_handle_tools_optional_context,
                handler_mode="context",
                category="Tools",
            ),
            "defaults": Command(
                name="defaults",
                description="Show or edit default tools",
                usage="defaults [list|add|remove|set|reset]",
                handler=_handle_tools_defaults_context,
                handler_mode="context",
                category="Tools",
            ),
            "test": Command(
                name="test",
                description="Test a custom tool",
                usage="test <custom-tool-id> [json-or-key=value...]",
                handler=_handle_tools_test_context,
                handler_mode="context",
                category="Tools",
            ),
        },
    ))
