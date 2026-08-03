"""Tool commands: the client-side halves of the /tools family.

``defaults``, ``search`` and ``test`` are genuinely local (the backend
registers no such verbs); the root and ``list`` handlers are defensive
fallbacks for a failed catalog registration. Backlog #131 folded
``/tools core|optional|enabled|category`` into ``/tools list [filter]`` and
retired the local ``core`` and ``optional`` declarations with it: the fold
freed both keys, which would otherwise have made the CLI the one surface
where those spellings still rendered their own view.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ._shared import (
    CommandClientMethodUnavailable,
    call_client_method,
    call_client_user_scoped,
    compact_id,
    mapping_get,
    mapping_sequence as _mapping_sequence,
    one_line,
    parse_scalar,
    string_list as _string_list,
    unsupported_transport_result,
)


async def _handle_tools_root_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """List or search tools; a defensive fallback for the backend root.

    The backend ``/tools`` root shadows this one whenever the catalog
    registers (it takes no arguments and renders the enabled readout), so
    the ``/tools <query>`` search shorthand here only runs if catalog
    registration itself fails. ``/tools search <query>`` is the spelling
    that works in both cases.
    """
    if args:
        return await _handle_tools_search_context(context, args)
    return await _handle_tools_list_context(context, [])


async def _handle_tools_list_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
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


async def _handle_tools_search_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /tools search <query>",
            error_code="usage_error",
        )
    query = " ".join(args).strip()

    try:
        try:
            data = await call_client_method(
                context,
                "search_tools",
                query,
                user_id=context.user_id,
                thread_id=context.thread_id,
                top_k=10,
            )
        except TypeError:
            data = await call_client_method(context, "search_tools", query)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/tools search", method_name=exc.method_name)
    except Exception as exc:
        if not _is_not_found_error(exc):
            raise
        data = await _legacy_tool_search_response(context, query, top_k=10)

    return CommandResult.completed(
        CommandMessage(_format_tool_search_response(data), title="Tools"),
        json_payload=data,
    )


async def _handle_tools_enable_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
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

    if enabled:
        suggestion_result = await _unknown_tool_suggestion_result(context, tool_name)
        if suggestion_result is not None:
            return suggestion_result

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


async def _unknown_tool_suggestion_result(
    context: CommandContext,
    tool_name: str,
) -> CommandResult | None:
    try:
        data = await call_client_method(
            context,
            "search_tools",
            tool_name,
            user_id=context.user_id,
            thread_id=context.thread_id,
            top_k=5,
        )
    except (CommandClientMethodUnavailable, TypeError):
        return None
    except Exception as exc:
        if not _is_not_found_error(exc):
            raise
        data = await _legacy_tool_search_response(context, tool_name, top_k=5)

    results = _mapping_sequence(mapping_get(data, "results", []))
    if any(_tool_name(result) == tool_name for result in results):
        return None

    if not results:
        return CommandResult.failed(
            f"Unknown tool '{tool_name}'. Use /tools search <query> to find tools.",
            error_code="unknown_tool",
        )

    lines = [f"Unknown tool '{tool_name}'. Did you mean:"]
    for result in results[:5]:
        name = _tool_name(result)
        hint = str(result.get("enable_hint") or f"/tools enable {name}")
        desc = one_line(result.get("description", ""), limit=70)
        lines.append(f"  {name}: {desc}")
        lines.append(f"    {hint}")
    return CommandResult.failed(
        "\n".join(lines),
        error_code="unknown_tool",
        json_payload=data,
    )


async def _handle_tools_defaults_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args or args[0].casefold() in {"list", "show"}:
        return await _show_default_tools(context, command="/tools defaults")

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
            result = await call_client_user_scoped(context, "reset_default_tools")
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


async def _show_default_tools(
    context: CommandContext,
    *,
    command: str,
) -> CommandResult:
    try:
        data = await call_client_user_scoped(context, "get_default_tools")
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(command, method_name=exc.method_name)

    default_names = _string_list(mapping_get(data, "default_tools", []))
    default_set = set(default_names)
    available = _mapping_sequence(mapping_get(data, "available_tools", []))
    by_name = {_tool_name(tool): tool for tool in available if _tool_name(tool)}

    lines = ["Default Core Toolset", "  Name                           Kind          Description"]
    for name in sorted(default_names):
        tool = by_name.get(name, {})
        kind = str(tool.get("category") or tool.get("tool_type") or "")
        description = one_line(tool.get("description", ""), limit=80)
        lines.append(
            f"  {compact_id(name, width=30):<30} "
            f"{compact_id(kind, width=12):<13} "
            f"{description}"
        )
    for tool in sorted(available, key=lambda item: _tool_name(item)):
        name = _tool_name(tool)
        if not name or name in default_set or not bool(tool.get("is_default")):
            continue
        kind = str(tool.get("category") or tool.get("tool_type") or "")
        description = one_line(tool.get("description", ""), limit=80)
        lines.append(
            f"  {compact_id(name, width=30):<30} "
            f"{compact_id(kind, width=12):<13} "
            f"{description}"
        )
    if len(lines) == 2:
        lines.append("  None")
    return CommandResult.completed(
        CommandMessage("\n".join(lines), title="Tools"),
        payload={"default_tools": tuple(sorted(default_set))},
    )


async def _set_default_tools(
    context: CommandContext,
    tool_names: Sequence[str],
) -> CommandResult:
    try:
        result = await call_client_user_scoped(context, "set_default_tools", list(tool_names))
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


async def _legacy_tool_search_response(
    context: CommandContext,
    query: str,
    *,
    top_k: int,
) -> dict[str, Any]:
    """Fallback for CLIs connected to an API that predates tool search."""

    tools = await _list_tool_entries(context)
    config = await _thread_config_or_empty(context)
    entries = _tools_json_entries(tools, config)
    ranked = [
        (_legacy_tool_search_score(query, entry), entry)
        for entry in entries
    ]
    ranked = [
        (score, entry)
        for score, entry in ranked
        if score > 0 or not query.strip()
    ]
    ranked.sort(key=lambda item: (-item[0], _tool_name(item[1]).casefold()))

    return {
        "query": query,
        "mode": "fuzzy" if query.strip() else "substring",
        "warning": (
            "Connected backend does not expose /users/{user_id}/tools/search yet; "
            "using local CLI fallback. Pull and restart the API for backend ranking."
        ),
        "results": [
            _legacy_tool_search_result(entry, score)
            for score, entry in ranked[:max(1, top_k)]
        ],
    }


def _legacy_tool_search_result(
    entry: Mapping[str, Any],
    score: float,
) -> dict[str, Any]:
    name = _tool_name(entry)
    status = str(entry.get("status") or "available")
    enabled = status not in {"available", "disabled", ""}
    return {
        "name": name,
        "description": str(entry.get("description") or ""),
        "category": str(entry.get("category") or entry.get("kind") or "unknown"),
        "security_level": str(entry.get("security_level") or "moderate"),
        "tool_type": str(entry.get("tool_type") or entry.get("kind") or "builtin"),
        "is_default": status in {"default", "default_enabled", "default_thread_tools"},
        "status": status,
        "score": round(score, 4),
        "enable_hint": (
            f"Already enabled. Disable with /tools disable {name}"
            if enabled
            else f"/tools enable {name}"
        ),
    }


def _legacy_tool_search_score(query: str, entry: Mapping[str, Any]) -> float:
    q = _compact_search_text(query)
    if not q:
        return 1.0

    fields = [
        _tool_name(entry),
        str(entry.get("id") or ""),
        str(entry.get("category") or ""),
        str(entry.get("kind") or ""),
        str(entry.get("tool_type") or ""),
        str(entry.get("implementation_type") or ""),
        " ".join(str(tag) for tag in entry.get("tags") or []),
        str(entry.get("description") or ""),
    ]
    best = 0.0
    for field in fields:
        candidate = _compact_search_text(field)
        if not candidate:
            continue
        if q == candidate:
            best = max(best, 1.0)
        elif candidate.startswith(q):
            best = max(best, 0.92)
        elif q in candidate:
            best = max(best, 0.82)
        elif _is_search_subsequence(q, candidate):
            best = max(best, 0.55)
        else:
            best = max(best, SequenceMatcher(None, q, candidate).ratio())
    return best if best >= 0.34 else 0.0


def _compact_search_text(value: str) -> str:
    return "".join(ch for ch in value.casefold() if ch.isalnum())


def _is_search_subsequence(needle: str, haystack: str) -> bool:
    if not needle:
        return True
    index = 0
    for char in haystack:
        if char == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False


def _is_not_found_error(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


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


def _format_tool_search_response(data: Mapping[str, Any]) -> str:
    results = _mapping_sequence(mapping_get(data, "results", []))
    mode = str(mapping_get(data, "mode", "substring"))
    query = str(mapping_get(data, "query", ""))
    lines = [f"Tool Search ({mode})"]
    if query:
        lines[0] += f": {query}"
    warning = str(mapping_get(data, "warning", "") or "")
    if warning:
        lines.append(f"Warning: {warning}")
    if not results:
        lines.append("No matching tools found.")
        return "\n".join(lines)

    lines.append("  Name                           Kind          Status     Hint")
    for result in results:
        name = _tool_name(result)
        kind = str(result.get("category") or result.get("tool_type") or "")
        status = str(result.get("status") or "")
        hint = str(result.get("enable_hint") or "")
        lines.append(
            f"  {compact_id(name, width=30):<30} "
            f"{compact_id(kind, width=12):<13} "
            f"{compact_id(status, width=10):<10} "
            f"{one_line(hint, limit=60)}"
        )
        description = one_line(result.get("description", ""), limit=92)
        if description:
            lines.append(f"    {description}")
    return "\n".join(lines)


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
        params[key] = parse_scalar(value)
    return params, ""


def register(registry: CommandRegistry) -> None:
    """Register tool commands."""
    registry.register(Command(
        name="tools",
        aliases=[],
        description="List/manage tools",
        usage="/tools list",
        handler=_handle_tools_root_context,
        category="Tools",
        subcommands={
            "list": Command(
                name="list",
                description="List tools",
                usage="list",
                handler=_handle_tools_list_context,
                category="Tools",
            ),
            "enable": Command(
                name="enable",
                aliases=["on"],
                description="Enable a tool for this thread",
                usage="enable <tool-id>",
                handler=_handle_tools_enable_context,
                category="Tools",
            ),
            "disable": Command(
                name="disable",
                aliases=["off"],
                description="Disable a tool for this thread",
                usage="disable <tool-id>",
                handler=_handle_tools_disable_context,
                category="Tools",
            ),
            "defaults": Command(
                name="defaults",
                description="Show or edit the default core toolset",
                usage="defaults [list|add|remove|set|reset]",
                handler=_handle_tools_defaults_context,
                category="Tools",
            ),
            "search": Command(
                name="search",
                description="Search tools",
                usage="search <query>",
                handler=_handle_tools_search_context,
                category="Tools",
            ),
            "test": Command(
                name="test",
                description="Test a custom tool",
                usage="test <custom-tool-id> [json-or-key=value...]",
                handler=_handle_tools_test_context,
                category="Tools",
            ),
        },
    ))
