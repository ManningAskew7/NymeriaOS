"""Thread management commands: /threads, /t."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import List, Dict, Any, TYPE_CHECKING

from rich.panel import Panel

from nymeria.core.thread_classification import classify_platform as _classify_platform

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
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
from ..rendering.tables import render_thread_table

if TYPE_CHECKING:
    from ..state import CLIState

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────

def _get_checkpoint_thread_ids(state: "CLIState") -> List[str]:
    """Query distinct thread IDs from the checkpoint database."""
    settings = state.settings
    thread_ids: List[str] = []

    if settings.database_backend == "sqlite":
        import sqlite3
        try:
            conn = sqlite3.connect(str(settings.db_path))
            cursor = conn.execute("SELECT DISTINCT thread_id FROM checkpoints")
            thread_ids = [row[0] for row in cursor.fetchall()]
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to query thread IDs from SQLite: {e}")

    elif settings.database_backend == "postgres":
        try:
            import psycopg  # type: ignore[import-untyped]
            with psycopg.connect(settings.postgres_uri) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                    thread_ids = [row[0] for row in cur.fetchall()]
        except Exception as e:
            logger.warning(f"Failed to query thread IDs from PostgreSQL: {e}")

    return thread_ids


def _build_thread_list(state: "CLIState") -> List[Dict[str, Any]]:
    """Build a merged thread list from checkpoints + metadata."""
    checkpoint_ids = _get_checkpoint_thread_ids(state)
    checkpoint_set = set(checkpoint_ids)
    store = state.thread_metadata_manager.get_store(state.user_id)

    threads: List[Dict[str, Any]] = []

    for tid in checkpoint_ids:
        meta = store.threads.get(tid)
        if meta:
            threads.append(meta.model_dump(mode="json"))
        else:
            threads.append({
                "thread_id": tid,
                "title": "New Chat",
                "pinned": False,
                "platform": _classify_platform(tid),
                "platform_meta": None,
                "created_at": None,
                "updated_at": None,
                "title_source": "default",
            })

    # Metadata-only threads (created but no checkpoint yet)
    for tid, meta in store.threads.items():
        if tid not in checkpoint_set:
            threads.append(meta.model_dump(mode="json"))

    return threads


def _find_thread_by_partial_id(
    threads: List[Dict[str, Any]], partial: str
) -> List[Dict[str, Any]]:
    """Find threads matching a partial ID prefix."""
    return [t for t in threads if t["thread_id"].startswith(partial)]


# ── Handlers ───────────────────────────────────────────────────────

def _handle_threads(state: "CLIState", args: List[str]) -> None:
    """List all threads (default when no subcommand given)."""
    threads = _build_thread_list(state)
    if not threads:
        state.console.print("[dim]No threads found.[/dim]")
        return

    # Sort: pinned first, then most recently updated
    threads.sort(
        key=lambda t: (not t.get("pinned", False), t.get("updated_at") or ""),
        reverse=False,
    )
    # Actually: pinned first (True sorts after False by default, so negate),
    # then updated_at descending
    threads.sort(
        key=lambda t: (
            0 if t.get("pinned", False) else 1,
            t.get("updated_at") or "",
        ),
    )
    # Reverse the updated_at part — just use a simple two-pass approach:
    # pinned threads first, then within each group sort by updated_at desc
    pinned = [t for t in threads if t.get("pinned")]
    unpinned = [t for t in threads if not t.get("pinned")]
    pinned.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
    unpinned.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
    threads = pinned + unpinned

    render_thread_table(state.console, threads, state.thread_id)


def _handle_switch(state: "CLIState", args: List[str]) -> None:
    """Switch to a thread by partial ID match."""
    if not args:
        state.console.print("[red]Usage: /threads switch <id>[/red]")
        return

    partial = args[0]
    threads = _build_thread_list(state)
    matches = _find_thread_by_partial_id(threads, partial)

    if len(matches) == 0:
        state.console.print(f"[red]No thread matching '{partial}'.[/red]")
    elif len(matches) == 1:
        tid = matches[0]["thread_id"]
        title = matches[0].get("title", "New Chat")
        state.switch_thread(tid)
        state.console.print(f"[green]Switched to {tid[:8]}[/green] [dim]{title}[/dim]")
    else:
        state.console.print(f"[yellow]Ambiguous — {len(matches)} threads match '{partial}':[/yellow]")
        for m in matches[:5]:
            state.console.print(f"  {m['thread_id'][:8]}  {m.get('title', 'New Chat')}")


def _handle_new(state: "CLIState", args: List[str]) -> None:
    """Create a new thread, optionally with a title."""
    title = " ".join(args) if args else None
    new_id = state.new_thread(title)
    label = title or new_id
    state.console.print(f"[green]New thread: {new_id}[/green] [dim]{label}[/dim]")


def _handle_delete(state: "CLIState", args: List[str]) -> None:
    """Delete a thread: metadata + checkpoints + config."""
    if not args:
        state.console.print("[red]Usage: /threads delete <id>[/red]")
        return

    partial = args[0]
    threads = _build_thread_list(state)
    matches = _find_thread_by_partial_id(threads, partial)

    if len(matches) == 0:
        state.console.print(f"[red]No thread matching '{partial}'.[/red]")
        return
    if len(matches) > 1:
        state.console.print(f"[yellow]Ambiguous — {len(matches)} threads match. Be more specific.[/yellow]")
        return

    tid = matches[0]["thread_id"]
    if tid == state.thread_id:
        state.console.print("[red]Cannot delete the active thread. Switch first.[/red]")
        return

    settings = state.settings

    # 1. Delete metadata
    state.thread_metadata_manager.delete_thread(state.user_id, tid)

    # 2. Delete checkpoints
    try:
        from ...core.checkpoint_cleanup import delete_thread_checkpoints
        delete_thread_checkpoints(settings, tid)
    except Exception as e:
        logger.warning(f"Failed to delete checkpoints for {tid}: {e}")

    # 3. Delete thread config
    try:
        tc = state.thread_config_manager.get_config(tid)
        was_callable = tc.callable if tc else False
        state.thread_config_manager.delete_config(tid)
        state.agent.invalidate_thread_config_cache(tid)
        if was_callable:
            state.agent.sync_agent_tools()
    except Exception as e:
        logger.warning(f"Failed to delete thread config for {tid}: {e}")

    state.console.print(f"[green]Deleted thread {tid[:8]}.[/green]")


def _handle_info(state: "CLIState", args: List[str]) -> None:
    """Show details about the current thread."""
    tid = state.thread_id
    title = state.get_thread_title()
    model = state.get_effective_model()

    # Context stats
    stats = state.agent.get_context_stats(tid)
    usage_pct = stats.get("usage_percentage", 0)
    total = stats.get("total_tokens", 0)
    limit = stats.get("context_limit", 0)

    # Thread config
    tc = state.thread_config_manager.get_config(tid)
    enabled = tc.enabled_tools if tc else []
    disabled = tc.disabled_tools if tc else []
    instructions = (tc.instructions[:80] + "...") if tc and tc.instructions else "None"
    callable_status = "Yes" if tc and tc.callable else "No"

    body = (
        f"  Thread ID       {tid}\n"
        f"  Title           {title}\n"
        f"  Model           {model}\n"
        f"  Callable        {callable_status}\n"
        f"  Context         {total:,} / {limit:,} ({usage_pct}%)\n"
        f"  Instructions    {instructions}\n"
        f"  Enabled tools   {', '.join(enabled) if enabled else 'None'}\n"
        f"  Disabled tools  {', '.join(disabled) if disabled else 'None'}"
    )

    state.console.print(Panel(body, title="Thread Info", border_style="dim", padding=(0, 1)))


def _handle_rename(state: "CLIState", args: List[str]) -> None:
    """Rename the current thread."""
    if not args:
        state.console.print("[red]Usage: /threads rename <title>[/red]")
        return

    new_title = " ".join(args)
    state.thread_metadata_manager.upsert_thread(
        state.user_id, state.thread_id, title=new_title, title_source="user"
    )
    state.console.print(f"[green]Renamed to: {new_title}[/green]")


async def _handle_thread_root_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """List threads by default in the v2 command layer."""

    if context.legacy_state is not None:
        _handle_threads(context.legacy_state, args)
        return CommandResult.completed()
    if args:
        return CommandResult.failed(
            "Usage: /thread list|switch|new|rename|delete|pin|config|compact|stop",
            error_code="usage_error",
        )
    return await _handle_list_context(context, args)


async def _handle_list_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        _handle_threads(context.legacy_state, [])
        return CommandResult.completed()

    try:
        threads = await _list_threads(context)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/thread list", method_name=exc.method_name)

    if not threads:
        return CommandResult.completed(CommandMessage("No threads found.", level="warning"))

    lines = _format_thread_list(threads, context.thread_id)
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Threads"))


async def _handle_switch_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        _handle_switch(context.legacy_state, args)
        return CommandResult.completed()
    if not args:
        return CommandResult.failed("Usage: /thread switch <id>", error_code="usage_error")

    match = await _resolve_thread(context, args[0], command="/thread switch")
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
        payload={"thread_id": thread_id, "title": title},
    )


async def _handle_new_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        _handle_new(context.legacy_state, args)
        return CommandResult.completed()

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
    if context.legacy_state is not None:
        _handle_rename(context.legacy_state, args)
        return CommandResult.completed()
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
    if context.legacy_state is not None:
        _handle_delete(context.legacy_state, args)
        return CommandResult.completed()

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
    if context.legacy_state is not None:
        return CommandResult.failed(
            "/thread pin is only available in the new command layer.",
            error_code="legacy_command_unavailable",
        )

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
    if context.legacy_state is not None:
        _handle_info(context.legacy_state, [])
        return CommandResult.completed()

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
    if context.legacy_state is not None:
        _handle_info(context.legacy_state, [])
        return CommandResult.completed()
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
    if context.legacy_state is not None:
        _handle_compact_legacy(context.legacy_state, args)
        return CommandResult.completed()
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


async def _handle_stop_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        context.legacy_state.agent.abort_with_cascade(context.legacy_state.thread_id)
        context.legacy_state.console.print("[green]Stop requested.[/green]")
        return CommandResult.completed()
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    try:
        await call_client_method(
            context,
            "stop",
            context.thread_id,
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/thread stop", method_name=exc.method_name)

    return CommandResult.completed(
        CommandMessage(f"Stop requested for {compact_id(context.thread_id)}.", level="success")
    )


def _handle_compact_legacy(state: "CLIState", args: list[str]) -> None:
    from .context import _handle_compact

    _handle_compact(state, args)


async def _list_threads(context: CommandContext) -> list[Mapping[str, Any]]:
    threads = await call_client_method(context, "list_threads", context.user_id)
    if not isinstance(threads, Sequence) or isinstance(threads, (str, bytes)):
        return []
    return [thread for thread in threads if isinstance(thread, Mapping)]


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

    matches = [
        thread
        for thread in threads
        if normalize_thread_id(thread).startswith(partial)
    ]
    if not matches:
        return CommandResult.failed(f"No thread matching '{partial}'.")
    if len(matches) > 1:
        preview = ", ".join(
            f"{compact_id(normalize_thread_id(item))} {thread_title(item)}"
            for item in matches[:5]
        )
        return CommandResult.completed(
            CommandMessage(
                f"Ambiguous: {len(matches)} threads match '{partial}': {preview}",
                level="warning",
            )
        )
    return matches[0]


def _format_thread_list(
    threads: Sequence[Mapping[str, Any]],
    active_thread_id: str | None,
) -> list[str]:
    ordered = sorted(
        threads,
        key=lambda item: (
            0 if item.get("pinned") else 1,
            str(item.get("updated_at") or ""),
        ),
        reverse=False,
    )
    pinned = [item for item in ordered if item.get("pinned")]
    unpinned = [item for item in ordered if not item.get("pinned")]
    pinned.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    unpinned.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)

    lines = ["Threads", "  * ID        Pin  Title                         Platform"]
    for thread in [*pinned, *unpinned]:
        thread_id = normalize_thread_id(thread)
        active = "*" if thread_id == active_thread_id else " "
        pin = "*" if thread.get("pinned") else " "
        title = one_line(thread_title(thread), limit=28)
        platform = one_line(thread.get("platform") or "", limit=12)
        lines.append(f"  {active} {compact_id(thread_id):<8}  {pin:<3}  {title:<28}  {platform}")
    return lines


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
        handler_mode="context",
        category="Threads",
        subcommands={
            "list": Command(
                name="list",
                description="List threads",
                usage="list",
                handler=_handle_list_context,
                handler_mode="context",
                category="Threads",
            ),
            "switch": Command(
                name="switch",
                aliases=["s"],
                description="Switch thread",
                usage="switch <id>",
                handler=_handle_switch_context,
                handler_mode="context",
                category="Threads",
            ),
            "new": Command(
                name="new",
                aliases=["n"],
                description="New thread",
                usage="new [title]",
                handler=_handle_new_context,
                handler_mode="context",
                category="Threads",
            ),
            "delete": Command(
                name="delete",
                aliases=["del", "rm"],
                description="Delete thread",
                usage="delete <id> [--yes]",
                handler=_handle_delete_context,
                handler_mode="context",
                category="Threads",
            ),
            "info": Command(
                name="info",
                description="Thread info",
                usage="info",
                handler=_handle_info_context,
                handler_mode="context",
                category="Threads",
            ),
            "rename": Command(
                name="rename",
                description="Rename thread",
                usage="rename <title>",
                handler=_handle_rename_context,
                handler_mode="context",
                category="Threads",
            ),
            "pin": Command(
                name="pin",
                description="Pin or unpin a thread",
                usage="pin [id] [on|off|toggle]",
                handler=_handle_pin_context,
                handler_mode="context",
                category="Threads",
            ),
            "config": Command(
                name="config",
                description="Show thread config",
                usage="config",
                handler=_handle_config_context,
                handler_mode="context",
                category="Threads",
            ),
            "compact": Command(
                name="compact",
                description="Compact context",
                usage="compact [--yes]",
                handler=_handle_compact_context,
                handler_mode="context",
                category="Threads",
            ),
            "stop": Command(
                name="stop",
                description="Stop active turn",
                usage="stop",
                handler=_handle_stop_context,
                handler_mode="context",
                category="Threads",
            ),
        },
    )
    registry.register(cmd)
