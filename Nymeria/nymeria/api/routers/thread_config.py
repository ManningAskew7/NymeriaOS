"""Thread configuration and callable-team routes."""

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...core.accounts import AuthenticatedUser
from ...core.thread_config import DreamingConfig, ThreadConfig, ThreadLLMConfig
from ..schemas.thread_config import (
    NotepadUpdateRequest,
    ThreadConfigUpdateRequest,
    ThreadTeamCreateRequest,
    ThreadTeamUpdateRequest,
)
from ..thread_config_helpers import (
    make_thread_team_id,
    normalize_thread_team_name,
    validate_callable_name,
)

_SubmodelT = TypeVar("_SubmodelT", bound=BaseModel)


def _merge_optional_submodel(
    existing: _SubmodelT | None,
    update_request: BaseModel,
    ctor: Callable[..., _SubmodelT],
) -> _SubmodelT:
    """Apply a partial-update request onto an optional nested submodel.

    The ``llm_config`` and ``dreaming`` thread-config fields are nested pydantic
    models that callers patch field-by-field. When the stored field is unset a
    fresh model is built from only the request's set fields, dropping explicit
    ``None`` values so untouched optional fields keep their model defaults. When
    the field already exists every set field (including an explicit ``None``) is
    assigned onto it, so a caller can null a single nested field without losing
    the rest. This preserves the exact clear-vs-set precedence the two merge
    blocks relied on inline.
    """
    data = update_request.model_dump(exclude_unset=True)
    if existing is None:
        return ctor(**{key: value for key, value in data.items() if value is not None})
    for key, value in data.items():
        setattr(existing, key, value)
    return existing


def _config_response(config: ThreadConfig) -> dict[str, Any]:
    result = config.model_dump(mode="json")
    result["has_customizations"] = config.has_customizations()
    return result


def _default_thread_config_response(thread_id: str) -> dict[str, Any]:
    """Return the default empty-config payload for an unconfigured thread.

    Built from the ``ThreadConfig`` model so it can never drift from the
    saved-config shape ``_config_response`` returns. A hand-maintained dict
    here had already fallen behind, dropping ``temporary_tools``,
    ``enabled_skills``, ``disabled_skills``, and ``inject_profile_in_prompt``,
    so a fresh thread's config payload was a different shape than it became
    after the first save. ``created_at``/``updated_at`` are nulled because no
    config has actually been persisted for this thread yet.
    """
    response = _config_response(ThreadConfig(thread_id=thread_id))
    response["created_at"] = None
    response["updated_at"] = None
    return response


def _serialize_thread_teams(agent: Any, user_id: str) -> dict[str, Any]:
    owned = list(agent.accounts_repo.list_threads_for_user(user_id))
    teams: dict[str, dict[str, Any]] = {}
    for thread_id in owned:
        tc = agent.thread_config_manager.get_config(thread_id)
        if not (tc and tc.callable_team_id):
            continue
        team_id = tc.callable_team_id
        team = teams.setdefault(
            team_id,
            {
                "id": team_id,
                "name": tc.callable_team_name or team_id,
                "thread_ids": [],
            },
        )
        team["thread_ids"].append(thread_id)
        if tc.callable_team_name:
            team["name"] = tc.callable_team_name

    result = sorted(teams.values(), key=lambda t: str(t["name"]).lower())
    return {"teams": result, "total": len(result)}


def _find_team(teams: list[dict[str, Any]], team_id: str) -> dict[str, Any] | None:
    """Return the team with ``team_id`` from an already-serialized team list.

    Pure over the pre-computed list so a handler can scan the config store once
    (``_serialize_thread_teams`` is an O(N) uncached read over every owned
    thread's config) and reuse the result for several lookups.
    """
    for team in teams:
        if team["id"] == team_id:
            return team
    return None


def _team_name_taken(
    teams: list[dict[str, Any]],
    name: str,
    *,
    excluding_team_id: str | None = None,
) -> bool:
    """Report whether ``name`` collides with a team in the serialized list.

    Case-insensitive over the trimmed name; ``excluding_team_id`` lets a rename
    ignore the team being edited. Pure over the pre-computed team list.
    """
    needle = name.strip().lower()
    for team in teams:
        if excluding_team_id and team["id"] == excluding_team_id:
            continue
        if str(team["name"]).strip().lower() == needle:
            return True
    return False


def _require_team_thread_ids(
    user: AuthenticatedUser,
    thread_ids: list[str],
    require_thread_access_fn: Callable[..., None],
) -> list[str]:
    seen: set[str] = set()
    clean: list[str] = []
    for raw_id in thread_ids:
        thread_id = str(raw_id or "").strip()
        if not thread_id or thread_id in seen:
            continue
        require_thread_access_fn(user, thread_id)
        clean.append(thread_id)
        seen.add(thread_id)
    if not clean:
        raise HTTPException(status_code=400, detail="At least one thread is required")
    return clean


def _save_thread_team_membership(
    agent: Any,
    thread_id: str,
    *,
    team_id: str | None,
    team_name: str | None,
) -> None:
    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)
    tc.callable_team_id = team_id
    tc.callable_team_name = team_name
    if not agent.thread_config_manager.save_config(tc):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save team membership for thread {thread_id}",
        )


def _invalidate_user_team_graphs(agent: Any, user_id: str) -> None:
    for owned_thread_id in agent.accounts_repo.list_threads_for_user(user_id):
        agent.invalidate_thread_config_cache(owned_thread_id)
    # Also clear per-user no-custom sentinel graphs, whose key uses "".
    agent.invalidate_thread_config_cache("")


def create_thread_config_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[..., None],
) -> APIRouter:
    """Create the thread configuration router with app dependencies injected."""
    router = APIRouter(tags=["Threads"])

    @router.get("/threads/{thread_id}/config")
    async def get_thread_config(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get per-thread configuration (returns defaults if none saved)."""
        require_thread_access_fn(user, thread_id, claim=False)
        agent = get_agent_fn()
        tc = agent.thread_config_manager.get_config(thread_id)
        if tc:
            return _config_response(tc)
        return _default_thread_config_response(thread_id)

    @router.patch("/threads/{thread_id}/config")
    async def update_thread_config(
        thread_id: str,
        request: ThreadConfigUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Update per-thread configuration (partial update)."""
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        tc = agent.thread_config_manager.get_config(thread_id)

        if tc is None:
            tc = ThreadConfig(thread_id=thread_id)

        if request.clear_instructions:
            tc.instructions = None
        if request.clear_disabled_tools:
            tc.disabled_tools = []
        if request.clear_enabled_tools:
            tc.enabled_tools = []
        if request.clear_enabled_skills:
            tc.enabled_skills = []
        if request.clear_disabled_skills:
            tc.disabled_skills = []
        if request.clear_llm_config:
            tc.llm_config = None
            tc.active_llm_fallback = None
        if request.clear_system_prompt:
            tc.system_prompt = None

        if request.instructions is not None and not request.clear_instructions:
            tc.instructions = request.instructions
        if request.disabled_tools is not None and not request.clear_disabled_tools:
            tc.disabled_tools = request.disabled_tools
        if request.enabled_tools is not None and not request.clear_enabled_tools:
            if user.role != "admin":
                from ...tools import (
                    ADMIN_ONLY_TOOL_NAMES,
                    DEVELOPER_ONLY_TOOL_NAMES,
                )

                blocked = ADMIN_ONLY_TOOL_NAMES.intersection(
                    request.enabled_tools
                )
                if blocked:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "Admin-only tools cannot be enabled by this user: "
                            f"{sorted(blocked)}"
                        ),
                    )
                blocked = DEVELOPER_ONLY_TOOL_NAMES.intersection(
                    request.enabled_tools
                )
                if blocked:
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "Developer-only diagnostic tools cannot be enabled "
                            f"by this user: {sorted(blocked)}"
                        ),
                    )
            tc.enabled_tools = request.enabled_tools
        if request.enabled_skills is not None and not request.clear_enabled_skills:
            tc.enabled_skills = request.enabled_skills
        if request.disabled_skills is not None and not request.clear_disabled_skills:
            tc.disabled_skills = request.disabled_skills
        if request.llm_config is not None and not request.clear_llm_config:
            tc.active_llm_fallback = None
            tc.llm_config = _merge_optional_submodel(
                tc.llm_config, request.llm_config, ThreadLLMConfig
            )
        if request.system_prompt is not None and not request.clear_system_prompt:
            tc.system_prompt = request.system_prompt
        if request.callable is not None:
            tc.callable = request.callable
            if request.callable and not (request.callable_name or tc.callable_name):
                raise HTTPException(
                    status_code=400,
                    detail="callable_name is required when enabling callable",
                )
        if request.callable_name is not None:
            if request.callable_name:
                validate_callable_name(request.callable_name)
                from ...tools import SEED_TOOLS

                core_tool_names = {t.name for t in SEED_TOOLS}
                if request.callable_name in core_tool_names:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Callable name '{request.callable_name}' "
                            "conflicts with a core tool name"
                        ),
                    )
                owned = set(agent.accounts_repo.list_threads_for_user(user_id))
                existing = agent.thread_config_manager.get_callable_thread_by_name(
                    request.callable_name,
                    owned_thread_ids=owned,
                )
                if existing and existing.thread_id != thread_id:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Callable name '{request.callable_name}' is already "
                            f"used by thread {existing.thread_id}"
                        ),
                    )
            tc.callable_name = request.callable_name
        if request.callable_description is not None:
            tc.callable_description = request.callable_description
        if request.callable_max_iterations is not None:
            tc.callable_max_iterations = request.callable_max_iterations
        if request.callable_team_id is not None:
            tc.callable_team_id = request.callable_team_id
        if request.callable_team_name is not None:
            tc.callable_team_name = request.callable_team_name
        if request.inject_todos_in_prompt is not None:
            tc.inject_todos_in_prompt = request.inject_todos_in_prompt
        if request.show_autonomous_prompts is not None:
            tc.show_autonomous_prompts = request.show_autonomous_prompts
        if request.show_prompt_metadata is not None:
            tc.show_prompt_metadata = request.show_prompt_metadata
        if request.telegram_autonomous_delivery is not None:
            tc.telegram_autonomous_delivery = request.telegram_autonomous_delivery
        if request.in_app_notification_level is not None:
            tc.in_app_notification_level = request.in_app_notification_level
        if request.clear_notification_profile:
            tc.notification_profile = None
        elif request.notification_profile is not None:
            tc.notification_profile = request.notification_profile or None
        if request.clear_memory_char_limit:
            tc.memory_char_limit = None
        elif request.memory_char_limit is not None:
            tc.memory_char_limit = request.memory_char_limit
        if request.clear_image_window_size:
            tc.image_window_size = None
        elif request.image_window_size is not None:
            tc.image_window_size = request.image_window_size
        if request.clear_sequential_tool_execution:
            tc.sequential_tool_execution = None
        elif request.sequential_tool_execution is not None:
            tc.sequential_tool_execution = request.sequential_tool_execution
        if request.clear_claude_code_model:
            tc.claude_code_model = None
        elif request.claude_code_model is not None:
            tc.claude_code_model = request.claude_code_model or None
        if request.clear_claude_code_mode:
            tc.claude_code_mode = None
        elif request.claude_code_mode is not None:
            tc.claude_code_mode = request.claude_code_mode or None
        if request.clear_dreaming:
            tc.dreaming = None
        elif request.dreaming is not None:
            tc.dreaming = _merge_optional_submodel(
                tc.dreaming, request.dreaming, DreamingConfig
            )

        if not agent.thread_config_manager.save_config(tc):
            raise HTTPException(status_code=500, detail="Failed to save thread config")

        agent.invalidate_thread_config_cache(thread_id)
        if request.callable_team_id is not None or request.callable_team_name is not None:
            for owned_thread_id in agent.accounts_repo.list_threads_for_user(user_id):
                agent.invalidate_thread_config_cache(owned_thread_id)
            agent.invalidate_thread_config_cache("")

        if (
            request.callable is not None
            or request.callable_name is not None
            or request.callable_description is not None
            or request.callable_max_iterations is not None
        ):
            agent.sync_agent_tools()
        elif request.callable_team_id is not None or request.callable_team_name is not None:
            from ...core.tool_search_index import mark_tool_search_dirty

            mark_tool_search_dirty()

        if tc.callable and tc.callable_name:
            agent.thread_metadata_manager.upsert_thread(
                user_id,
                thread_id,
                title=tc.callable_name,
                title_source="callable",
                platform="callable",
            )

        return _config_response(tc)

    @router.delete("/threads/{thread_id}/config")
    async def delete_thread_config(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Reset thread to global defaults (delete custom config)."""
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        tc = agent.thread_config_manager.get_config(thread_id)
        was_agent = tc.callable if tc else False
        agent.thread_config_manager.delete_config(thread_id)
        agent.invalidate_thread_config_cache(thread_id)
        if was_agent:
            agent.sync_agent_tools()
        else:
            from ...core.tool_search_index import mark_tool_search_dirty

            mark_tool_search_dirty()
        return {"status": "ok", "thread_id": thread_id}

    @router.get("/threads/{thread_id}/notepad")
    async def get_thread_notepad(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Read a thread's persistent notepad (the agent's thread memory).

        This is the same markdown store the agent edits via memory tools and the
        /notepad slash command; here it is exposed for direct editing in the UI.
        """
        require_thread_access_fn(user, thread_id, claim=False)
        from ...core.memory_limits import get_effective_thread_memory_char_limit
        from ...tools import thread_notes

        content = thread_notes.read_notepad(thread_id) or ""
        return {
            "thread_id": thread_id,
            "content": content,
            "char_count": len(content),
            "char_limit": get_effective_thread_memory_char_limit(thread_id),
        }

    @router.put("/threads/{thread_id}/notepad")
    async def update_thread_notepad(
        thread_id: str,
        request: NotepadUpdateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Replace a thread's notepad content (blank clears it).

        Enforces the per-thread/global memory char limit via write_notepad; a
        limit violation surfaces as HTTP 400.
        """
        require_thread_access_fn(user, thread_id)
        from ...core.memory_limits import get_effective_thread_memory_char_limit
        from ...tools import thread_notes

        result = thread_notes.write_notepad(thread_id, request.content, mode="replace")
        if result.startswith("[Error]"):
            raise HTTPException(status_code=400, detail=result)

        content = thread_notes.read_notepad(thread_id) or ""
        return {
            "thread_id": thread_id,
            "content": content,
            "char_count": len(content),
            "char_limit": get_effective_thread_memory_char_limit(thread_id),
            "message": result,
        }

    @router.get("/thread-teams")
    async def list_thread_teams(user_id: str = Depends(authed_user_id)):
        """List callable visibility teams for the authenticated user's threads."""
        agent = get_agent_fn()
        return _serialize_thread_teams(agent, user_id)

    @router.post("/thread-teams")
    async def create_thread_team(
        request: ThreadTeamCreateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Create a callable team and move the requested threads into it."""
        agent = get_agent_fn()
        name = normalize_thread_team_name(request.name)
        teams = _serialize_thread_teams(agent, user_id)["teams"]
        if _team_name_taken(teams, name):
            raise HTTPException(
                status_code=409,
                detail=f"Thread team '{name}' already exists",
            )
        thread_ids = _require_team_thread_ids(
            user,
            request.thread_ids,
            require_thread_access_fn,
        )
        team_id = make_thread_team_id(name)
        for thread_id in thread_ids:
            _save_thread_team_membership(
                agent,
                thread_id,
                team_id=team_id,
                team_name=name,
            )
        _invalidate_user_team_graphs(agent, user_id)
        from ...core.tool_search_index import mark_tool_search_dirty

        mark_tool_search_dirty()
        saved_teams = _serialize_thread_teams(agent, user_id)["teams"]
        team = _find_team(saved_teams, team_id)
        return team or {"id": team_id, "name": name, "thread_ids": thread_ids}

    @router.patch("/thread-teams/{team_id}")
    async def update_thread_team(
        team_id: str,
        request: ThreadTeamUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Rename a callable team and/or replace its thread membership."""
        agent = get_agent_fn()
        teams = _serialize_thread_teams(agent, user_id)["teams"]
        existing = _find_team(teams, team_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Thread team not found")

        name = str(existing["name"])
        if request.name is not None:
            name = normalize_thread_team_name(request.name)
            if _team_name_taken(teams, name, excluding_team_id=team_id):
                raise HTTPException(
                    status_code=409,
                    detail=f"Thread team '{name}' already exists",
                )

        if request.thread_ids is None:
            thread_ids = list(existing["thread_ids"])
        else:
            thread_ids = _require_team_thread_ids(
                user,
                request.thread_ids,
                require_thread_access_fn,
            )

        old_ids = set(existing["thread_ids"])
        new_ids = set(thread_ids)
        for thread_id in sorted(old_ids - new_ids):
            _save_thread_team_membership(
                agent,
                thread_id,
                team_id=None,
                team_name=None,
            )
        for thread_id in thread_ids:
            _save_thread_team_membership(
                agent,
                thread_id,
                team_id=team_id,
                team_name=name,
            )

        _invalidate_user_team_graphs(agent, user_id)
        from ...core.tool_search_index import mark_tool_search_dirty

        mark_tool_search_dirty()
        saved_teams = _serialize_thread_teams(agent, user_id)["teams"]
        team = _find_team(saved_teams, team_id)
        return team or {"id": team_id, "name": name, "thread_ids": thread_ids}

    @router.delete("/thread-teams/{team_id}")
    async def delete_thread_team(
        team_id: str,
        user_id: str = Depends(authed_user_id),
    ):
        """Delete a callable team by clearing membership from its threads."""
        agent = get_agent_fn()
        teams = _serialize_thread_teams(agent, user_id)["teams"]
        existing = _find_team(teams, team_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Thread team not found")
        for thread_id in existing["thread_ids"]:
            _save_thread_team_membership(
                agent,
                thread_id,
                team_id=None,
                team_name=None,
            )
        _invalidate_user_team_graphs(agent, user_id)
        from ...core.tool_search_index import mark_tool_search_dirty

        mark_tool_search_dirty()
        return {"status": "ok", "team_id": team_id}

    return router
