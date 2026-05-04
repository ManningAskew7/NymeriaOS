"""Thread management commands: /threads, /t."""

from __future__ import annotations

import logging
from typing import List, Dict, Any, TYPE_CHECKING

from rich.panel import Panel

from nymeria.core.thread_classification import classify_platform as _classify_platform

from . import Command, CommandRegistry
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


# ── Registration ───────────────────────────────────────────────────

def register(registry: CommandRegistry) -> None:
    """Register thread commands."""
    cmd = Command(
        name="threads",
        aliases=["/t"],
        description="List threads",
        handler=_handle_threads,
        subcommands={
            "switch": Command(name="switch", aliases=[], description="Switch thread", handler=_handle_switch),
            "s": Command(name="s", aliases=[], description="Switch thread", handler=_handle_switch, hidden=True),
            "new": Command(name="new", aliases=[], description="New thread", handler=_handle_new),
            "n": Command(name="n", aliases=[], description="New thread", handler=_handle_new, hidden=True),
            "delete": Command(name="delete", aliases=[], description="Delete thread", handler=_handle_delete),
            "info": Command(name="info", aliases=[], description="Thread info", handler=_handle_info),
            "rename": Command(name="rename", aliases=[], description="Rename thread", handler=_handle_rename),
        },
    )
    registry.register(cmd)
