"""Core toolset step (informational): the literal tools every thread receives.

Section A of `docs/private/core-toolset-plan.md`: these tools ship in every
default thread and are not chosen at init. This screen shows them for
orientation; the following family steps are where the user seeds the init-chosen
tools on top. Wiring init selections into the per-user `default_thread_tools` is
separate future work, so nothing is stored here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from textual.widgets import Static

from ..nav import Step
from .base import WizardStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp


# Section A target set (12). Notes call out the few that are not self-evident.
CORE_TOOLS: tuple[tuple[str, str], ...] = (
    ("bash_execute", ""),
    ("file_read", ""),
    ("file_write", ""),
    ("memory_add", ""),
    ("memory_edit", ""),
    ("memory_read", ""),
    ("nym_todo", "scheduled-task tools"),
    ("nym_todo_delete", ""),
    ("nym_todo_list", ""),
    ("notify", ""),
    ("slash_command", "runs /kit and /skill"),
    ("spawn_thread", "orchestrate sub-threads"),
)


def _core_tools_markup() -> str:
    lines = [
        "[bold]Planned core toolset[/bold] (the target default for every thread):",
        "",
    ]
    for name, note in CORE_TOOLS:
        suffix = f"  [#8a93a3]{note}[/#8a93a3]" if note else ""
        lines.append(f"  [#bbddfb]{name}[/#bbddfb]{suffix}")
    lines.append("")
    lines.append(
        "Next you pick which web search, web fetch, RAG, and image tools to add "
        "on top. Init does not write these into your defaults yet."
    )
    return "\n".join(lines)


class CoreToolsStep(WizardStep):
    """Informational: the always-on core tools, before the family pickers."""

    def compose_body(self) -> ComposeResult:
        yield Static(_core_tools_markup())

    def collect(self) -> bool:
        # Core tools are seeded automatically; nothing to record here.
        return True


def make_core_tools_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> CoreToolsStep:
        return CoreToolsStep(
            wizard,
            number,
            total,
            step_id="core_tools",
            title="Core toolset",
            note=(
                "The planned default tools for every thread. The next steps add "
                "optional tool families on top."
            ),
            hint="enter next   esc back   ctrl+q quit",
        )

    return Step(id="core_tools", applies=lambda _state: True, build=build)


__all__ = ["make_core_tools_step", "CoreToolsStep", "CORE_TOOLS"]
