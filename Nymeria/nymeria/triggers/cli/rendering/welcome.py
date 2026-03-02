"""Welcome panel and status header for the CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from ..state import CLIState


def render_welcome(state: "CLIState") -> None:
    """Print the startup welcome panel."""
    model = state.get_effective_model()
    # Shorten long model IDs for display
    model_display = model.split("/")[-1] if "/" in model else model

    thread_display = state.get_thread_title()

    body = Text()
    body.append("Nymeria\n", style="bold blue")
    body.append("\n")
    body.append("  Model   ", style="dim")
    body.append(f"{model_display}\n", style="white")
    body.append("  Thread  ", style="dim")
    body.append(f"{thread_display}\n", style="white")
    body.append("\n")
    body.append("  /help", style="dim cyan")
    body.append(" for commands  ", style="dim")
    body.append("/threads", style="dim cyan")
    body.append(" to switch", style="dim")

    state.console.print()
    state.console.print(Panel(body, border_style="blue", padding=(0, 2)))
    state.console.print()
