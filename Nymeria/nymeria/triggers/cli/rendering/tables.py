"""Rich Table renderers for list commands."""

from __future__ import annotations

from typing import List, Dict, Any, Optional, TYPE_CHECKING

from rich.console import Console
from rich.table import Table
from rich import box

if TYPE_CHECKING:
    pass


def render_thread_table(
    console: Console,
    threads: List[Dict[str, Any]],
    active_thread_id: str,
) -> None:
    """Render a table of threads."""
    if not threads:
        console.print("[dim]No threads found.[/dim]")
        return

    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
    table.add_column("", width=2)  # active marker
    table.add_column("ID", style="dim", max_width=10)
    table.add_column("Title", min_width=20)
    table.add_column("Platform", style="dim")

    for t in threads:
        tid = t.get("thread_id", "")
        title = t.get("title", "New Chat")
        platform = t.get("platform", "desktop")
        marker = "[cyan]*[/cyan]" if tid == active_thread_id else " "
        id_display = tid[:8]

        table.add_row(marker, id_display, title, platform)

    console.print(table)


def render_todo_table(console: Console, todos: List[Any]) -> None:
    """Render a table of TODO items."""
    if not todos:
        console.print("[dim]No TODOs found.[/dim]")
        return

    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
    table.add_column("ID", style="dim", width=8)
    table.add_column("Status", width=12)
    table.add_column("Task", min_width=30)
    table.add_column("Thread", style="dim", max_width=10)

    status_colors = {
        "pending": "yellow",
        "in_progress": "cyan",
        "done": "green",
    }

    for item in todos:
        status = item.status.value if hasattr(item.status, "value") else str(item.status)
        color = status_colors.get(status, "white")
        task_display = item.task[:60] + "..." if len(item.task) > 60 else item.task
        thread_display = (item.thread_id or "")[:8]

        table.add_row(
            item.id,
            f"[{color}]{status}[/{color}]",
            task_display,
            thread_display,
        )

    console.print(table)


def render_tools_table(
    console: Console,
    tools: List[Dict[str, str]],
    title: Optional[str] = None,
) -> None:
    """Render a table of tools."""
    if not tools:
        console.print("[dim]No tools found.[/dim]")
        return

    table = Table(
        box=box.SIMPLE,
        show_edge=False,
        pad_edge=False,
        title=title,
        title_style="bold",
    )
    table.add_column("Name", min_width=20)
    table.add_column("Category", style="dim", width=12)
    table.add_column("Status", width=10)

    for t in tools:
        status = t.get("status", "enabled")
        status_style = "green" if status == "enabled" else "dim"
        table.add_row(
            t["name"],
            t.get("category", ""),
            f"[{status_style}]{status}[/{status_style}]",
        )

    console.print(table)


def render_memory_table(console: Console, memories: List[Any]) -> None:
    """Render a table of user memories."""
    if not memories:
        console.print("[dim]No memories saved.[/dim]")
        return

    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False)
    table.add_column("Key", min_width=15)
    table.add_column("Value", min_width=30)
    table.add_column("Uses", style="dim", width=5, justify="right")

    for mem in memories:
        value_display = mem.value[:50] + "..." if len(mem.value) > 50 else mem.value
        table.add_row(mem.key, value_display, str(mem.access_count))

    console.print(table)
