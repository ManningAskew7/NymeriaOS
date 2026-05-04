"""System commands: /help, /clear, /exit, /history, /settings."""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from rich.panel import Panel
from rich.text import Text

from . import Command, CommandRegistry

if TYPE_CHECKING:
    from ..state import CLIState


def _handle_help(state: "CLIState", args: List[str]) -> None:
    """Show categorized help."""
    sections = {
        "Thread": [
            ("/threads, /t", "List all threads"),
            ("/threads switch <id>, /t s <id>", "Switch thread (partial ID)"),
            ("/threads new [name], /t n", "Create new thread"),
            ("/threads delete <id>", "Delete a thread"),
            ("/threads info", "Current thread details"),
            ("/threads rename <title>", "Rename current thread"),
        ],
        "Model": [
            ("/model", "Show effective model"),
            ("/model set <model-id>", "Set per-thread model override"),
        ],
        "Tools": [
            ("/tools", "List tools for current thread"),
            ("/tools enable <name>", "Enable an optional tool"),
            ("/tools disable <name>", "Disable a tool"),
            ("/tools optional", "List all optional tools"),
        ],
        "TODOs": [
            ("/todos", "List all TODOs"),
            ("/todo add <task>", "Add a TODO"),
            ("/todo done <id>", "Complete a TODO"),
            ("/todo delete <id>", "Delete a TODO"),
        ],
        "Memory": [
            ("/memory list", "List saved memories"),
            ("/memory save <key> <value>", "Save a memory"),
            ("/memory forget <key>", "Remove a memory"),
        ],
        "Context": [
            ("/context", "Show context window stats"),
            ("/compact", "Trigger manual compaction"),
        ],
        "System": [
            ("/help, /h", "Show this help"),
            ("/settings", "Show global settings"),
            ("/history", "Show conversation history"),
            ("/clear", "Clear screen"),
            ("/exit, /quit", "Exit the CLI"),
        ],
    }

    body = Text()
    for section_name, cmds in sections.items():
        body.append(f"\n {section_name}\n", style="bold")
        for cmd, desc in cmds:
            body.append(f"  {cmd:<38}", style="cyan")
            body.append(f" {desc}\n", style="dim")

    state.console.print(Panel(body, title="Commands", border_style="green", padding=(0, 1)))


def _handle_clear(state: "CLIState", args: List[str]) -> None:
    """Clear terminal and re-render welcome."""
    from ..rendering.welcome import render_welcome

    state.console.clear()
    render_welcome(state)


def _handle_exit(state: "CLIState", args: List[str]) -> None:
    """Exit the CLI."""
    state.running = False
    state.console.print("[dim]Goodbye![/dim]")


def _handle_history(state: "CLIState", args: List[str]) -> None:
    """Show conversation history."""
    history = state.agent.get_conversation_history(state.thread_id)
    if not history:
        state.console.print("[dim]No conversation history.[/dim]")
        return

    for msg in history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if role == "human":
            text = content if isinstance(content, str) else str(content)
            state.console.print(f"\n[bold cyan]You:[/bold cyan] {text[:200]}")
        elif role == "assistant":
            if isinstance(content, str):
                preview = content[:200]
            elif isinstance(content, list):
                # Collect text from content blocks
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                preview = " ".join(parts)[:200]
            else:
                preview = str(content)[:200]
            if preview:
                state.console.print(f"[bold green]Nymeria:[/bold green] {preview}...")

        # Show tool calls inline
        tool_calls = msg.get("toolCalls", [])
        for tc in tool_calls:
            name = tc.get("name", "unknown")
            state.console.print(f"  [yellow]> {name}[/yellow]")


def _handle_settings(state: "CLIState", args: List[str]) -> None:
    """Display current global settings."""
    s = state.settings

    lines = [
        f"  Provider        {s.llm_provider}",
        f"  Model           {s.llm_model}",
        f"  Temperature     {s.llm_temperature}",
        f"  Extended think  {s.llm_extended_thinking}",
        f"  Context mgmt    {s.context_management}",
        f"  Compact at      {int(s.compact_threshold * 100)}%",
        f"  Database        {s.database_backend}",
        f"  Watchdog        {'on' if s.watchdog_enabled else 'off'} (every {s.watchdog_interval_minutes}m)",
        f"  Timezone        {s.user_timezone}",
    ]

    if s.llm_base_url:
        lines.insert(2, f"  Base URL        {s.llm_base_url}")

    body = "\n".join(lines)
    state.console.print(Panel(body, title="Settings", border_style="dim", padding=(0, 1)))


def register(registry: CommandRegistry) -> None:
    """Register all system commands."""
    registry.register(Command(
        name="help",
        aliases=["/h"],
        description="Show help",
        handler=_handle_help,
    ))
    registry.register(Command(
        name="clear",
        aliases=[],
        description="Clear screen",
        handler=_handle_clear,
    ))
    registry.register(Command(
        name="exit",
        aliases=["/quit", "/q"],
        description="Exit the CLI",
        handler=_handle_exit,
    ))
    registry.register(Command(
        name="history",
        aliases=[],
        description="Show conversation history",
        handler=_handle_history,
    ))
    registry.register(Command(
        name="settings",
        aliases=[],
        description="Show global settings",
        handler=_handle_settings,
    ))
