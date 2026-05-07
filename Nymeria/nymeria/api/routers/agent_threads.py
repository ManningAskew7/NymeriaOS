"""Agent Threads routes."""

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.accounts import AuthenticatedUser
from ...core.event_bus import publish_sync_event as default_publish_sync_event
from ...core.thread_config import ThreadConfig, ThreadLLMConfig
from ...tools import builtin_tool_names
from ..schemas.agent_threads import AgentThreadCreateRequest


def create_agent_threads_router(
    verify_api_key: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create the Agent Threads router with app dependencies injected."""
    router = APIRouter(tags=["Agent Threads"])

    @router.get("/agents/templates")
    async def list_agent_templates(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List agent templates (legacy; returns empty list)."""
        return {"templates": [], "total": 0}

    @router.get("/agents/threads")
    async def list_agent_threads(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List the caller's callable threads (callable=True)."""
        agent = get_agent_fn()
        owned = set(agent.accounts_repo.list_threads_for_user(user.id))
        threads = agent.thread_config_manager.list_callable_threads(
            owned_thread_ids=owned,
        )
        result = []
        for tc in threads:
            data = tc.model_dump(mode="json")
            data["has_customizations"] = tc.has_customizations()
            result.append(data)
        return {"threads": result, "total": len(result)}

    @router.post("/agents/threads")
    async def create_agent_thread(
        http_request: Request,
        request: AgentThreadCreateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Create a new callable thread."""
        agent = get_agent_fn()

        if request.callable_name in builtin_tool_names():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Callable name '{request.callable_name}' conflicts with "
                    "a built-in tool name"
                ),
            )

        # Check if a callable thread with this name already exists for this
        # user. Two users can each have a "Helper"; invocation is gated by
        # ownership at runtime so there is no actual conflict.
        owned = set(agent.accounts_repo.list_threads_for_user(user.id))
        existing = agent.thread_config_manager.get_callable_thread_by_name(
            request.callable_name,
            owned_thread_ids=owned,
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Callable thread for '{request.callable_name}' already "
                    f"exists: {existing.thread_id}"
                ),
            )

        llm_config = None
        if (
            request.llm_provider
            or request.llm_model
            or request.llm_temperature is not None
            or request.llm_max_tokens is not None
        ):
            llm_config = ThreadLLMConfig(
                provider=request.llm_provider,
                model=request.llm_model,
                temperature=request.llm_temperature,
                max_tokens=request.llm_max_tokens,
            )

        random_suffix = uuid.uuid4().hex[:8]
        thread_id = f"agent-{request.callable_name.lower()}-{random_suffix}"

        tc = ThreadConfig(
            thread_id=thread_id,
            system_prompt=request.system_prompt,
            callable=True,
            callable_name=request.callable_name,
            callable_description=(
                request.callable_description
                or f"Invoke the {request.callable_name} callable thread"
            ),
            llm_config=llm_config,
        )

        if not agent.thread_config_manager.save_config(tc):
            raise HTTPException(status_code=500, detail="Failed to create agent thread")

        # Claim the thread for the creator so the runtime ownership gate in
        # create_callable_thread_tool() lets the creator invoke it but rejects
        # anyone else.
        agent.accounts_repo.claim_thread(thread_id, user.id)

        agent.thread_metadata_manager.upsert_thread(
            user.id,
            thread_id,
            title=request.callable_name,
            title_source="callable",
            platform="callable",
        )

        agent.sync_agent_tools()

        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event_fn(
            event_type="thread_created",
            thread_id=thread_id,
            user_id=user.id,
            data={
                "title": request.callable_name,
                "title_source": "callable",
                "platform": "callable",
            },
            origin_client_id=client_id,
        )

        result = tc.model_dump(mode="json")
        result["has_customizations"] = tc.has_customizations()
        return result

    return router
