"""Model commands: /model, /model set."""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from . import Command, CommandRegistry

if TYPE_CHECKING:
    from ..state import CLIState


def _handle_model(state: "CLIState", args: List[str]) -> None:
    """Show the effective model for the current thread."""
    global_model = state.settings.llm_model
    effective = state.get_effective_model()

    tc = state.thread_config_manager.get_config(state.thread_id)
    override = (tc.llm_config.model if tc and tc.llm_config and tc.llm_config.model else None)

    state.console.print(f"  [dim]Global:[/dim]   {global_model}")
    state.console.print(f"  [dim]Effective:[/dim] {effective}")
    if override:
        state.console.print(f"  [dim]Override:[/dim] [cyan]{override}[/cyan]")
    else:
        state.console.print("  [dim]Override:[/dim] [dim]None (using global)[/dim]")


def _handle_model_set(state: "CLIState", args: List[str]) -> None:
    """Set a per-thread model override."""
    if not args:
        state.console.print("[red]Usage: /model set <model-id>[/red]")
        return

    model_id = args[0]
    tc = state.thread_config_manager.get_config(state.thread_id)

    if tc is None:
        from ....core.thread_config import ThreadConfig, ThreadLLMConfig
        tc = ThreadConfig(
            thread_id=state.thread_id,
            llm_config=ThreadLLMConfig(model=model_id),
        )
    else:
        if tc.llm_config is None:
            from ....core.thread_config import ThreadLLMConfig
            tc.llm_config = ThreadLLMConfig(model=model_id)
        else:
            tc.llm_config.model = model_id

    state.thread_config_manager.save_config(tc)
    state.agent.invalidate_thread_config_cache(state.thread_id)
    state.console.print(f"[green]Model set to: {model_id}[/green]")


def register(registry: CommandRegistry) -> None:
    """Register model commands."""
    registry.register(Command(
        name="model",
        aliases=[],
        description="Show/set model",
        handler=_handle_model,
        subcommands={
            "set": Command(name="set", aliases=[], description="Set model", handler=_handle_model_set),
        },
    ))
