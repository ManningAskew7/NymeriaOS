"""Core toolset step: the seed tools your new threads start with, each keepable.

The rows are the REAL seed (``tool_seed.core_seed_tool_names``: ``SEED_TOOLS``
minus capability-expansion), all pre-checked. It is an editable per-user seed,
not a fixed set: finalize writes the kept core tools plus the family picks
that follow into the bootstrap admin's ``default_thread_tools`` (the Docker
shapes carry the same list in ``NYMERIA_INIT_DEFAULT_THREAD_TOOLS``), and the
operator changes it later from the desktop app or ``/tools``. The kept list
lives in ``state.extras["core_tools"]`` with the family steps' semantics: key
absent means the full seed (a non-interactive run, and any seed tool added
later); a present list is a decision (backlog #102).

Nothing is pinned on: no tool is ever force-bound (the modularity rule). The
two tools whose absence breaks a cross-thread contract say so in their row,
with the note every other disable surface appends. The ``Skill`` meta tool is
not a row: it is bound automatically whenever any skill is enabled and is not
part of the editable list. Per-tool approval toggles wait on security
profiles (#102 scope 3), which do not exist yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..nav import Step
from .base import Choice, multi_select_step

if TYPE_CHECKING:
    from ..state import WizardState

STEP_ID = "core_tools"

# One plain line per seed tool, written for the person installing, not for the
# model (the tools' own descriptions are the agent contract). A seed tool
# without a line here falls back to the first sentence of its description, so
# a tool promoted into the seed later never shows a blank row.
CORE_TOOL_NOTES: dict[str, str] = {
    "bash_execute": "Runs shell commands on the machine it is hosted on: the agent's main way to act.",
    "file_read": "Reads files, including images.",
    "file_write": "Creates or overwrites files.",
    "file_edit": "Makes exact, all-or-nothing edits to existing text files.",
    "file_list": "Lists directories (read-only).",
    "memory_add": "Adds to memory: the thread's notepad, or a global key.",
    "memory_edit": "Edits, clears or removes memory entries.",
    "memory_read": "Reads memory entries.",
    "rag_search": "Searches its own past conversations, saved memories and finished TODOs.",
    "nym_todo": "Creates and updates TODOs, including scheduled and recurring work.",
    "nym_todo_delete": "Deletes a TODO and cancels its schedule.",
    "nym_todo_list": "Lists TODOs.",
    "notify": "Sends you a notification through your configured channels.",
    "slash_command": (
        "Runs slash commands on its own thread as if it were you (`/tools`, "
        "`/model`, `/config`): the agent's self-management tool. Powerful, so "
        "gated: commands like `/stop`, `/clear` and `/restart` stay yours alone. "
        "Skills and kits do not need it; they activate through the `Skill` tool."
    ),
    "run_tools_in_order": "Runs a batch of tool calls one after another instead of all at once.",
    "tool_invoke": "Runs, once, a tool that is not bound to the thread, without adding it.",
    "spawn_thread": "Creates or deletes sub-threads with their own configuration, to orchestrate work.",
    "reply_to_thread": "Answers requests other threads send it.",
    "wait_for_reply": "Waits for, or checks on, the answer to a request it sent another thread.",
}


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    head, sep, _ = text.partition(". ")
    return f"{head}." if sep else text


def core_tool_description(name: str, fallback: str = "") -> str:
    """The row text for one seed tool: its curated line (else the first
    sentence of ``fallback``, the tool's own description), plus what stops
    working without it when the backend records a capability loss (the same
    note every disable surface appends, phrased for a row still checked)."""
    from ...tools.metadata import CAPABILITY_LOSS_NOTES

    text = CORE_TOOL_NOTES.get(name) or _first_sentence(fallback)
    loss = CAPABILITY_LOSS_NOTES.get(name)
    return f"{text} If unticked: {loss}." if loss else text


def core_tool_choices() -> list[Choice]:
    from ...tools import SEED_TOOLS
    from ..tool_seed import core_seed_tool_names

    own = {tool.name: tool.description or "" for tool in SEED_TOOLS}
    return [
        Choice(name, name, core_tool_description(name, own.get(name, "")))
        for name in core_seed_tool_names()
    ]


def _get_initial(state: "WizardState") -> list[str]:
    from ..tool_seed import kept_core_tool_names

    return kept_core_tool_names(state)


def _store(state: "WizardState", value: list[str]) -> None:
    state.extras[STEP_ID] = list(value)


CORE_TOOLS_NOTE = (
    "Your editable default set, not a fixed one: untick what new threads should "
    "not start with (not a block; a thread can still enable it). Change it later "
    "in the desktop app (Settings, Tools) or `/tools enable|disable <name> "
    "global`. Not listed: `Skill`, added automatically whenever a skill is enabled."
)


def make_core_tools_step() -> Step:
    """Multi-select over the real seed, every row pre-checked (checked = kept).

    Choices are built with the step list, like the family steps' catalogs."""
    return multi_select_step(
        step_id=STEP_ID,
        title="Core toolset",
        choices=core_tool_choices(),
        get_initial=_get_initial,
        store=_store,
        note=CORE_TOOLS_NOTE,
    )


__all__ = [
    "CORE_TOOL_NOTES",
    "CORE_TOOLS_NOTE",
    "STEP_ID",
    "core_tool_choices",
    "core_tool_description",
    "make_core_tools_step",
]
