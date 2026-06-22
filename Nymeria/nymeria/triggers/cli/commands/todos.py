"""TODO commands: /todos, /todo add, /todo done, /todo delete."""

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
from ....core.todo_constants import RECURRENCE_FORMAT_HINT, validate_recurrence


TODO_STATUS_VALUES = {"pending", "in_progress", "done"}


async def _handle_todos_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    return await _list_todos_context(context, args)


async def _handle_todo_root_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /todo list|add|edit|done|delete|schedule|repeat",
            error_code="usage_error",
        )
    return await _list_todos_context(context, [])


async def _list_todos_context(
    context: CommandContext,
    args: Sequence[str],
) -> CommandResult:
    parsed = _parse_todo_list_args(args, context.thread_id)
    if isinstance(parsed, CommandResult):
        return parsed

    try:
        todos = await _list_todos(
            context,
            filter_status=parsed["filter_status"],
            thread_id=parsed["thread_id"],
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/todos", method_name=exc.method_name)

    if not todos:
        return CommandResult.completed(CommandMessage("No TODOs found.", level="warning"))
    lines = _format_todos(todos, title="TODOs")
    return CommandResult.completed(CommandMessage("\n".join(lines), title="TODOs"))


async def _handle_todo_add_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    parsed = _parse_todo_mutation_args(
        args,
        allow_status=False,
        allow_clear=False,
        default_thread_id=context.thread_id,
        use_default_thread=True,
    )
    if isinstance(parsed, CommandResult):
        return parsed
    task = " ".join(parsed["positionals"]).strip()
    if not task:
        return CommandResult.failed(
            "Usage: /todo add <task> [--schedule <when>] [--notes <text>] "
            "[--recurrence <interval>] [--thread current|<id>]. "
            f"{RECURRENCE_FORMAT_HINT}",
            error_code="usage_error",
        )

    try:
        created = await call_client_method(
            context,
            "add_todo",
            context.user_id,
            task,
            scheduled_for=parsed["scheduled_for"],
            notes=parsed["notes"],
            recurrence=parsed["recurrence"],
            thread_id=parsed["thread_id"],
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/todo add", method_name=exc.method_name)

    todo_id = _todo_id(created)
    return CommandResult.completed(
        CommandMessage(f"Added TODO: {compact_id(todo_id)} {task}", level="success"),
        payload={"todo_id": todo_id, "task": task},
    )


async def _handle_todo_edit_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /todo edit <id> [new task] [--status <status>] "
            "[--notes <text>] [--schedule <when>|--clear-schedule] "
            "[--recurrence <value>|--clear-recurrence]",
            error_code="usage_error",
        )

    match = await _resolve_todo(context, args[0], command="/todo edit")
    if isinstance(match, CommandResult):
        return match

    parsed = _parse_todo_mutation_args(
        args[1:],
        allow_status=True,
        allow_clear=True,
        default_thread_id=context.thread_id,
        use_default_thread=False,
    )
    if isinstance(parsed, CommandResult):
        return parsed

    patch: dict[str, Any] = {}
    task = " ".join(parsed["positionals"]).strip()
    if task:
        patch["task"] = task
    if parsed["status"] is not None:
        patch["status"] = parsed["status"]
    if parsed["notes"] is not None:
        patch["notes"] = parsed["notes"]
    if parsed["scheduled_for"] is not _UNSET:
        patch["scheduled_for"] = parsed["scheduled_for"]
    if parsed["clear_schedule"]:
        patch["clear_schedule"] = True
    if parsed["thread_id"] is not None:
        patch["thread_id"] = parsed["thread_id"]
    if parsed["recurrence"] is not None:
        patch["recurrence"] = parsed["recurrence"]
    if parsed["clear_recurrence"]:
        patch["clear_recurrence"] = True

    if not patch:
        return CommandResult.failed("No TODO updates were provided.")

    todo_id = _todo_id(match)
    try:
        updated = await call_client_method(
            context,
            "update_todo",
            context.user_id,
            todo_id,
            **patch,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/todo edit", method_name=exc.method_name)

    return CommandResult.completed(
        CommandMessage(
            f"Updated TODO: {compact_id(_todo_id(updated))} {_todo_task(updated)}",
            level="success",
        ),
        payload={"todo_id": todo_id, "updated": tuple(sorted(patch))},
    )


async def _handle_todo_done_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed("Usage: /todo done <id>", error_code="usage_error")

    match = await _resolve_todo(context, args[0], command="/todo done")
    if isinstance(match, CommandResult):
        return match
    todo_id = _todo_id(match)
    try:
        updated = await call_client_method(context, "complete_todo", context.user_id, todo_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/todo done", method_name=exc.method_name)

    return CommandResult.completed(
        CommandMessage(
            f"Completed: {compact_id(todo_id)} {_todo_task(updated or match)}",
            level="success",
        ),
        payload={"todo_id": todo_id},
    )


async def _handle_todo_delete_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    args, explicit_confirmation = strip_confirmation_flags(args)
    if not args:
        return CommandResult.failed(
            "Usage: /todo delete <id> [--yes]",
            error_code="usage_error",
        )

    match = await _resolve_todo(context, args[0], command="/todo delete")
    if isinstance(match, CommandResult):
        return match
    todo_id = _todo_id(match)

    confirmed = await confirmation_granted(
        context,
        f"Delete TODO {todo_id}?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return confirmation_required_result("/todo delete")

    try:
        await call_client_method(context, "delete_todo", context.user_id, todo_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/todo delete", method_name=exc.method_name)

    return CommandResult.completed(
        CommandMessage(
            f"Deleted: {compact_id(todo_id)} {_todo_task(match)}",
            level="success",
        ),
        payload={"todo_id": todo_id},
    )


async def _handle_todo_schedule_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) < 2:
        return CommandResult.failed(
            "Usage: /todo schedule <id> <when|clear>",
            error_code="usage_error",
        )

    match = await _resolve_todo(context, args[0], command="/todo schedule")
    if isinstance(match, CommandResult):
        return match
    todo_id = _todo_id(match)
    value = " ".join(args[1:]).strip()
    patch = {"clear_schedule": True} if value.casefold() in {"clear", "none", "off"} else {"scheduled_for": value}

    try:
        updated = await call_client_method(
            context,
            "update_todo",
            context.user_id,
            todo_id,
            **patch,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/todo schedule", method_name=exc.method_name)

    detail = "cleared" if patch.get("clear_schedule") else str(mapping_get(updated, "scheduled_for", value))
    return CommandResult.completed(
        CommandMessage(f"Schedule updated: {compact_id(todo_id)} {detail}", level="success"),
        payload={"todo_id": todo_id, "schedule": detail},
    )


async def _handle_todo_recurrence_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) < 2:
        return CommandResult.failed(
            f"Usage: /todo recurrence <id> <interval|clear>. {RECURRENCE_FORMAT_HINT}",
            error_code="usage_error",
        )

    raw_value = args[1]
    value = raw_value.casefold()
    if value in {"clear", "none", "off"}:
        patch: dict[str, Any] = {"clear_recurrence": True}
    else:
        try:
            canonical = validate_recurrence(raw_value)
        except ValueError as exc:
            return CommandResult.failed(str(exc), error_code="usage_error")
        patch = {"recurrence": canonical}
        value = canonical

    match = await _resolve_todo(context, args[0], command="/todo recurrence")
    if isinstance(match, CommandResult):
        return match
    todo_id = _todo_id(match)

    try:
        updated = await call_client_method(
            context,
            "update_todo",
            context.user_id,
            todo_id,
            **patch,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/todo recurrence", method_name=exc.method_name)

    detail = "cleared" if patch.get("clear_recurrence") else str(mapping_get(updated, "recurrence", value))
    return CommandResult.completed(
        CommandMessage(f"Recurrence updated: {compact_id(todo_id)} {detail}", level="success"),
        payload={"todo_id": todo_id, "recurrence": detail},
    )


async def _list_todos(
    context: CommandContext,
    *,
    filter_status: str | None = None,
    thread_id: str | None = None,
) -> list[Mapping[str, Any]]:
    try:
        raw = await call_client_method(
            context,
            "list_todos",
            context.user_id,
            filter_status=filter_status,
            thread_id=thread_id,
        )
    except TypeError:
        raw = await call_client_method(context, "list_todos", context.user_id)
    todos = _mapping_sequence(mapping_get(raw, "items", raw))
    if filter_status and filter_status != "all":
        todos = [todo for todo in todos if _todo_status(todo) == filter_status]
    if thread_id:
        todos = [todo for todo in todos if str(todo.get("thread_id") or "") == thread_id]
    return todos


async def _resolve_todo(
    context: CommandContext,
    partial: str,
    *,
    command: str,
) -> Mapping[str, Any] | CommandResult:
    try:
        todos = await _list_todos(context, filter_status="all")
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(command, method_name=exc.method_name)

    matches = [todo for todo in todos if _todo_id(todo).startswith(partial)]
    if not matches:
        return CommandResult.failed(f"No TODO matching '{partial}'.")
    if len(matches) > 1:
        preview = ", ".join(
            f"{compact_id(_todo_id(todo))} {_todo_task(todo)}" for todo in matches[:5]
        )
        return CommandResult.completed(
            CommandMessage(
                f"Ambiguous: {len(matches)} TODOs match '{partial}': {preview}",
                level="warning",
            )
        )
    return matches[0]


def _parse_todo_list_args(
    args: Sequence[str],
    current_thread_id: str | None,
) -> dict[str, Any] | CommandResult:
    filter_status: str | None = None
    thread_id: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        normalized = arg.casefold()
        if normalized in {"all", "--all"}:
            filter_status = "all"
        elif normalized in TODO_STATUS_VALUES:
            filter_status = normalized
        elif normalized == "--status" and index + 1 < len(args):
            value = args[index + 1].casefold()
            if value not in TODO_STATUS_VALUES | {"all"}:
                return CommandResult.failed("Unknown TODO status.", error_code="usage_error")
            filter_status = value
            index += 1
        elif normalized == "--thread" and index + 1 < len(args):
            thread_id = _selected_thread(args[index + 1], current_thread_id)
            index += 1
        elif normalized in {"--current-thread", "--thread-current"}:
            thread_id = current_thread_id
        else:
            return CommandResult.failed(
                "Usage: /todos [all|pending|in_progress|done] [--thread current|<id>]",
                error_code="usage_error",
            )
        index += 1
    return {"filter_status": filter_status, "thread_id": thread_id}


_UNSET = object()


def _parse_todo_mutation_args(
    args: Sequence[str],
    *,
    allow_status: bool,
    allow_clear: bool,
    default_thread_id: str | None,
    use_default_thread: bool,
) -> dict[str, Any] | CommandResult:
    parsed: dict[str, Any] = {
        "positionals": [],
        "notes": None,
        "scheduled_for": _UNSET,
        "recurrence": None,
        "status": None,
        "thread_id": default_thread_id if use_default_thread else None,
        "clear_schedule": False,
        "clear_recurrence": False,
    }
    index = 0
    while index < len(args):
        arg = args[index]
        normalized = arg.casefold()

        if normalized in {"--notes", "-n"}:
            value, index_or_error = _next_value(args, index, "--notes")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            parsed["notes"] = value
            index = index_or_error
        elif normalized in {"--schedule", "--scheduled-for", "--when"}:
            value, index_or_error = _next_value(args, index, "--schedule")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            parsed["scheduled_for"] = value
            index = index_or_error
        elif normalized == "--clear-schedule" and allow_clear:
            parsed["clear_schedule"] = True
            parsed["scheduled_for"] = _UNSET
        elif normalized == "--recurrence":
            value, index_or_error = _next_value(args, index, "--recurrence")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            try:
                canonical = validate_recurrence(value)
            except ValueError as exc:
                return CommandResult.failed(str(exc), error_code="usage_error")
            parsed["recurrence"] = canonical
            index = index_or_error
        elif normalized == "--clear-recurrence" and allow_clear:
            parsed["clear_recurrence"] = True
            parsed["recurrence"] = None
        elif normalized == "--status" and allow_status:
            value, index_or_error = _next_value(args, index, "--status")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            value = value.casefold()
            if value not in TODO_STATUS_VALUES:
                return CommandResult.failed("Unknown TODO status.", error_code="usage_error")
            parsed["status"] = value
            index = index_or_error
        elif normalized == "--thread":
            value, index_or_error = _next_value(args, index, "--thread")
            if isinstance(index_or_error, CommandResult):
                return index_or_error
            parsed["thread_id"] = _selected_thread(value, default_thread_id)
            index = index_or_error
        elif normalized.startswith("--"):
            return CommandResult.failed(f"Unknown option: {arg}", error_code="usage_error")
        else:
            parsed["positionals"].append(arg)
        index += 1

    if parsed["scheduled_for"] is _UNSET and not allow_clear:
        parsed["scheduled_for"] = None
    return parsed


def _next_value(
    args: Sequence[str],
    index: int,
    option: str,
) -> tuple[str, int | CommandResult]:
    if index + 1 >= len(args):
        return "", CommandResult.failed(f"{option} requires a value.", error_code="usage_error")
    return args[index + 1], index + 1


def _selected_thread(value: str, current_thread_id: str | None) -> str | None:
    if value.casefold() in {"current", "."}:
        return current_thread_id
    if value.casefold() in {"none", "default"}:
        return None
    return value


def _format_todos(
    todos: Sequence[Mapping[str, Any]],
    *,
    title: str,
) -> list[str]:
    lines = [title, "  ID        Status       Task                            Schedule        Recur"]
    for todo in todos:
        scheduled = one_line(todo.get("scheduled_for") or "", limit=15)
        recurrence = one_line(todo.get("recurrence") or "", limit=8)
        lines.append(
            f"  {compact_id(_todo_id(todo)):<8}  "
            f"{compact_id(_todo_status(todo), width=11):<11}  "
            f"{one_line(_todo_task(todo), limit=30):<30}  "
            f"{scheduled:<15}  {recurrence}"
        )
    return lines


def _mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _todo_id(todo: Mapping[str, Any] | Any) -> str:
    return str(mapping_get(todo, "id", ""))


def _todo_task(todo: Mapping[str, Any] | Any) -> str:
    return str(mapping_get(todo, "task", ""))


def _todo_status(todo: Mapping[str, Any] | Any) -> str:
    status = mapping_get(todo, "status", "")
    value = getattr(status, "value", status)
    return str(value or "")


def register(registry: CommandRegistry) -> None:
    """Register TODO commands."""
    registry.register(Command(
        name="todos",
        aliases=[],
        description="List TODOs",
        usage="/todos [all|pending|in_progress|done]",
        handler=_handle_todos_context,
        category="Personal",
    ))
    registry.register(Command(
        name="todo",
        aliases=[],
        description="Manage TODOs",
        usage="/todo list",
        handler=_handle_todo_root_context,
        category="Personal",
        subcommands={
            "list": Command(
                name="list",
                description="List TODOs",
                usage="list [all|pending|in_progress|done]",
                handler=_list_todos_context,
                category="Personal",
            ),
            "add": Command(
                name="add",
                description="Add TODO",
                usage="add <task>",
                handler=_handle_todo_add_context,
                category="Personal",
            ),
            "edit": Command(
                name="edit",
                description="Edit TODO",
                usage="edit <id> [new task] [options]",
                handler=_handle_todo_edit_context,
                category="Personal",
            ),
            "done": Command(
                name="done",
                aliases=["complete"],
                description="Complete TODO",
                usage="done <id>",
                handler=_handle_todo_done_context,
                category="Personal",
            ),
            "delete": Command(
                name="delete",
                aliases=["remove", "rm"],
                description="Delete TODO",
                usage="delete <id> [--yes]",
                handler=_handle_todo_delete_context,
                category="Personal",
            ),
            "schedule": Command(
                name="schedule",
                description="Set or clear TODO schedule",
                usage="schedule <id> <when|clear>",
                handler=_handle_todo_schedule_context,
                category="Personal",
            ),
            "recurrence": Command(
                name="recurrence",
                aliases=["repeat"],
                description="Set or clear TODO recurrence",
                usage="recurrence <id> <interval|clear>",
                handler=_handle_todo_recurrence_context,
                category="Personal",
            ),
        },
    ))
