"""Agent-facing tools for discovering and installing Agent Skills.

These tools let Nymeria reason about skills as first-class resources:
- list_installed_skills: what's currently available on disk
- search_skills: search installed skills OR pull a fresh index from a marketplace
- install_skill: download a skill from a marketplace into user/global scope

The `Skill` meta-tool itself is *not* defined here — it's built dynamically
per-graph in skills.meta_tool.create_skill_meta_tool so its description can
carry the (name, description) index for the current thread's active skills.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, List, Literal, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_user_id

logger = logging.getLogger(__name__)


def _agent():
    """Lazy import to avoid a tool->agent circular import at module load."""
    from ..core.agent import get_current_agent
    return get_current_agent()


@tool
def list_installed_skills(
    scope: Literal["all", "user", "global", "bundled"] = "all",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List all Agent Skills currently installed and visible to the user.

    A skill is a bundle of procedural knowledge (SKILL.md + optional scripts/
    references/assets). Installed skills are available to be enabled on
    threads; enabled skills show up in the `Skill` meta-tool's index.

    Args:
        scope: Filter by install location. "all" (default) returns every
            skill; "user" is skills installed privately for the current user;
            "global" is admin-installed skills visible to everyone;
            "bundled" is repo-shipped skills.

    Returns:
        JSON string with a list of {name, description, scope, allowed_tools,
        has_scripts, has_references} entries.
    """
    agent = _agent()
    if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
        return json.dumps({"error": "skills subsystem not initialized"})

    user_id = get_user_id(config)
    skills = agent.skill_manager.list_installed(user_id=user_id)
    if scope != "all":
        skills = [s for s in skills if s.scope == scope]

    payload = [
        {
            "name": s.name,
            "description": s.description,
            "scope": s.scope,
            "allowed_tools": s.allowed_tools,
            "has_scripts": s.has_scripts,
            "has_references": s.has_references,
        }
        for s in skills
    ]
    return json.dumps({"count": len(payload), "skills": payload}, indent=2)


@tool
def search_skills(
    query: str,
    source: Literal["installed", "anthropic"] = "installed",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search for Agent Skills by keyword, either in the local installed pool
    or in a remote marketplace.

    Args:
        query: Free-text search term matched against skill names and
            descriptions (case-insensitive substring).
        source: Where to search. "installed" (default) hits the local disk;
            "anthropic" hits github.com/anthropics/skills for skills you
            don't yet have installed.

    Returns:
        JSON string with a list of matches. Each match includes
        {name, description, source}.
    """
    agent = _agent()
    q = (query or "").strip().lower()

    if source == "installed":
        if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
            return json.dumps({"error": "skills subsystem not initialized"})
        user_id = get_user_id(config)
        matches = []
        for s in agent.skill_manager.list_installed(user_id=user_id):
            if not q or q in s.name.lower() or q in s.description.lower():
                matches.append({
                    "name": s.name,
                    "description": s.description,
                    "scope": s.scope,
                    "source": "installed",
                })
        return json.dumps({"count": len(matches), "results": matches}, indent=2)

    if source == "anthropic":
        try:
            from ..skills.marketplace import get_fetcher, MarketplaceError
            fetcher = get_fetcher("anthropic")
            entries = fetcher.list(query=q or None)
        except MarketplaceError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            logger.exception("anthropic skills search failed")
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

        matches = [
            {"name": e.name, "description": e.description, "source": e.source}
            for e in entries
        ]
        return json.dumps({"count": len(matches), "results": matches}, indent=2)

    return json.dumps({"error": f"unknown source: {source!r}"})


@tool
def install_skill(
    name: str,
    source: Literal["anthropic"] = "anthropic",
    scope: Literal["user", "global"] = "user",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Install an Agent Skill from a marketplace onto disk.

    Installed skills are not automatically enabled on any thread — use the
    desktop Settings → Skills tab, or the skill-creator skill's enable
    workflow, to turn them on.

    Args:
        name: The skill's kebab-case name (as returned by search_skills).
        source: Which marketplace to pull from. Only "anthropic" (the
            official anthropics/skills GitHub repo) is supported in Phase 1.
        scope: Where to store it on disk. "user" (default) installs privately
            for the current user; "global" makes it visible to all users.

    Returns:
        Human-readable summary of the install. On failure, returns an error
        message starting with "[error]".
    """
    agent = _agent()
    if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
        return "[error] skills subsystem not initialized"

    user_id = get_user_id(config)

    try:
        from ..skills.marketplace import get_fetcher, MarketplaceError
        fetcher = get_fetcher(source)
    except MarketplaceError as e:
        return f"[error] {e}"
    except NotImplementedError as e:
        return f"[error] {e}"

    target_dir = agent.skill_manager.target_dir(
        scope, user_id=user_id if scope == "user" else None
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        skill = fetcher.fetch(name, target_dir)
    except MarketplaceError as e:
        return f"[error] {e}"
    except Exception as e:
        logger.exception("install_skill failed")
        return f"[error] {type(e).__name__}: {e}"

    agent.skill_manager.reload()

    details: List[str] = [
        f"Installed skill {skill.name!r} from {source} into {scope} scope.",
        f"Location: {skill.path}",
        f"Description: {skill.description}",
    ]
    if skill.allowed_tools:
        details.append("Declared allowed-tools: " + ", ".join(skill.allowed_tools))
    if skill.has_scripts:
        details.append(f"Ships {len(skill.list_scripts())} scripts.")
    if skill.has_references:
        details.append(f"Ships {len(skill.list_references())} reference files.")
    details.append(
        "To make this skill available on a thread, enable it via the Skills "
        "settings (or add its name to ThreadConfig.enabled_skills)."
    )
    return "\n".join(details)


SEARCH_SKILLS_TOOLS = [list_installed_skills, search_skills, install_skill]

__all__ = [
    "list_installed_skills",
    "search_skills",
    "install_skill",
    "SEARCH_SKILLS_TOOLS",
]
