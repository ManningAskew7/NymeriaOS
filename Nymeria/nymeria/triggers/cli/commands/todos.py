"""TODO commands: /todos, /todo add, /todo done, /todo delete."""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from . import Command, CommandRegistry
from ..rendering.tables import render_todo_table

if TYPE_CHECKING:
    from ..state import CLIState


def _handle_todos(state: "CLIState", args: List[str]) -> None:
    """List all TODOs."""
    todo_list = state.todo_manager.get_todos(state.user_id)
    items = todo_list.items
    if not items:
        state.console.print("[dim]No TODOs.[/dim]")
        return
    render_todo_table(state.console, items)


def _handle_todo_add(state: "CLIState", args: List[str]) -> None:
    """Add a new TODO."""
    if not args:
        state.console.print("[red]Usage: /todo add <task>[/red]")
        return

    task = " ".join(args)
    with state.todo_manager.atomic_update(state.user_id) as todo_list:
        todo_list.add_item(task)
        # Set thread_id on the newly added item
        if todo_list.items:
            todo_list.items[-1].thread_id = state.thread_id
    state.console.print(f"[green]Added TODO: {task}[/green]")


def _handle_todo_done(state: "CLIState", args: List[str]) -> None:
    """Complete a TODO by partial ID."""
    if not args:
        state.console.print("[red]Usage: /todo done <id>[/red]")
        return

    partial = args[0]
    todo_list = state.todo_manager.get_todos(state.user_id)
    matches = [item for item in todo_list.items if item.id.startswith(partial)]

    if len(matches) == 0:
        state.console.print(f"[red]No TODO matching '{partial}'.[/red]")
    elif len(matches) > 1:
        state.console.print(f"[yellow]Ambiguous — {len(matches)} TODOs match. Be more specific.[/yellow]")
    else:
        with state.todo_manager.atomic_update(state.user_id) as tl:
            tl.complete_item(matches[0].id)
        state.console.print(f"[green]Completed: {matches[0].task}[/green]")


def _handle_todo_delete(state: "CLIState", args: List[str]) -> None:
    """Delete a TODO by partial ID."""
    if not args:
        state.console.print("[red]Usage: /todo delete <id>[/red]")
        return

    partial = args[0]
    todo_list = state.todo_manager.get_todos(state.user_id)
    matches = [item for item in todo_list.items if item.id.startswith(partial)]

    if len(matches) == 0:
        state.console.print(f"[red]No TODO matching '{partial}'.[/red]")
    elif len(matches) > 1:
        state.console.print(f"[yellow]Ambiguous — {len(matches)} TODOs match. Be more specific.[/yellow]")
    else:
        with state.todo_manager.atomic_update(state.user_id) as tl:
            tl.delete_item(matches[0].id)
        state.console.print(f"[green]Deleted: {matches[0].task}[/green]")


def register(registry: CommandRegistry) -> None:
    """Register TODO commands."""
    # /todos — list all
    registry.register(Command(
        name="todos",
        aliases=[],
        description="List TODOs",
        handler=_handle_todos,
    ))
    # /todo — with subcommands add, done, delete
    registry.register(Command(
        name="todo",
        aliases=[],
        description="Manage TODOs",
        handler=_handle_todos,  # bare /todo also lists
        subcommands={
            "add": Command(name="add", aliases=[], description="Add TODO", handler=_handle_todo_add),
            "done": Command(name="done", aliases=[], description="Complete TODO", handler=_handle_todo_done),
            "delete": Command(name="delete", aliases=[], description="Delete TODO", handler=_handle_todo_delete),
        },
    ))
