"""Core toolset step (informational): the always-on tools every thread receives.

These tools ship in every default thread and are not chosen at init. This screen
shows them for orientation; the following family steps seed the init-chosen tools
on top, and finalize writes the whole set (core plus picks) into the bootstrap
admin's `default_thread_tools`. The displayed list is the REAL seed
(`tool_seed.core_seed_tool_names`, i.e. `SEED_TOOLS` minus capability-expansion),
which is what finalize writes; `CORE_TOOLSET_TARGET` below is the aspirational 12-tool
target from `docs/private/core-toolset-plan.md` Section A (core-slimming unbuilt).
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
CORE_TOOLSET_TARGET: tuple[tuple[str, str], ...] = (
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
    from ..tool_seed import core_seed_tool_names

    notes = dict(CORE_TOOLSET_TARGET)
    lines = [
        "[bold]Core toolset[/bold] (always on for every new thread):",
        "",
    ]
    for name in core_seed_tool_names():
        note = notes.get(name, "")
        suffix = f"  [#8a93a3]{note}[/#8a93a3]" if note else ""
        lines.append(f"  [#bbddfb]{name}[/#bbddfb]{suffix}")
    lines.append("")
    lines.append(
        "Next you pick which web search, web fetch, RAG, and image tools to add "
        "on top. These core tools plus your picks become your default toolset."
    )
    return "\n".join(lines)


class CoreToolsStep(WizardStep):
    """Informational: the always-on core tools, before the family pickers."""

    def compose_body(self) -> ComposeResult:
        yield Static(_core_tools_markup())

    def collect(self) -> bool:
        # Core tools are seeded automatically by finalize; nothing to record here.
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
                "The always-on tools every thread receives. The next steps add "
                "optional tool families on top of these."
            ),
            hint="enter next   esc back   ctrl+q quit",
        )

    return Step(id="core_tools", applies=lambda _state: True, build=build)


__all__ = ["make_core_tools_step", "CoreToolsStep", "CORE_TOOLSET_TARGET"]
