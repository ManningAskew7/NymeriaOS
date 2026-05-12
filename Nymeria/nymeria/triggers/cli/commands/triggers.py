"""Trigger automation commands."""

from __future__ import annotations

import json
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
    parse_scalar,
    strip_confirmation_flags,
    unsupported_transport_result,
)


async def _handle_triggers_root(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /triggers list|create|edit|enable|disable|history|test|delete",
            error_code="usage_error",
        )
    return await _handle_triggers_list(context, [])


async def _handle_triggers_list(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    enabled_only = "--enabled" in args
    thread_id = _thread_filter(args, context.thread_id)
    try:
        triggers = await call_client_method(
            context,
            "list_triggers",
            context.user_id,
            enabled_only=enabled_only,
            thread_id=thread_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/triggers list", method_name=exc.method_name)

    entries = _mapping_sequence(triggers)
    if not entries:
        return CommandResult.completed(CommandMessage("No triggers found.", level="warning"))
    return CommandResult.completed(
        CommandMessage("\n".join(_format_triggers(entries)), title="Triggers"),
        payload={"count": len(entries)},
    )


async def _handle_triggers_create(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    parsed = _parse_create_args(args, context.thread_id)
    if isinstance(parsed, CommandResult):
        return parsed

    try:
        created = await call_client_method(
            context,
            "create_trigger",
            parsed,
            user_id=context.user_id,
        )
    except TypeError:
        created = await call_client_method(context, "create_trigger", parsed, context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/triggers create", method_name=exc.method_name)

    trigger_id = str(mapping_get(created, "id", ""))
    await context.dispatch({"type": "triggers_updated"})
    return CommandResult.completed(
        CommandMessage(
            f"Created trigger: {compact_id(trigger_id)} {mapping_get(created, 'name', '')}",
            level="success",
        ),
        payload={"trigger_id": trigger_id},
    )


async def _handle_triggers_edit(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) < 2:
        return CommandResult.failed(
            "Usage: /triggers edit <id> key=value [key=value...]",
            error_code="usage_error",
        )
    trigger_id = args[0]
    patch, error = _parse_trigger_patch(args[1:])
    if error:
        return CommandResult.failed(error, error_code="usage_error")

    try:
        updated = await call_client_method(
            context,
            "update_trigger",
            trigger_id,
            patch,
            user_id=context.user_id,
        )
    except TypeError:
        updated = await call_client_method(
            context,
            "update_trigger",
            trigger_id,
            patch,
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/triggers edit", method_name=exc.method_name)

    await context.dispatch({"type": "triggers_updated"})
    return CommandResult.completed(
        CommandMessage(
            f"Updated trigger: {compact_id(mapping_get(updated, 'id', trigger_id))}",
            level="success",
        ),
        payload={"trigger_id": trigger_id, "updated": tuple(sorted(patch))},
    )


async def _handle_triggers_enable(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    return await _set_trigger_enabled(context, args, enabled=True)


async def _handle_triggers_disable(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    return await _set_trigger_enabled(context, args, enabled=False)


async def _set_trigger_enabled(
    context: CommandContext,
    args: Sequence[str],
    *,
    enabled: bool,
) -> CommandResult:
    if not args:
        verb = "enable" if enabled else "disable"
        return CommandResult.failed(f"Usage: /triggers {verb} <id>", error_code="usage_error")
    trigger_id = args[0]
    try:
        updated = await call_client_method(
            context,
            "update_trigger",
            trigger_id,
            {"enabled": enabled},
            user_id=context.user_id,
        )
    except TypeError:
        updated = await call_client_method(
            context,
            "update_trigger",
            trigger_id,
            {"enabled": enabled},
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/triggers enable", method_name=exc.method_name)
    await context.dispatch({"type": "triggers_updated"})
    return CommandResult.completed(
        CommandMessage(
            f"{'Enabled' if enabled else 'Disabled'} trigger: "
            f"{compact_id(mapping_get(updated, 'id', trigger_id))}",
            level="success",
        ),
        payload={"trigger_id": trigger_id, "enabled": enabled},
    )


async def _handle_triggers_history(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    limit = _limit_arg(args, default=20)
    try:
        if args and not args[0].isdigit() and not args[0].startswith("--limit"):
            executions = await call_client_method(
                context,
                "get_trigger_executions",
                args[0],
                context.user_id,
                limit,
            )
        else:
            executions = await call_client_method(
                context,
                "get_recent_trigger_executions",
                context.user_id,
                limit,
            )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/triggers history", method_name=exc.method_name)

    entries = _mapping_sequence(executions)
    if not entries:
        return CommandResult.completed(
            CommandMessage("No trigger executions found.", level="warning")
        )
    return CommandResult.completed(
        CommandMessage("\n".join(_format_executions(entries)), title="Triggers"),
        payload={"count": len(entries)},
    )


async def _handle_triggers_test(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed("Usage: /triggers test <id>", error_code="usage_error")
    trigger_id = args[0]
    try:
        result = await call_client_method(
            context,
            "test_trigger",
            trigger_id,
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/triggers test", method_name=exc.method_name)

    lines = [
        f"Trigger Test: {trigger_id}",
        f"  Action            {mapping_get(result, 'action_type', '')}",
        f"  Conditions pass   {mapping_get(result, 'conditions_pass', '')}",
        f"  Rendered output   {one_line(mapping_get(result, 'rendered_output', ''), limit=120)}",
    ]
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Triggers"))


async def _handle_triggers_delete(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    args, explicit_confirmation = strip_confirmation_flags(args)
    if not args:
        return CommandResult.failed(
            "Usage: /triggers delete <id> [--yes]",
            error_code="usage_error",
        )
    trigger_id = args[0]
    confirmed = await confirmation_granted(
        context,
        f"Delete trigger {trigger_id}?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return confirmation_required_result("/triggers delete")
    try:
        await call_client_method(context, "delete_trigger", trigger_id, context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/triggers delete", method_name=exc.method_name)
    await context.dispatch({"type": "triggers_updated"})
    return CommandResult.completed(
        CommandMessage(f"Deleted trigger: {compact_id(trigger_id)}", level="success"),
        payload={"trigger_id": trigger_id},
    )


def _parse_create_args(
    args: Sequence[str],
    current_thread_id: str | None,
) -> dict[str, Any] | CommandResult:
    options: dict[str, Any] = {
        "source_type": "webhook",
        "action_type": "agent_prompt",
        "source_config": {},
        "action_config": {},
        "conditions": [],
        "cooldown_seconds": 0,
        "enabled": True,
        "thread_id": None,
    }
    positional: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        normalized = arg.casefold()
        if normalized in {"--source", "--source-type"}:
            value, index_or_error = _next(args, index, "--source")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            options["source_type"] = value
            index = index_or_error
        elif normalized in {"--action", "--action-type"}:
            value, index_or_error = _next(args, index, "--action")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            options["action_type"] = value
            index = index_or_error
        elif normalized in {"--prompt", "--template"}:
            value, index_or_error = _next(args, index, "--prompt")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            options["action_config"]["prompt_template"] = value
            index = index_or_error
        elif normalized == "--secret":
            value, index_or_error = _next(args, index, "--secret")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            options["source_config"]["secret"] = value
            index = index_or_error
        elif normalized == "--cooldown":
            value, index_or_error = _next(args, index, "--cooldown")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            options["cooldown_seconds"] = _positive_int(value, default=0)
            index = index_or_error
        elif normalized == "--thread":
            value, index_or_error = _next(args, index, "--thread")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            options["thread_id"] = current_thread_id if value in {"current", "."} else value
            index = index_or_error
        elif normalized == "--disabled":
            options["enabled"] = False
        elif normalized == "--enabled":
            options["enabled"] = True
        elif normalized.startswith("--source-config="):
            parsed = _json_object(arg.split("=", 1)[1], "--source-config")
            if isinstance(parsed, CommandResult):
                return parsed
            options["source_config"].update(parsed)
        elif normalized.startswith("--action-config="):
            parsed = _json_object(arg.split("=", 1)[1], "--action-config")
            if isinstance(parsed, CommandResult):
                return parsed
            options["action_config"].update(parsed)
        elif normalized.startswith("--"):
            return CommandResult.failed(f"Unknown option: {arg}", error_code="usage_error")
        else:
            positional.append(arg)
        index += 1

    name = " ".join(positional).strip()
    if not name:
        return CommandResult.failed(
            "Usage: /triggers create <name> [--source webhook] [--secret <secret>] "
            "[--prompt <template>] [--thread current|<id>]",
            error_code="usage_error",
        )
    if options["source_type"] == "webhook" and "secret" not in options["source_config"]:
        options["source_config"]["secret"] = "change-me"
    if options["action_type"] == "agent_prompt" and "prompt_template" not in options["action_config"]:
        options["action_config"]["prompt_template"] = "Trigger {trigger_name} fired."
    return {"name": name, **options}


def _parse_trigger_patch(args: Sequence[str]) -> tuple[dict[str, Any], str]:
    patch: dict[str, Any] = {}
    for arg in args:
        if "=" not in arg:
            return {}, f"Expected key=value, got {arg!r}."
        key, value = arg.split("=", 1)
        key = key.strip()
        if key == "name":
            patch["name"] = value
        elif key == "enabled":
            patch["enabled"] = bool(parse_scalar(value))
        elif key in {"cooldown", "cooldown_seconds"}:
            patch["cooldown_seconds"] = _positive_int(value, default=0)
        elif key == "source_config":
            parsed = _json_object(value, "source_config")
            if isinstance(parsed, CommandResult):
                return {}, parsed.messages[0].content if parsed.messages else "Invalid JSON."
            patch["source_config"] = parsed
        elif key == "action_config":
            parsed = _json_object(value, "action_config")
            if isinstance(parsed, CommandResult):
                return {}, parsed.messages[0].content if parsed.messages else "Invalid JSON."
            patch["action_config"] = parsed
        elif key == "action_type":
            patch["action_type"] = value
        else:
            return {}, f"Unsupported trigger field: {key}"
    return patch, ""


def _format_triggers(triggers: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["Triggers", "  ID        Enabled  Health     Source       Name"]
    for trigger in triggers:
        enabled = "yes" if trigger.get("enabled") else "no"
        lines.append(
            f"  {compact_id(trigger.get('id')):<8}  {enabled:<7}  "
            f"{compact_id(trigger.get('health_status'), width=9):<9}  "
            f"{compact_id(trigger.get('source_type'), width=11):<11}  "
            f"{one_line(trigger.get('name'), limit=60)}"
        )
    return lines


def _format_executions(executions: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["Trigger Executions", "  Time                  Status   Trigger    Summary"]
    for execution in executions:
        lines.append(
            f"  {one_line(execution.get('timestamp'), limit=20):<20}  "
            f"{compact_id(execution.get('status'), width=8):<8} "
            f"{compact_id(execution.get('trigger_id'), width=8):<8}  "
            f"{one_line(execution.get('events_summary') or execution.get('error_message'), limit=80)}"
        )
    return lines


def _thread_filter(args: Sequence[str], current_thread_id: str | None) -> str | None:
    for index, arg in enumerate(args):
        if arg == "--thread" and index + 1 < len(args):
            value = args[index + 1]
            return current_thread_id if value.casefold() in {"current", "."} else value
        if arg in {"--current-thread", "--thread-current"}:
            return current_thread_id
    return None


def _limit_arg(args: Sequence[str], *, default: int) -> int:
    for arg in args:
        if arg.isdigit():
            return _positive_int(arg, default=default)
        if arg.startswith("--limit="):
            return _positive_int(arg.split("=", 1)[1], default=default)
    return default


def _json_object(value: str, label: str) -> dict[str, Any] | CommandResult:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        return CommandResult.failed(f"Invalid {label} JSON: {exc}", error_code="usage_error")
    if not isinstance(parsed, dict):
        return CommandResult.failed(f"{label} must be a JSON object.", error_code="usage_error")
    return parsed


def _next(
    args: Sequence[str],
    index: int,
    option: str,
) -> tuple[str, int | CommandResult]:
    if index + 1 >= len(args):
        return "", CommandResult.failed(f"{option} requires a value.", error_code="usage_error")
    return args[index + 1], index + 1


def _positive_int(value: str, *, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def register(registry: CommandRegistry) -> None:
    """Register trigger commands."""
    registry.register(Command(
        name="triggers",
        aliases=["trigger"],
        description="Manage event triggers",
        usage="/triggers list",
        handler=_handle_triggers_root,
        handler_mode="context",
        category="Automation",
        subcommands={
            "list": Command(
                name="list",
                description="List triggers",
                usage="list [--enabled] [--thread current|<id>]",
                handler=_handle_triggers_list,
                handler_mode="context",
                category="Automation",
            ),
            "create": Command(
                name="create",
                aliases=["add"],
                description="Create a trigger",
                usage="create <name> [--source webhook] [--prompt <template>]",
                handler=_handle_triggers_create,
                handler_mode="context",
                category="Automation",
            ),
            "edit": Command(
                name="edit",
                description="Edit trigger fields",
                usage="edit <id> key=value [key=value...]",
                handler=_handle_triggers_edit,
                handler_mode="context",
                category="Automation",
            ),
            "enable": Command(
                name="enable",
                description="Enable a trigger",
                usage="enable <id>",
                handler=_handle_triggers_enable,
                handler_mode="context",
                category="Automation",
            ),
            "disable": Command(
                name="disable",
                description="Disable a trigger",
                usage="disable <id>",
                handler=_handle_triggers_disable,
                handler_mode="context",
                category="Automation",
            ),
            "history": Command(
                name="history",
                description="Show trigger execution history",
                usage="history [trigger-id] [limit]",
                handler=_handle_triggers_history,
                handler_mode="context",
                category="Automation",
            ),
            "test": Command(
                name="test",
                description="Dry-run a trigger",
                usage="test <id>",
                handler=_handle_triggers_test,
                handler_mode="context",
                category="Automation",
            ),
            "delete": Command(
                name="delete",
                aliases=["remove", "rm"],
                description="Delete a trigger",
                usage="delete <id> [--yes]",
                handler=_handle_triggers_delete,
                handler_mode="context",
                category="Automation",
            ),
        },
    ))
