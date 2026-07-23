"""Thread configuration and callable-team routes."""

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...core.accounts import AuthenticatedUser
from ...core.team_manager import serialize_thread_teams
from ...core.thread_config import DreamingConfig, ThreadConfig, ThreadLLMConfig
from ..schemas.thread_config import (
    NotepadUpdateRequest,
    ThreadConfigUpdateRequest,
    ThreadTeamCreateRequest,
    ThreadTeamUpdateRequest,
)
from ..thread_config_helpers import (
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


def _config_response(
    config: ThreadConfig,
    *,
    agent: Any = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    result = config.model_dump(mode="json")
    result["has_customizations"] = config.has_customizations()
    # callable_team_name is deprecated on the config (backlog #100): clients
    # keep receiving it, DERIVED from the team entity store, falling back to
    # a surviving legacy config value and then the raw id.
    if config.callable_team_id and agent is not None and user_id:
        manager = getattr(agent, "team_manager", None)
        derived = (
            manager.resolve_team_name(user_id, config.callable_team_id)
            if manager is not None
            else None
        )
        result["callable_team_name"] = (
            derived or config.callable_team_name or config.callable_team_id
        )
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
    """Team list via the shared serializer (core/team_manager.py).

    Store entities carry name/description; membership comes from the one
    shared config scan. Kept as a local name so router callers read naturally.
    """
    return serialize_thread_teams(agent, user_id)


def _find_team(teams: list[dict[str, Any]], team_id: str) -> dict[str, Any] | None:
    """Return the team with ``team_id`` from an already-serialized team list.

    Pure over the pre-computed list so a handler can run the serializer once
    (its membership scan is an O(N) read over every owned thread's config)
    and reuse the result for several lookups.
    """
    for team in teams:
        if team["id"] == team_id:
            return team
    return None


def _require_team_thread_ids(
    user: AuthenticatedUser,
    thread_ids: list[str],
    require_thread_access_fn: Callable[..., None],
) -> list[str]:
    """Validate and dedupe requested member threads; empty is legal.

    Empty teams are a supported state (backlog #100): a team entity may exist
    with no member threads, so no minimum count is enforced here.
    """
    seen: set[str] = set()
    clean: list[str] = []
    for raw_id in thread_ids:
        thread_id = str(raw_id or "").strip()
        if not thread_id or thread_id in seen:
            continue
        require_thread_access_fn(user, thread_id)
        clean.append(thread_id)
        seen.add(thread_id)
    return clean


def _save_thread_team_membership(
    agent: Any,
    thread_id: str,
    *,
    team_id: str | None,
) -> None:
    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)
    tc.callable_team_id = team_id
    # Deprecated: names live in the team entity store; team writes clear the
    # legacy config field.
    tc.callable_team_name = None
    if not agent.thread_config_manager.save_config(tc):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to save team membership for thread {thread_id}",
        )


def _get_or_adopt_team(agent: Any, user_id: str, team_id: str) -> Any:
    """Resolve a team entity, adopting a dangling membership-referenced id.

    Raw config edits can reference team ids the store has never seen; PATCH
    and DELETE keep working on those by adopting the id (display name from a
    surviving legacy config name, else the id). Returns None when the id is
    neither in the store nor referenced by any membership.
    """
    manager = agent.team_manager
    team = manager.get_team(user_id, team_id)
    if team is not None:
        return team
    memberships, legacy_names = manager._membership_scan(user_id)
    if team_id in memberships:
        return manager.ensure_team_exists(
            user_id, team_id, fallback_name=legacy_names.get(team_id)
        )
    return None


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
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get per-thread configuration (returns defaults if none saved)."""
        require_thread_access_fn(user, thread_id, claim=False)
        agent = get_agent_fn()
        tc = agent.thread_config_manager.get_config(thread_id)
        if tc:
            return _config_response(tc, agent=agent, user_id=user_id)
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
            # "" is the unteam sentinel; store None so read sites need no
            # normalization for configs written from here on.
            old_team_id = tc.callable_team_id or None
            old_team_name = tc.callable_team_name
            tc.callable_team_id = request.callable_team_id or None
            tc.callable_team_name = None
            manager = getattr(agent, "team_manager", None)
            if manager is not None:
                # Bank a surviving legacy name for the PREVIOUS team before
                # it leaves this config (unteam or move): the store must hold
                # the name before this save clears it.
                if old_team_id and old_team_name:
                    manager.ensure_team_exists(
                        user_id, old_team_id, fallback_name=old_team_name
                    )
                if tc.callable_team_id:
                    # Keep the entity store coherent with membership writes
                    # that reference an id it has never seen (an incoming
                    # deprecated callable_team_name serves only as the adopted
                    # entity's display-name fallback; renames go through the
                    # teams API).
                    manager.ensure_team_exists(
                        user_id,
                        tc.callable_team_id,
                        fallback_name=request.callable_team_name,
                    )
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
        if request.clear_hooks_enabled:
            tc.hooks_enabled = None
        elif request.hooks_enabled is not None:
            tc.hooks_enabled = request.hooks_enabled
        if request.clear_hook_overrides:
            tc.hook_overrides = {}
        elif request.hook_overrides is not None:
            tc.hook_overrides = request.hook_overrides
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

        # Deprecated-name hygiene (backlog #100): a surviving legacy
        # callable_team_name is folded into the entity store (migration ran on
        # store access; adoption covers dangling ids), then cleared so the
        # config never re-persists it.
        if tc.callable_team_name is not None:
            manager = getattr(agent, "team_manager", None)
            if manager is not None:
                if tc.callable_team_id:
                    manager.ensure_team_exists(
                        user_id,
                        tc.callable_team_id,
                        fallback_name=tc.callable_team_name,
                    )
                tc.callable_team_name = None

        if not agent.thread_config_manager.save_config(tc):
            raise HTTPException(status_code=500, detail="Failed to save thread config")

        agent.invalidate_thread_config_cache(thread_id)
        if request.callable_team_id is not None:
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
        elif request.callable_team_id is not None:
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

        return _config_response(tc, agent=agent, user_id=user_id)

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
        """Create a callable team, optionally moving threads into it.

        ``thread_ids`` may be empty: a team entity can exist with no members.
        """
        agent = get_agent_fn()
        name = normalize_thread_team_name(request.name)
        thread_ids = _require_team_thread_ids(
            user,
            request.thread_ids,
            require_thread_access_fn,
        )
        try:
            team = agent.team_manager.create_team(
                user_id,
                name=name,
                description=request.description,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        for thread_id in thread_ids:
            _save_thread_team_membership(agent, thread_id, team_id=team.id)
        if thread_ids:
            _invalidate_user_team_graphs(agent, user_id)
            from ...core.tool_search_index import mark_tool_search_dirty

            mark_tool_search_dirty()
        saved_teams = _serialize_thread_teams(agent, user_id)["teams"]
        found = _find_team(saved_teams, team.id)
        return found or {
            "id": team.id,
            "name": team.name,
            "description": team.description,
            "thread_ids": thread_ids,
        }

    @router.patch("/thread-teams/{team_id}")
    async def update_thread_team(
        team_id: str,
        request: ThreadTeamUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Rename/describe a callable team and/or replace its membership.

        A rename or description edit alone is an O(1) store write: no member
        thread config is touched and no graph is invalidated. Replacing
        membership with ``[]`` unteams every member but keeps the team.
        """
        agent = get_agent_fn()
        manager = agent.team_manager
        existing = _get_or_adopt_team(agent, user_id, team_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Thread team not found")

        if request.name is not None:
            try:
                manager.rename_team(
                    user_id, team_id, normalize_thread_team_name(request.name)
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        if request.description is not None:
            manager.describe_team(user_id, team_id, request.description)

        membership_changed = False
        if request.thread_ids is not None:
            thread_ids = _require_team_thread_ids(
                user,
                request.thread_ids,
                require_thread_access_fn,
            )
            old_ids = set(manager.members(user_id, team_id))
            new_ids = set(thread_ids)
            for thread_id in sorted(old_ids - new_ids):
                _save_thread_team_membership(agent, thread_id, team_id=None)
            for thread_id in sorted(new_ids - old_ids):
                _save_thread_team_membership(agent, thread_id, team_id=team_id)
            membership_changed = old_ids != new_ids

        if membership_changed:
            _invalidate_user_team_graphs(agent, user_id)
        if membership_changed or request.name is not None:
            # The search index tags callables with the team name, so a rename
            # re-tags even though no graph rebuild is needed.
            from ...core.tool_search_index import mark_tool_search_dirty

            mark_tool_search_dirty()
        saved_teams = _serialize_thread_teams(agent, user_id)["teams"]
        team = _find_team(saved_teams, team_id)
        if team is None:
            raise HTTPException(status_code=404, detail="Thread team not found")
        return team

    @router.delete("/thread-teams/{team_id}")
    async def delete_thread_team(
        team_id: str,
        user_id: str = Depends(authed_user_id),
    ):
        """Delete a callable team: clear its membership and remove the entity."""
        agent = get_agent_fn()
        manager = agent.team_manager
        members = manager.members(user_id, team_id)
        deleted = manager.delete_team(user_id, team_id)
        if not deleted and not members:
            raise HTTPException(status_code=404, detail="Thread team not found")
        for thread_id in members:
            _save_thread_team_membership(agent, thread_id, team_id=None)
        if members:
            _invalidate_user_team_graphs(agent, user_id)
            from ...core.tool_search_index import mark_tool_search_dirty

            mark_tool_search_dirty()
        return {"status": "ok", "team_id": team_id}

    return router
