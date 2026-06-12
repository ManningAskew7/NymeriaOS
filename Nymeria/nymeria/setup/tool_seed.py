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
    # Reconfigure: re-append any user-added tools captured during hydration so a
    # profile update never silently drops tools that are neither core nor a known
    # init family member. Empty on first-run.
    for name in getattr(state, "unmanaged_tools", []) or []:
        if name not in names:
            names.append(name)
    return names


def docker_init_seed_env(state: "WizardState") -> dict[str, str]:
    """Env vars that carry the bootstrap admin's picks into a Docker container.

    The Docker single-container shape owns its `/data` volume, so finalize cannot
    seed the bootstrap profile on the host the way local/service hosting does
    (`finalize.seed_bootstrap_profile`). Instead the picks ride in `.env.docker`
    as two name-list env vars the container reads once on first boot (see
    ``config/init_seed_env.py`` for the contract and the container-side readers).

    Returns ONLY the vars that DIFFER from the backend's own first-boot defaults
    (``core_seed_tool_names()`` for tools, ``DEFAULT_GLOBAL_SKILLS`` for skills),
    so the container's normal core-seed and default-skill migrations run
    unchanged where they agree. In practice the skills carrier is now always
    written: the wizard's curated six-kit default
    (``default_checked_skill_kits``, widened 2026-06-12) deliberately differs
    from the narrower backend fallback, and the carrier is the only way it
    reaches the container's volume. When a var is written it holds the exact
    list `seed_bootstrap_profile` would have written on the host, keeping the
    two seeding paths in agreement.
    """
    from ..config.init_seed_env import (
        INIT_DEFAULT_THREAD_TOOLS_ENV,
        INIT_ENABLED_GLOBAL_SKILLS_ENV,
        format_init_name_list,
    )
    from ..core.user_profile import DEFAULT_GLOBAL_SKILLS

    out: dict[str, str] = {}
    tools = default_thread_tools_for_state(state)
    if tools != core_seed_tool_names():
        out[INIT_DEFAULT_THREAD_TOOLS_ENV] = format_init_name_list(tools)
    skills = selected_global_skills_for_state(state)
    if skills != DEFAULT_GLOBAL_SKILLS:
        out[INIT_ENABLED_GLOBAL_SKILLS_ENV] = format_init_name_list(skills)
    return out


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
    "docker_init_seed_env",
]
