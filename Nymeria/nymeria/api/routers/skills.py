"""Agent Skills routes."""

import logging
from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ...core.accounts import AuthenticatedUser
from ..schemas.skills import (
    GlobalSkillsUpdateRequest,
    SkillDetailResponse,
    SkillInstallRequest,
)

logger = logging.getLogger(__name__)


def create_skills_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[..., None],
) -> APIRouter:
    """Create the Agent Skills router with app dependencies injected."""
    router = APIRouter(tags=["Skills"])

    def _invalidate_graph_caches() -> None:
        # Canonical evict-and-rebuild: clears the per-thread graph caches under
        # the graph lock and recompiles the defaults so a skill change is
        # visible on the next turn. Best-effort: never fail the skill mutation
        # on a rebuild error.
        try:
            get_agent_fn()._rebuild_default_graphs()
        except Exception:
            logger.warning("Failed to rebuild graphs after skill change", exc_info=True)

    def _skill_to_metadata(skill) -> dict[str, Any]:
        return {
            "name": skill.name,
            "description": skill.description,
            "scope": skill.scope,
            "allowed_tools": skill.allowed_tools,
            "required_tools": skill.required_tools,
            "tool_ttl": skill.tool_ttl,
            "is_skill_kit": skill.is_skill_kit,
            "default_active": False,
            "has_scripts": skill.has_scripts,
            "has_references": skill.has_references,
            "has_assets": skill.has_assets,
        }

    @router.get("/skills")
    async def list_skills(
        user_id: str = Depends(authed_user_id),
        scope: Optional[str] = Query(None, description="Filter by scope: user/global/bundled"),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List all installed skills visible to *user_id*."""
        agent = get_agent_fn()
        if agent.skill_manager is None:
            return {"skills": [], "error": "skills subsystem unavailable"}
        skills = agent.skill_manager.list_installed(user_id=user_id)
        if scope:
            skills = [s for s in skills if s.scope == scope]
        return {"skills": [_skill_to_metadata(s) for s in skills]}

    @router.get("/skills/marketplace/search")
    async def search_marketplace(
        source: str = Query("anthropic"),
        q: Optional[str] = Query(None),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Search a remote marketplace for skills."""
        from ...skills.marketplace import MarketplaceError, get_fetcher

        try:
            fetcher = get_fetcher(source)
            entries = fetcher.list(query=q)
        except NotImplementedError as e:
            raise HTTPException(status_code=501, detail=str(e)) from e
        except MarketplaceError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        return {
            "source": source,
            "query": q,
            "results": [
                {
                    "name": e.name,
                    "description": e.description,
                    "source": e.source,
                    "repo_url": e.repo_url,
                }
                for e in entries
            ],
        }

    @router.get("/skills/{name}", response_model=SkillDetailResponse)
    async def get_skill(
        name: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return the full body + frontmatter of an installed skill."""
        agent = get_agent_fn()
        if agent.skill_manager is None:
            raise HTTPException(status_code=503, detail="skills subsystem unavailable")
        skill = agent.skill_manager.get(name, user_id=user_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        return {
            **_skill_to_metadata(skill),
            "body": skill.body,
            "path": str(skill.path),
            "license": skill.license,
            "scripts": skill.list_scripts(),
            "references": skill.list_references(),
        }

    @router.post("/skills/install")
    async def install_skill_endpoint(
        request: SkillInstallRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Install a skill from a marketplace into user or global scope.

        ``scope=user`` is per-user and any caller may install for themselves.
        ``scope=global`` writes into the shared skills directory visible to
        every user -- admin only, since a skill bundle can ship scripts that
        the agent process can execute.
        """
        agent = get_agent_fn()
        if agent.skill_manager is None:
            raise HTTPException(status_code=503, detail="skills subsystem unavailable")
        from ...skills.marketplace import MarketplaceError, get_fetcher

        if request.scope not in ("user", "global"):
            raise HTTPException(status_code=400, detail="scope must be 'user' or 'global'")
        if request.scope == "global" and user.role != "admin":
            raise HTTPException(status_code=403, detail="Global skill install requires admin")
        try:
            fetcher = get_fetcher(request.source)
        except NotImplementedError as e:
            raise HTTPException(status_code=501, detail=str(e)) from e
        except MarketplaceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        target_dir = agent.skill_manager.target_dir(
            request.scope,
            user_id=user_id if request.scope == "user" else None,
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            skill = fetcher.fetch(request.name, target_dir)
        except MarketplaceError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.exception("install_skill_endpoint failed")
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}") from e

        agent.skill_manager.reload()
        _invalidate_graph_caches()
        return {"status": "ok", "skill": _skill_to_metadata(skill), "path": str(skill.path)}

    @router.delete("/skills/{name}")
    async def uninstall_skill(
        name: str,
        scope: str = Query("user"),
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Remove an installed skill from disk."""
        agent = get_agent_fn()
        if agent.skill_manager is None:
            raise HTTPException(status_code=503, detail="skills subsystem unavailable")
        if scope not in ("user", "global"):
            raise HTTPException(status_code=400, detail="scope must be 'user' or 'global'")
        if scope == "global" and user.role != "admin":
            raise HTTPException(status_code=403, detail="Global skill uninstall requires admin")
        try:
            deleted = agent.skill_manager.uninstall(
                name,
                scope=scope,
                user_id=user_id if scope == "user" else None,
            )
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from e
        if not deleted:
            raise HTTPException(status_code=404, detail=f"skill not found in scope={scope}: {name}")

        # Also clean up any stale references to this skill name in profile/thread configs.
        try:
            profile = agent.profile_manager.get_profile(user_id)
            if name in getattr(profile, "enabled_global_skills", []):
                profile.enabled_global_skills = [
                    n for n in profile.enabled_global_skills if n != name
                ]
                agent.profile_manager.save_profile(profile)
        except Exception:
            logger.warning("Failed to clean up stale skill refs in profile", exc_info=True)

        _invalidate_graph_caches()
        return {"status": "ok", "deleted": name, "scope": scope}

    @router.get("/threads/{thread_id}/skills")
    async def get_thread_active_skills(
        thread_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Resolved set of skills active on this thread (after scope + overrides)."""
        require_thread_access_fn(user, thread_id, claim=False)
        agent = get_agent_fn()
        if agent.skill_manager is None:
            return {"skills": []}
        profile = agent.profile_manager.get_profile(user_id)
        tc = agent.thread_config_manager.get_config(thread_id)
        enabled_global = list(getattr(profile, "enabled_global_skills", []) or [])
        enabled_thread = list(tc.enabled_skills) if tc and tc.enabled_skills else []
        disabled_thread = list(tc.disabled_skills) if tc and tc.disabled_skills else []
        active = agent.skill_manager.list_for_thread(
            user_id=user_id,
            enabled_global_skills=enabled_global,
            thread_enabled_skills=enabled_thread,
            thread_disabled_skills=disabled_thread,
        )
        return {
            "thread_id": thread_id,
            "default_enabled": [],
            "enabled_global": enabled_global,
            "thread_enabled": enabled_thread,
            "thread_disabled": disabled_thread,
            "skills": [_skill_to_metadata(s) for s in active],
        }

    @router.get("/settings/global-skills")
    async def get_global_skills(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Which skills are enabled-by-default for every new thread."""
        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        return {"enabled_global_skills": list(getattr(profile, "enabled_global_skills", []) or [])}

    @router.put("/settings/global-skills")
    async def set_global_skills(
        request: GlobalSkillsUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Replace the user's enabled-by-default skill list."""
        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        profile.enabled_global_skills = list(request.skill_names)
        agent.profile_manager.save_profile(profile)
        _invalidate_graph_caches()
        return {"enabled_global_skills": profile.enabled_global_skills}

    return router
