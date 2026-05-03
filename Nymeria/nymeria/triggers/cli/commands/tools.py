"""Tool commands: /tools, /tools enable, /tools disable, /tools optional."""

from __future__ import annotations

from typing import List, Dict, TYPE_CHECKING

from . import Command, CommandRegistry
from ..rendering.tables import render_tools_table

if TYPE_CHECKING:
    from ..state import CLIState


def _user_role(state: "CLIState") -> str:
    try:
        user = state.agent.accounts_repo.get_user_by_id(state.user_id)
        return user.role if user else "user"
    except Exception:
        return "user"


def _get_loaded_tools(state: "CLIState") -> List[Dict[str, str]]:
    """Get tools currently loaded for this thread."""
    from ....tools import ALL_TOOLS, OPTIONAL_TOOLS

    tc = state.thread_config_manager.get_config(state.thread_id)
    disabled = set(tc.disabled_tools) if tc else set()
    enabled = set(tc.enabled_tools) if tc else set()

    tools = []
    for t in ALL_TOOLS:
        status = "disabled" if t.name in disabled else "enabled"
        tools.append({"name": t.name, "category": "core", "status": status})

    for name in sorted(enabled):
        if name in OPTIONAL_TOOLS:
            tools.append({"name": name, "category": "optional", "status": "enabled"})

    # Callable thread tools
    callable_threads = state.thread_config_manager.list_callable_threads()
    for ct in callable_threads:
        if ct.callable_name and ct.callable_name not in disabled:
            tools.append({"name": ct.callable_name, "category": "callable", "status": "enabled"})

    return tools


def _handle_tools(state: "CLIState", args: List[str]) -> None:
    """List tools loaded for the current thread."""
    tools = _get_loaded_tools(state)
    render_tools_table(state.console, tools)


def _handle_tools_enable(state: "CLIState", args: List[str]) -> None:
    """Enable an optional tool for this thread."""
    if not args:
        state.console.print("[red]Usage: /tools enable <name>[/red]")
        return

    tool_name = args[0]

    from ....tools import OPTIONAL_TOOLS, filter_developer_only_tools
    if tool_name not in OPTIONAL_TOOLS:
        state.console.print(f"[red]'{tool_name}' is not an optional tool.[/red]")
        state.console.print("[dim]Use /tools optional to see available tools.[/dim]")
        return
    _, blocked = filter_developer_only_tools([tool_name], _user_role(state))
    if blocked:
        state.console.print(f"[red]'{tool_name}' is developer-only.[/red]")
        return

    tc = state.thread_config_manager.get_config(state.thread_id)
    if tc is None:
        from ....core.thread_config import ThreadConfig
        tc = ThreadConfig(thread_id=state.thread_id)

    if tool_name not in tc.enabled_tools:
        tc.enabled_tools.append(tool_name)
    # Also remove from disabled if it was there
    if tool_name in tc.disabled_tools:
        tc.disabled_tools.remove(tool_name)

    state.thread_config_manager.save_config(tc)
    state.agent.invalidate_thread_config_cache(state.thread_id)
    state.console.print(f"[green]Enabled: {tool_name}[/green]")


def _handle_tools_disable(state: "CLIState", args: List[str]) -> None:
    """Disable a tool for this thread."""
    if not args:
        state.console.print("[red]Usage: /tools disable <name>[/red]")
        return

    tool_name = args[0]
    tc = state.thread_config_manager.get_config(state.thread_id)
    if tc is None:
        from ....core.thread_config import ThreadConfig
        tc = ThreadConfig(thread_id=state.thread_id)

    if tool_name not in tc.disabled_tools:
        tc.disabled_tools.append(tool_name)
    # Remove from enabled if it was there
    if tool_name in tc.enabled_tools:
        tc.enabled_tools.remove(tool_name)

    state.thread_config_manager.save_config(tc)
    state.agent.invalidate_thread_config_cache(state.thread_id)
    state.console.print(f"[green]Disabled: {tool_name}[/green]")


def _handle_tools_optional(state: "CLIState", args: List[str]) -> None:
    """List all optional tools with their status for this thread."""
    from ....tools import OPTIONAL_TOOLS, filter_discoverable_optional_tool_names

    tc = state.thread_config_manager.get_config(state.thread_id)
    enabled = set(tc.enabled_tools) if tc else set()
    visible_optional = filter_discoverable_optional_tool_names(
        OPTIONAL_TOOLS.keys(),
        _user_role(state),
    )

    tools = []
    for name in sorted(visible_optional):
        status = "enabled" if name in enabled else "disabled"
        tools.append({"name": name, "category": "optional", "status": status})

    render_tools_table(state.console, tools, title="Optional Tools")


def register(registry: CommandRegistry) -> None:
    """Register tool commands."""
    registry.register(Command(
        name="tools",
        aliases=[],
        description="List/manage tools",
        handler=_handle_tools,
        subcommands={
            "enable": Command(name="enable", aliases=[], description="Enable tool", handler=_handle_tools_enable),
            "disable": Command(name="disable", aliases=[], description="Disable tool", handler=_handle_tools_disable),
            "optional": Command(name="optional", aliases=[], description="List optional tools", handler=_handle_tools_optional),
        },
    ))
