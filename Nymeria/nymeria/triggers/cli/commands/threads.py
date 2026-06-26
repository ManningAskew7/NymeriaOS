"""Thread management commands: /threads, /t."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ._shared import (
    call_client_method,
    compact_id,
    confirmation_granted,
    confirmation_required_result,
    format_bool,
    has_client_method,
    mapping_get,
    normalize_thread_id,
    one_line,
    strip_confirmation_flags,
    thread_title,
    unsupported_transport_result,
    CommandClientMethodUnavailable,
)


@dataclass(frozen=True, slots=True)
class ThreadResolution:
    """Result of resolving a user-facing thread reference."""

    status: str
    ref: str
    thread: Mapping[str, Any] | None = None
    matches: tuple[Mapping[str, Any], ...] = ()

    @property
    def matched(self) -> bool:
        return self.status == "matched" and self.thread is not None


async def _handle_thread_root_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """List threads by default in the v2 command layer."""

    if args:
        return CommandResult.failed(
            "Usage: /thread list|switch|new|rename|delete|pin|config|branch|compact",
            error_code="usage_error",
        )
    return await _handle_list_context(context, args)


async def _handle_list_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        threads = await _list_threads(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/thread list", method_name=exc.method_name)
    teams = await _list_thread_teams_or_empty(context)

    if not threads:
        return CommandResult.completed(
            CommandMessage("No threads found.", level="warning"),
            json_payload=[],
        )

    lines = _format_thread_list(threads, context.thread_id, teams=teams)
    return CommandResult.completed(
        CommandMessage("\n".join(lines), title="Threads"),
        json_payload=list(threads),
    )


async def _handle_switch_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed(
            "Usage: /thread switch <id-or-title>",
            error_code="usage_error",
        )

    match = await _resolve_thread(
        context,
        " ".join(args).strip(),
        command="/thread switch",
    )
    if isinstance(match, CommandResult):
        return match
    thread_id = normalize_thread_id(match)
    title = thread_title(match)
    context.thread_id = thread_id
    await context.dispatch(
        {"type": "switch_thread", "thread_id": thread_id, "thread_label": title}
    )
    return CommandResult.completed(
        CommandMessage(
            f"Switched to {compact_id(thread_id)} {title}",
            level="success",
        ),
        payload={"thread_id": thread_id, "title": title, "thread_switched": True},
    )


async def _handle_new_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    title = " ".join(args).strip() or None
    try:
        created = await call_client_method(
            context,
            "create_thread",
            context.user_id,
            title=title,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/thread new", method_name=exc.method_name)

    if not isinstance(created, Mapping):
        return CommandResult.failed("Thread create response was not a mapping.")
    thread_id = normalize_thread_id(created)
    if not thread_id:
        return CommandResult.failed("Thread create response did not include a thread ID.")
    selected_title = thread_title(created)
    context.thread_id = thread_id
    await context.dispatch(
        {
            "type": "switch_thread",
            "thread_id": thread_id,
            "thread_label": selected_title,
        }
    )
    return CommandResult.completed(
        CommandMessage(
            f"New thread: {thread_id} {selected_title}",
            level="success",
        ),
        payload={"thread_id": thread_id, "title": selected_title},
    )


async def _handle_rename_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed("Usage: /thread rename <title>", error_code="usage_error")
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    title = " ".join(args).strip()
    try:
        await call_client_method(
            context,
            "update_thread_metadata",
            context.thread_id,
            context.user_id,
            title=title,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/thread rename", method_name=exc.method_name)

    await context.dispatch({"type": "set_thread_label", "thread_label": title})
    return CommandResult.completed(
        CommandMessage(f"Renamed to: {title}", level="success"),
        payload={"thread_id": context.thread_id, "title": title},
    )


async def _handle_delete_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    args, explicit_confirmation = strip_confirmation_flags(args)
    if not args:
        return CommandResult.failed(
            "Usage: /thread delete <id> [--yes]",
            error_code="usage_error",
        )

    match = await _resolve_thread(context, args[0], command="/thread delete")
    if isinstance(match, CommandResult):
        return match
    thread_id = normalize_thread_id(match)
    if thread_id == context.thread_id:
        return CommandResult.failed("Cannot delete the active thread. Switch first.")

    confirmed = await confirmation_granted(
        context,
        f"Delete thread {thread_id}?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return confirmation_required_result("/thread delete")

    try:
        await call_client_method(
            context,
            "delete_thread",
            thread_id,
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/thread delete", method_name=exc.method_name)

    return CommandResult.completed(
        CommandMessage(f"Deleted thread {compact_id(thread_id)}.", level="success"),
        payload={"thread_id": thread_id},
    )


async def _handle_pin_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    thread_ref, desired = _parse_pin_args(args, context.thread_id)
    if not thread_ref:
        return CommandResult.failed("Usage: /thread pin [id] [on|off|toggle]")

    match = await _resolve_thread(context, thread_ref, command="/thread pin")
    if isinstance(match, CommandResult):
        return match
    thread_id = normalize_thread_id(match)
    current = bool(match.get("pinned", False))
    pinned = not current if desired is None else desired

    try:
        await call_client_method(
            context,
            "update_thread_metadata",
            thread_id,
            context.user_id,
            pinned=pinned,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/thread pin", method_name=exc.method_name)

    if thread_id == context.thread_id:
        await context.dispatch({"type": "thread_metadata_updated"})
    return CommandResult.completed(
        CommandMessage(
            f"{'Pinned' if pinned else 'Unpinned'} {compact_id(thread_id)}.",
            level="success",
        ),
        payload={"thread_id": thread_id, "pinned": pinned},
    )


async def _handle_info_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    thread: Mapping[str, Any] | None = None
    try:
        threads = await _list_threads(context)
        for candidate in threads:
            if normalize_thread_id(candidate) == context.thread_id:
                thread = candidate
                break
    except CommandClientMethodUnavailable:
        thread = None

    stats: Mapping[str, Any] = {}
    try:
        context_stats = await call_client_method(
            context,
            "get_context_stats",
            context.thread_id,
            user_id=context.user_id,
        )
        if isinstance(context_stats, Mapping):
            stats = context_stats
    except CommandClientMethodUnavailable:
        stats = {}

    config = await _thread_config_or_none(context)
    return CommandResult.completed(
        CommandMessage(
            _format_thread_info(context.thread_id, thread, stats, config),
            title="Thread Info",
        )
    )


async def _handle_config_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    try:
        config = await call_client_method(
            context,
            "get_thread_config",
            context.thread_id,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/thread config", method_name=exc.method_name)

    if not isinstance(config, Mapping):
        return CommandResult.completed(CommandMessage("No thread config found.", level="warning"))
    return CommandResult.completed(
        CommandMessage(_format_thread_config(config), title="Thread Config")
    )


async def _handle_compact_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    args, explicit_confirmation = strip_confirmation_flags(args)
    if args:
        return CommandResult.failed(
            "Usage: /thread compact [--yes]",
            error_code="usage_error",
        )
    confirmed = await confirmation_granted(
        context,
        f"Compact thread {context.thread_id}?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return confirmation_required_result("/thread compact")

    try:
        result = await call_client_method(
            context,
            "compact",
            context.thread_id,
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/thread compact", method_name=exc.method_name)

    await context.dispatch({"type": "thread_context_updated"})
    return _compact_result(result)


async def _handle_branch_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    from_message_index, title, error = _parse_branch_args(args)
    if error:
        return CommandResult.failed(error, error_code="usage_error")

    try:
        result = await call_client_method(
            context,
            "branch_thread",
            context.thread_id,
            context.user_id,
            title=title,
            from_message_index=from_message_index,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/branch", method_name=exc.method_name)

    if not isinstance(result, Mapping):
        return CommandResult.failed("Thread branch response was not a mapping.")
    thread_id = normalize_thread_id(result)
    if not thread_id:
        return CommandResult.failed("Thread branch response did not include a thread ID.")
    selected_title = thread_title(result)
    copied_from = result.get("from_message_index")

    context.thread_id = thread_id
    await context.dispatch(
        {
            "type": "switch_thread",
            "thread_id": thread_id,
            "thread_label": selected_title,
        }
    )

    suffix = f" from message #{copied_from}" if copied_from else ""
    return CommandResult.completed(
        CommandMessage(
            f"Created branch '{selected_title}' ({compact_id(thread_id)}){suffix}.",
            level="success",
        ),
        payload={
            "thread_id": thread_id,
            "title": selected_title,
            "source_thread_id": result.get("source_thread_id"),
            "from_message_index": copied_from,
            "thread_switched": True,
        },
        json_payload=dict(result),
    )


async def _list_threads(context: CommandContext) -> list[Mapping[str, Any]]:
    threads = await call_client_method(context, "list_threads", context.user_id)
    if not isinstance(threads, Sequence) or isinstance(threads, (str, bytes)):
        return []
    return [thread for thread in threads if isinstance(thread, Mapping)]


async def _list_thread_teams_or_empty(context: CommandContext) -> list[Mapping[str, Any]]:
    if not has_client_method(context, "list_thread_teams"):
        return []
    try:
        raw = await call_client_method(context, "list_thread_teams", context.user_id)
    except CommandClientMethodUnavailable:
        return []
    if isinstance(raw, Mapping):
        raw = raw.get("teams", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [team for team in raw if isinstance(team, Mapping)]


async def _resolve_thread(
    context: CommandContext,
    partial: str,
    *,
    command: str,
) -> Mapping[str, Any] | CommandResult:
    try:
        threads = await _list_threads(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(command, method_name=exc.method_name)

    resolution = resolve_thread_reference(threads, partial)
    if resolution.matched:
        assert resolution.thread is not None
        return resolution.thread
    if resolution.status == "missing":
        return CommandResult.failed(f"No thread matching '{partial}'.")
    return CommandResult.completed(
        CommandMessage(
            _format_thread_resolution_ambiguity(partial, resolution.matches),
            level="warning",
        )
    )


def resolve_thread_reference(
    threads: Sequence[Mapping[str, Any]],
    ref: str,
) -> ThreadResolution:
    """Resolve a thread by exact ID, unique ID prefix, title, or title substring."""

    needle = " ".join(str(ref or "").split()).strip()
    if not needle:
        return ThreadResolution(status="missing", ref=needle)

    exact_id = [thread for thread in threads if normalize_thread_id(thread) == needle]
    if exact_id:
        return ThreadResolution(status="matched", ref=needle, thread=exact_id[0])

    prefix = [
        thread for thread in threads if normalize_thread_id(thread).startswith(needle)
    ]
    if len(prefix) == 1:
        return ThreadResolution(status="matched", ref=needle, thread=prefix[0])
    if len(prefix) > 1:
        return ThreadResolution(
            status="ambiguous",
            ref=needle,
            matches=tuple(prefix),
        )

    exact_title = [thread for thread in threads if thread_title(thread) == needle]
    if len(exact_title) == 1:
        return ThreadResolution(status="matched", ref=needle, thread=exact_title[0])
    if len(exact_title) > 1:
        return ThreadResolution(
            status="ambiguous",
            ref=needle,
            matches=tuple(exact_title),
        )

    folded = needle.casefold()
    substring = [
        thread
        for thread in threads
        if folded in thread_title(thread).casefold()
    ]
    if len(substring) == 1:
        return ThreadResolution(status="matched", ref=needle, thread=substring[0])
    if len(substring) > 1:
        return ThreadResolution(
            status="ambiguous",
            ref=needle,
            matches=tuple(substring),
        )
    return ThreadResolution(status="missing", ref=needle)


def _format_thread_resolution_ambiguity(
    ref: str,
    matches: Sequence[Mapping[str, Any]],
) -> str:
    preview = ", ".join(
        f"{compact_id(normalize_thread_id(item))} {thread_title(item)}"
        for item in matches[:5]
    )
    return f"Ambiguous: {len(matches)} threads match '{ref}': {preview}"


def _format_thread_list(
    threads: Sequence[Mapping[str, Any]],
    active_thread_id: str | None,
    *,
    teams: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    by_id = {normalize_thread_id(thread): thread for thread in threads}
    grouped_ids = _team_thread_ids(teams)
    lines = ["Threads"]

    if teams:
        for team in teams:
            team_threads = _ordered_threads(
                [
                    by_id.get(thread_id)
                    for thread_id in _team_ids(team)
                    if thread_id in by_id
                ]
            )
            if not team_threads:
                continue
            lines.append(f"  Team: {one_line(team.get('name') or team.get('id'), limit=60)}")
            lines.append("    * ID        Pin  Title                         Platform")
            lines.extend(
                _format_thread_rows(
                    team_threads,
                    active_thread_id,
                    indent="    ",
                )
            )

    ungrouped = [
        thread for thread in threads if normalize_thread_id(thread) not in grouped_ids
    ]
    pinned = _ordered_threads([thread for thread in ungrouped if thread.get("pinned")])
    recent = _ordered_threads([thread for thread in ungrouped if not thread.get("pinned")])
    if teams:
        if pinned:
            lines.append("  Pinned")
            lines.append("    * ID        Pin  Title                         Platform")
            lines.extend(_format_thread_rows(pinned, active_thread_id, indent="    "))
        if recent:
            lines.append("  Recent")
            lines.append("    * ID        Pin  Title                         Platform")
            lines.extend(_format_thread_rows(recent, active_thread_id, indent="    "))
    else:
        lines.append("  * ID        Pin  Title                         Platform")
        lines.extend(
            _format_thread_rows([*pinned, *recent], active_thread_id, indent="  ")
        )
    return lines


def _ordered_threads(threads: Sequence[Mapping[str, Any] | None]) -> list[Mapping[str, Any]]:
    selected = [thread for thread in threads if isinstance(thread, Mapping)]
    selected.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return selected


def _format_thread_rows(
    threads: Sequence[Mapping[str, Any]],
    active_thread_id: str | None,
    *,
    indent: str,
) -> list[str]:
    rows: list[str] = []
    for thread in threads:
        thread_id = normalize_thread_id(thread)
        active = "*" if thread_id == active_thread_id else " "
        pin = "*" if thread.get("pinned") else " "
        title = one_line(thread_title(thread), limit=28)
        platform = one_line(thread.get("platform") or "", limit=12)
        rows.append(
            f"{indent}{active} {compact_id(thread_id):<8}  "
            f"{pin:<3}  {title:<28}  {platform}"
        )
    return rows


def _team_thread_ids(teams: Sequence[Mapping[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for team in teams:
        ids.update(_team_ids(team))
    return ids


def _team_ids(team: Mapping[str, Any]) -> list[str]:
    raw = team.get("thread_ids") or team.get("threadIds") or []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [str(thread_id) for thread_id in raw if str(thread_id)]


def _parse_pin_args(
    args: Sequence[str],
    current_thread_id: str | None,
) -> tuple[str | None, bool | None]:
    if not args:
        return current_thread_id, None
    first = args[0].casefold()
    state_values = {
        "on": True,
        "true": True,
        "yes": True,
        "off": False,
        "false": False,
        "no": False,
        "toggle": None,
    }
    if first in state_values:
        return current_thread_id, state_values[first]
    if len(args) == 1:
        return args[0], None
    second = args[1].casefold()
    if second not in state_values:
        return args[0], None
    return args[0], state_values[second]


def _parse_branch_args(args: Sequence[str]) -> tuple[int | None, str | None, str]:
    from_message_index: int | None = None
    title_parts: list[str] = []
    i = 0
    while i < len(args):
        arg = str(args[i])
        if arg in {"--from", "-f"}:
            if i + 1 >= len(args):
                return None, None, "Usage: /branch [--from N] [title]"
            raw_index = str(args[i + 1])
            try:
                from_message_index = int(raw_index)
            except ValueError:
                return None, None, "--from must be an integer message index"
            i += 2
            continue
        if arg.startswith("--from="):
            raw_index = arg.split("=", 1)[1]
            try:
                from_message_index = int(raw_index)
            except ValueError:
                return None, None, "--from must be an integer message index"
            i += 1
            continue
        title_parts.append(arg)
        i += 1

    if from_message_index is not None and from_message_index < 1:
        return None, None, "--from must be 1 or greater"
    title = " ".join(title_parts).strip() or None
    return from_message_index, title, ""


async def _thread_config_or_none(context: CommandContext) -> Mapping[str, Any] | None:
    if not has_client_method(context, "get_thread_config") or not context.thread_id:
        return None
    try:
        config = await call_client_method(
            context,
            "get_thread_config",
            context.thread_id,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable:
        return None
    return config if isinstance(config, Mapping) else None


def _format_thread_info(
    thread_id: str,
    thread: Mapping[str, Any] | None,
    stats: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> str:
    rows = [
        ("Thread ID", thread_id),
        ("Title", thread_title(thread or {})),
        ("Pinned", format_bool((thread or {}).get("pinned", False))),
        ("Platform", (thread or {}).get("platform", "")),
        ("Model", _effective_model(stats, config)),
        ("Context", _context_usage(stats)),
        ("Callable", format_bool((config or {}).get("callable", False))),
        ("Enabled tools", _csv((config or {}).get("enabled_tools"))),
        ("Disabled tools", _csv((config or {}).get("disabled_tools"))),
    ]
    if config and config.get("instructions"):
        rows.append(("Instructions", one_line(config.get("instructions"), limit=80)))
    return "\n".join(_aligned_rows(rows, "Thread Info"))


def _format_thread_config(config: Mapping[str, Any]) -> str:
    llm_config = config.get("llm_config") or {}
    if not isinstance(llm_config, Mapping):
        llm_config = {}
    rows = [
        ("Thread ID", config.get("thread_id", "")),
        ("Callable", format_bool(config.get("callable", False))),
        ("Callable name", config.get("callable_name", "") or ""),
        ("Instructions", one_line(config.get("instructions"), limit=80)),
        ("System prompt", one_line(config.get("system_prompt"), limit=80)),
        ("Enabled tools", _csv(config.get("enabled_tools"))),
        ("Disabled tools", _csv(config.get("disabled_tools"))),
        ("Enabled skills", _csv(config.get("enabled_skills"))),
        ("Disabled skills", _csv(config.get("disabled_skills"))),
        ("LLM provider", llm_config.get("provider", "") or ""),
        ("LLM model", llm_config.get("model", "") or ""),
    ]
    return "\n".join(_aligned_rows(rows, "Thread Config"))


def _compact_result(result: Any) -> CommandResult:
    if isinstance(result, Mapping) and result.get("success") is False:
        reason = result.get("reason", "Unknown reason")
        return CommandResult.completed(CommandMessage(f"Skipped: {reason}", level="warning"))
    removed = mapping_get(result, "messages_removed", None)
    before = mapping_get(result, "messages_before", None)
    after = mapping_get(result, "messages_after", None)
    if removed is not None:
        detail = f" Removed {removed} messages"
        if before is not None and after is not None:
            detail += f" ({before} -> {after})"
        detail += "."
    else:
        detail = "."
    return CommandResult.completed(
        CommandMessage(f"Compaction requested{detail}", level="success")
    )


def _effective_model(
    stats: Mapping[str, Any],
    config: Mapping[str, Any] | None,
) -> str:
    llm_config = (config or {}).get("llm_config") or {}
    if isinstance(llm_config, Mapping) and llm_config.get("model"):
        return str(llm_config["model"])
    return str(stats.get("model") or stats.get("active_model") or "")


def _context_usage(stats: Mapping[str, Any]) -> str:
    total = stats.get("total_tokens", stats.get("used_tokens", ""))
    limit = stats.get("context_limit", stats.get("max_tokens", ""))
    pct = stats.get("usage_percentage")
    parts = []
    if total != "":
        parts.append(f"{total:,}" if isinstance(total, int) else str(total))
    if limit != "":
        rendered_limit = f"{limit:,}" if isinstance(limit, int) else str(limit)
        parts.append(f"/ {rendered_limit}")
    if pct is not None:
        parts.append(f"({pct}%)")
    return " ".join(parts)


def _csv(value: Any) -> str:
    if not value:
        return "None"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return ", ".join(str(item) for item in value) or "None"
    return str(value)


def _aligned_rows(rows: Sequence[tuple[str, Any]], title: str) -> list[str]:
    width = max((len(label) for label, _value in rows), default=0)
    lines = [title]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {'' if value is None else value}")
    return lines


# ── Registration ───────────────────────────────────────────────────

def register(registry: CommandRegistry) -> None:
    """Register thread commands."""
    cmd = Command(
        name="thread",
        aliases=["/threads", "/t"],
        description="Manage threads",
        usage="/thread list",
        handler=_handle_thread_root_context,
        category="Threads",
        subcommands={
            "list": Command(
                name="list",
                description="List threads",
                usage="list",
                handler=_handle_list_context,
                category="Threads",
            ),
            "switch": Command(
                name="switch",
                aliases=["s"],
                description="Switch thread",
                usage="switch <id>",
                handler=_handle_switch_context,
                category="Threads",
            ),
            "new": Command(
                name="new",
                aliases=["n"],
                description="New thread",
                usage="new [title]",
                handler=_handle_new_context,
                category="Threads",
            ),
            "delete": Command(
                name="delete",
                aliases=["del", "rm"],
                description="Delete thread",
                usage="delete <id> [--yes]",
                handler=_handle_delete_context,
                category="Threads",
            ),
            "info": Command(
                name="info",
                description="Thread info",
                usage="info",
                handler=_handle_info_context,
                category="Threads",
            ),
            "rename": Command(
                name="rename",
                description="Rename thread",
                usage="rename <title>",
                handler=_handle_rename_context,
                category="Threads",
            ),
            "pin": Command(
                name="pin",
                description="Pin or unpin a thread",
                usage="pin [id] [on|off|toggle]",
                handler=_handle_pin_context,
                category="Threads",
            ),
            "config": Command(
                name="config",
                description="Show thread config",
                usage="config",
                handler=_handle_config_context,
                category="Threads",
            ),
            "branch": Command(
                name="branch",
                aliases=["fork"],
                description="Branch the current thread",
                usage="branch [--from N] [title]",
                handler=_handle_branch_context,
                category="Threads",
            ),
            "compact": Command(
                name="compact",
                description="Compact context",
                usage="compact [--yes]",
                handler=_handle_compact_context,
                category="Threads",
            ),
        },
    )
    registry.register(cmd)
    registry.register(Command(
        name="branch",
        aliases=["/fork"],
        description="Branch the current thread",
        usage="/branch [--from N] [title]",
        handler=_handle_branch_context,
        category="Threads",
    ))
