"""What `nymeria init` writes into the bootstrap admin's default tool set.

TUI-free single source of truth shared by the core-tools screen (what it
displays), the review screen (what it summarizes), and finalize (what it writes
to ``data/users/default/profile.json``). The core seed mirrors the backend's own
``_migrate_tool_preferences`` (``core/agent.py``): all of ``SEED_TOOLS`` minus the
capability-expansion tools. We seed from the real ``SEED_TOOLS`` rather than the
aspirational 12-tool core in ``steps/core_tools.py`` so init never silently drops
tools that exist today (the core-slimming is unbuilt).

Optional tools (``web_search_*`` / ``image_gen_*``) are NOT in ``SEED_TOOLS``, so
to make a new thread inherit a picked backend by default it must be written into
an explicit ``default_thread_tools`` list: core seed plus the user's picks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state import WizardState


def core_seed_tool_names() -> list[str]:
    """The seed tools every new thread's default set starts with.

    Mirrors ``NymeriaAgent._migrate_tool_preferences`` (``core/agent.py``) so an
    admin seeded at init matches a normally-migrated user for the core portion.
    Imported lazily to keep the setup package importable without the heavy tools
    package (the wizard chrome must load fast).
    """
    from ..tools import SEED_TOOLS, CAPABILITY_EXPANSION_TOOL_NAMES

    return [t.name for t in SEED_TOOLS if t.name not in CAPABILITY_EXPANSION_TOOL_NAMES]


def default_thread_tools_for_state(state: "WizardState") -> list[str]:
    """Explicit ``default_thread_tools`` to seed: core plus picked family members.

    Order-preserving dedup, core first then the init-chosen ``web_search_*`` /
    ``fetch_url_*`` / ``image_gen_*`` names.
    """
    from .steps.placeholders import seeded_tool_names

    names = list(core_seed_tool_names())
    for name in seeded_tool_names(state):
        if name not in names:
            names.append(name)
    return names


def selected_global_skills_for_state(state: "WizardState") -> list[str]:
    """``enabled_global_skills`` to seed: the self-improve guidance skill plus the
    init-chosen capability kits.

    ``self-improve`` is always included (the text-only routing/guidance skill that
    the backend also defaults on); the chosen ``*-management`` kits follow.
    Order-preserving dedup.
    """
    from .steps.placeholders import seeded_global_skills

    names = ["self-improve"]
    for name in seeded_global_skills(state):
        if name not in names:
            names.append(name)
    return names


__all__ = [
    "core_seed_tool_names",
    "default_thread_tools_for_state",
    "selected_global_skills_for_state",
]
