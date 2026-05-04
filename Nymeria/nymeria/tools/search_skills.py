"""Agent-facing tools for discovering and installing Agent Skills.

These tools let Nymeria reason about skills as first-class resources:
- list_installed_skills: what's currently available on disk
- search_skills: search installed skills OR pull a fresh index from a marketplace
- install_skill: download a skill from a marketplace into user/global scope

The `Skill` meta-tool itself is *not* defined here; it's built dynamically
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
        required_tools, tool_ttl, is_skill_kit, default_active, has_scripts,
        has_references} entries.
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
            "required_tools": s.required_tools,
            "tool_ttl": s.tool_ttl,
            "is_skill_kit": s.is_skill_kit,
            "default_active": False,
            "has_scripts": s.has_scripts,
            "has_references": s.has_references,
        }
        for s in skills
    ]
    return json.dumps({"count": len(payload), "skills": payload}, indent=2)


_MARKETPLACE_INDEX_TTL_SECONDS = 15 * 60
_marketplace_indexed_at: dict[str, float] = {}


def _ensure_marketplace_indexed(agent, source: str) -> tuple[str, str | None]:
    """Refresh the marketplace namespace in the embedding index if stale.

    Returns (namespace, warning_or_none). On fetcher errors the warning is
    surfaced but we still return the namespace so any previously-indexed
    data can be searched.
    """
    import time as _time
    namespace = f"marketplace:{source}"
    index = getattr(agent.skill_manager, "embedding_index", None) if agent and agent.skill_manager else None
    if index is None:
        return namespace, "skills embedding index unavailable"

    now = _time.time()
    last = _marketplace_indexed_at.get(source, 0.0)
    if now - last < _MARKETPLACE_INDEX_TTL_SECONDS:
        return namespace, None

    try:
        from ..skills.marketplace import get_fetcher, MarketplaceError
        fetcher = get_fetcher(source)
        entries = fetcher.list(query=None)
    except NotImplementedError as e:
        return namespace, f"marketplace {source!r} not available: {e}"
    except MarketplaceError as e:
        return namespace, f"marketplace fetch failed: {e}"
    except Exception as e:
        logger.exception("marketplace list failed for %s", source)
        return namespace, f"marketplace fetch error: {type(e).__name__}: {e}"

    try:
        index.rebuild(namespace=namespace, items=entries)
        _marketplace_indexed_at[source] = now
    except Exception as e:
        logger.exception("marketplace index rebuild failed for %s", source)
        return namespace, f"index rebuild failed: {e}"
    return namespace, None


@tool
def search_skills(
    query: str,
    source: Literal["installed", "anthropic"] = "installed",
    top_k: int = 8,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search for Agent Skills by natural-language query.

    Uses semantic similarity (OpenAI embeddings) when available, falls back
    to keyword BM25 search, then substring matching. This means you can
    search by intent like "extract text from images" and find skills named
    `ocr-tool` even though your query doesn't contain "ocr".

    Args:
        query: Free-text search term. Names, descriptions, and intent all match.
        source: Where to search. "installed" (default) = the local disk pool;
            "anthropic" = github.com/anthropics/skills (for discovering skills
            you don't yet have installed).
        top_k: Max number of results to return (default 8).

    Returns:
        JSON string with ``{count, mode, results, warning?}``:
            - ``mode`` is "semantic" (best), "bm25" (keyword fallback), or
              "substring" (final safety net)
            - ``warning`` is only present when search is running in degraded
              mode. Surface its message to the user so they can set up a
              better configuration (typically set EMBEDDING_API_KEY).
            - each result is ``{name, description, score, ...}``
    """
    agent = _agent()
    if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
        return json.dumps({"error": "skills subsystem not initialized"})

    index = getattr(agent.skill_manager, "embedding_index", None)
    q = query or ""

    if source == "installed":
        if index is None:
            # Ultimate substring fallback — no index at all.
            user_id = get_user_id(config)
            q_lower = q.strip().lower()
            matches = []
            for s in agent.skill_manager.list_installed(user_id=user_id):
                if not q_lower or q_lower in s.name.lower() or q_lower in s.description.lower():
                    matches.append({
                        "name": s.name,
                        "description": s.description,
                        "scope": s.scope,
                        "score": 1.0,
                    })
            return json.dumps({
                "count": len(matches),
                "mode": "substring",
                "warning": "embedding index unavailable; using substring match",
                "results": matches[:top_k],
            }, indent=2)

        response = index.search(q, namespace="installed", top_k=top_k)
        out = response.to_json()
        # Annotate that results are drawn from the installed pool.
        for r in out["results"]:
            r.setdefault("source", "installed")
        return json.dumps(out, indent=2)

    if source == "anthropic":
        namespace, mp_warning = _ensure_marketplace_indexed(agent, "anthropic")
        if index is None:
            # Index unavailable — fall through to direct substring match against
            # the marketplace list.
            try:
                from ..skills.marketplace import get_fetcher, MarketplaceError
                fetcher = get_fetcher("anthropic")
                entries = fetcher.list(query=q or None)
            except (MarketplaceError, NotImplementedError) as e:
                return json.dumps({"error": str(e)})
            matches = [
                {"name": e.name, "description": e.description, "source": e.source, "score": 1.0}
                for e in entries
            ][:top_k]
            return json.dumps({
                "count": len(matches),
                "mode": "substring",
                "warning": "embedding index unavailable; using keyword match on marketplace list",
                "results": matches,
            }, indent=2)

        response = index.search(q, namespace=namespace, top_k=top_k)
        out = response.to_json()
        if mp_warning:
            # Preserve any earlier warning (e.g. from degraded semantic) by joining.
            existing = out.get("warning")
            out["warning"] = f"{existing}; {mp_warning}" if existing else mp_warning
        for r in out["results"]:
            r.setdefault("source", "anthropic")
        return json.dumps(out, indent=2)

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

    Installed skills are not automatically enabled on any thread. Use the
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

    # ``scope=user`` is per-user (every caller can install for themselves);
    # ``scope=global`` writes into the shared skills directory visible to all
    # users. Skill bundles can ship scripts the agent process can execute, so
    # global install is admin-only — same gate as POST /skills/install.
    if scope == "global":
        try:
            caller = agent.accounts_repo.get_user_by_id(user_id)
            if not caller or caller.role != "admin":
                return (
                    "[error] install_skill scope=global requires admin role. "
                    "Install with scope='user' (default) or ask the workspace "
                    "admin to install it globally."
                )
        except Exception as e:
            logger.warning("install_skill: admin check failed: %s", e)
            return f"[error] install_skill: caller verification failed: {e}"

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
    with agent._graph_cache_lock:
        agent._user_graphs.clear()
    try:
        agent._async_user_graphs.clear()
    except Exception:
        logger.debug("Failed to clear async graph cache")

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
