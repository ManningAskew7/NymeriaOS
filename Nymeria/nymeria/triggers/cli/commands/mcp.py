"""MCP server commands: /mcp list, add, remove, discover, test, retry, status."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    CommandClientMethodUnavailable,
    call_client_method,
    compact_id,
    confirmation_granted,
    confirmation_required_result,
    mapping_get,
    one_line,
    strip_confirmation_flags,
    unsupported_transport_result,
)


async def _handle_mcp_root(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        return CommandResult.failed(
            "/mcp is only available in the new command layer.",
            error_code="legacy_command_unavailable",
        )
    if args:
        return CommandResult.failed(
            "Usage: /mcp list|add|remove|discover|test|retry|status|logs",
            error_code="usage_error",
        )
    return await _handle_mcp_list(context, [])


async def _handle_mcp_list(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        servers = await _list_servers(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/mcp list", method_name=exc.method_name)

    if not servers:
        return CommandResult.completed(
            CommandMessage("No MCP servers configured.", level="warning")
        )
    return CommandResult.completed(
        CommandMessage(_format_mcp_servers(servers), title="MCP Servers")
    )


async def _handle_mcp_add(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    confirmed_args, explicit_confirmation = strip_confirmation_flags(args)
    name, confirmed_args = _consume_option(confirmed_args, "--name")
    thread_id, confirmed_args = _consume_option(confirmed_args, "--thread")
    no_auto_enable, confirmed_args = _consume_flag(confirmed_args, "--no-auto-enable")

    source = " ".join(confirmed_args).strip()
    if not source:
        return CommandResult.failed(
            "Usage: /mcp add <source> [--name name] [--thread id] "
            "[--no-auto-enable] [--yes]",
            error_code="usage_error",
        )

    if thread_id == "current":
        thread_id = context.thread_id

    request = {
        "source": source,
        "name": name,
        "confirmed": explicit_confirmation,
        "auto_enable": not no_auto_enable,
        "thread_id": thread_id,
        "config_values": {},
    }
    try:
        result = await call_client_method(context, "install_mcp_server", request)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/mcp add", method_name=exc.method_name)

    await context.dispatch({"type": "mcp_updated"})
    return _format_mcp_install_result(result, command="/mcp add")


async def _handle_mcp_remove(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    args, explicit_confirmation = strip_confirmation_flags(args)
    if not args:
        return CommandResult.failed(
            "Usage: /mcp remove <server-id> [--yes]",
            error_code="usage_error",
        )
    server_id = args[0]
    confirmed = await confirmation_granted(
        context,
        f"Remove MCP server {server_id}?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return confirmation_required_result("/mcp remove")

    try:
        result = await call_client_method(context, "delete_mcp_server", server_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/mcp remove", method_name=exc.method_name)

    deleted = mapping_get(result, "deleted", server_id)
    await context.dispatch({"type": "mcp_updated"})
    return CommandResult.completed(
        CommandMessage(f"Removed MCP server: {deleted}", level="success"),
        payload={"server_id": str(deleted)},
    )


async def _handle_mcp_discover(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /mcp discover <server-id>",
            error_code="usage_error",
        )
    server_id = args[0]
    try:
        result = await call_client_method(
            context,
            "discover_mcp_server_tools",
            server_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/mcp discover", method_name=exc.method_name)

    count = mapping_get(result, "count", None)
    if count is None:
        discovered = mapping_get(result, "discovered_tools", [])
        count = len(discovered) if isinstance(discovered, Sequence) else 0
    await context.dispatch({"type": "mcp_updated"})
    return CommandResult.completed(
        CommandMessage(
            f"Discovered {count} MCP tool{'s' if count != 1 else ''} for {server_id}.",
            level="success",
        ),
        payload={"server_id": server_id, "count": count},
    )


async def _handle_mcp_test(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed("Usage: /mcp test <server-id>", error_code="usage_error")
    server_id = args[0]
    try:
        result = await call_client_method(context, "test_mcp_server", server_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/mcp test", method_name=exc.method_name)

    status = str(mapping_get(result, "status", "ok"))
    tools_count = mapping_get(result, "tools_count", mapping_get(result, "toolsCount", ""))
    error = mapping_get(result, "error", "")
    if status.casefold() in {"ok", "success", "connected"} and not error:
        suffix = f" ({tools_count} tools)" if tools_count != "" else ""
        return CommandResult.completed(
            CommandMessage(f"MCP test passed: {server_id}{suffix}", level="success"),
            payload={"server_id": server_id, "status": status},
        )
    return CommandResult.failed(
        f"MCP test failed: {server_id}\n  {one_line(error or result, limit=300)}",
        error_code="mcp_test_failed",
        payload={"server_id": server_id, "status": status},
    )


async def _handle_mcp_retry(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    args, explicit_confirmation = strip_confirmation_flags(args)
    if not args:
        return CommandResult.failed(
            "Usage: /mcp retry <server-id> [--yes]",
            error_code="usage_error",
        )
    server_id = args[0]
    confirmed = await confirmation_granted(
        context,
        f"Retry MCP setup for {server_id}?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return confirmation_required_result("/mcp retry")

    try:
        result = await call_client_method(
            context,
            "retry_mcp_server_install",
            server_id,
            {"confirmed": True, "config_values": {}},
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/mcp retry", method_name=exc.method_name)

    await context.dispatch({"type": "mcp_updated"})
    return _format_mcp_install_result(result, command="/mcp retry")


async def _handle_mcp_status(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        try:
            server = await call_client_method(context, "get_mcp_server", args[0])
        except CommandClientMethodUnavailable as exc:
            return unsupported_transport_result("/mcp status", method_name=exc.method_name)
        if not isinstance(server, Mapping):
            return CommandResult.failed("MCP server response was not a mapping.")
        return CommandResult.completed(
            CommandMessage(_format_mcp_server_status(server), title="MCP Server")
        )

    try:
        servers = await _list_servers(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/mcp status", method_name=exc.method_name)
    if not servers:
        return CommandResult.completed(
            CommandMessage("No MCP servers configured.", level="warning")
        )
    return CommandResult.completed(
        CommandMessage(_format_mcp_servers(servers, include_errors=True), title="MCP Servers")
    )


async def _handle_mcp_logs(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /mcp logs <server-id> [limit]",
            error_code="usage_error",
        )
    server_id = args[0]
    limit = _parse_limit(args[1] if len(args) > 1 else None, default=20)
    try:
        server = await call_client_method(context, "get_mcp_server", server_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/mcp logs", method_name=exc.method_name)
    if not isinstance(server, Mapping):
        return CommandResult.failed("MCP server response was not a mapping.")

    logs = _string_list(server.get("install_logs"))
    if not logs:
        return CommandResult.completed(
            CommandMessage(f"No install logs for {server_id}.", level="warning")
        )
    selected = logs[-limit:] if limit > 0 else logs
    lines = [f"MCP Logs: {server_id}"]
    lines.extend(f"  {line}" for line in selected)
    return CommandResult.completed(CommandMessage("\n".join(lines), title="MCP Logs"))


async def _list_servers(context: CommandContext) -> list[Mapping[str, Any]]:
    data = await call_client_method(context, "list_mcp_servers")
    if isinstance(data, Mapping):
        raw = data.get("servers") or data.get("data") or []
    else:
        raw = data
    return _mapping_sequence(raw)


def _format_mcp_servers(
    servers: Sequence[Mapping[str, Any]],
    *,
    include_errors: bool = False,
) -> str:
    lines = [
        "MCP Servers",
        "  ID                             State     Tools  Name",
    ]
    for server in sorted(servers, key=lambda item: _server_id(item)):
        server_id = _server_id(server)
        state = _server_state(server)
        tools = _tool_count(server)
        name = str(server.get("name") or server_id)
        line = (
            f"  {compact_id(server_id, width=30):<30} "
            f"{compact_id(state, width=8):<9} "
            f"{str(tools):>5}  {one_line(name, limit=48)}"
        )
        if include_errors and server.get("last_error"):
            line += f"  error: {one_line(server.get('last_error'), limit=50)}"
        lines.append(line)
    return "\n".join(lines)


def _format_mcp_server_status(server: Mapping[str, Any]) -> str:
    rows = [
        ("ID", _server_id(server)),
        ("Name", server.get("name", "")),
        ("Enabled", _yes_no(server.get("enabled"))),
        ("Install status", server.get("install_status", "")),
        ("Transport", server.get("transport", "")),
        ("Runtime", server.get("runtime_type", "")),
        ("Tools", _tool_count(server)),
        ("Missing config", len(_mapping_sequence(server.get("missing_config")))),
        ("Last error", one_line(server.get("last_error", ""), limit=120)),
    ]
    width = max((len(label) for label, _value in rows), default=0)
    lines = ["MCP Server"]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {value}")
    logs = _string_list(server.get("install_logs"))
    if logs:
        lines.append("\nRecent logs:")
        lines.extend(f"  {line}" for line in logs[-5:])
    return "\n".join(lines)


def _format_mcp_install_result(result: Any, *, command: str) -> CommandResult:
    server = mapping_get(result, "server", {})
    server_id = _server_id(server if isinstance(server, Mapping) else {})
    status = str(mapping_get(result, "status", "ok"))
    discovered = mapping_get(result, "discovered_tools", 0)
    discovery_error = mapping_get(result, "discovery_error", "")
    requires_confirmation = bool(mapping_get(result, "requires_confirmation", False))

    if requires_confirmation:
        return CommandResult.completed(
            CommandMessage(
                f"{command} needs admin confirmation. Re-run with --yes after reviewing the preview.",
                level="warning",
            ),
            payload={"server_id": server_id, "status": status},
        )
    if status not in {"ok", "ready", "success"}:
        message = f"MCP server saved as {status}: {server_id or 'unknown'}"
        if discovery_error:
            message += f"\n  {one_line(discovery_error, limit=240)}"
        return CommandResult.completed(
            CommandMessage(message, level="warning"),
            payload={"server_id": server_id, "status": status},
        )

    message = f"MCP server ready: {server_id or 'unknown'}"
    if discovered != "":
        message += f" ({discovered} tools)"
    return CommandResult.completed(
        CommandMessage(message, level="success"),
        payload={"server_id": server_id, "status": status, "discovered_tools": discovered},
    )


def _server_id(server: Mapping[str, Any]) -> str:
    return str(server.get("id") or server.get("server_id") or "")


def _server_state(server: Mapping[str, Any]) -> str:
    status = str(server.get("install_status") or "")
    if status and status != "ready":
        return status
    return "enabled" if bool(server.get("enabled", True)) else "disabled"


def _tool_count(server: Mapping[str, Any]) -> int:
    tools = server.get("discovered_tools") or []
    if isinstance(tools, Sequence) and not isinstance(tools, (str, bytes)):
        return len(tools)
    try:
        return int(tools)
    except (TypeError, ValueError):
        return 0


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item)]


def _consume_option(
    args: Sequence[str],
    option: str,
) -> tuple[str | None, list[str]]:
    remaining: list[str] = []
    selected: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == option and index + 1 < len(args):
            selected = args[index + 1]
            index += 2
            continue
        if arg.startswith(f"{option}="):
            selected = arg.split("=", 1)[1]
            index += 1
            continue
        remaining.append(arg)
        index += 1
    return selected, remaining


def _consume_flag(args: Sequence[str], flag: str) -> tuple[bool, list[str]]:
    enabled = False
    remaining: list[str] = []
    for arg in args:
        if arg == flag:
            enabled = True
        else:
            remaining.append(arg)
    return enabled, remaining


def _parse_limit(value: str | None, *, default: int) -> int:
    if value is None:
        return default
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def register(registry: CommandRegistry) -> None:
    """Register MCP server commands."""
    registry.register(Command(
        name="mcp",
        aliases=[],
        description="Manage MCP servers",
        usage="/mcp list",
        handler=_handle_mcp_root,
        handler_mode="context",
        category="MCP",
        subcommands={
            "list": Command(
                name="list",
                description="List MCP servers",
                usage="list",
                handler=_handle_mcp_list,
                handler_mode="context",
                category="MCP",
            ),
            "add": Command(
                name="add",
                aliases=["install"],
                description="Install an MCP server",
                usage="add <source> [--name name] [--thread id] [--yes]",
                handler=_handle_mcp_add,
                handler_mode="context",
                category="MCP",
            ),
            "remove": Command(
                name="remove",
                aliases=["rm", "delete"],
                description="Remove an MCP server",
                usage="remove <server-id> [--yes]",
                handler=_handle_mcp_remove,
                handler_mode="context",
                category="MCP",
            ),
            "discover": Command(
                name="discover",
                description="Rediscover MCP tools",
                usage="discover <server-id>",
                handler=_handle_mcp_discover,
                handler_mode="context",
                category="MCP",
            ),
            "test": Command(
                name="test",
                description="Test MCP connectivity",
                usage="test <server-id>",
                handler=_handle_mcp_test,
                handler_mode="context",
                category="MCP",
            ),
            "retry": Command(
                name="retry",
                description="Retry MCP setup",
                usage="retry <server-id> [--yes]",
                handler=_handle_mcp_retry,
                handler_mode="context",
                category="MCP",
            ),
            "status": Command(
                name="status",
                description="Show MCP status",
                usage="status [server-id]",
                handler=_handle_mcp_status,
                handler_mode="context",
                category="MCP",
            ),
            "logs": Command(
                name="logs",
                description="Show MCP install logs",
                usage="logs <server-id> [limit]",
                handler=_handle_mcp_logs,
                handler_mode="context",
                category="MCP",
            ),
        },
    ))
