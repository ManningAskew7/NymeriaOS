"""User memory endpoints."""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.accounts import AuthenticatedUser

logger = logging.getLogger(__name__)


class MemorySaveRequest(BaseModel):
    key: str = Field(..., description="Memory key identifier")
    value: str = Field(..., max_length=1000, description="Memory content")


def create_memory_router(
    verify_api_key: Callable[..., Any],
    get_agent_fn: Callable[..., Any],
    require_same_user_or_admin_fn: Callable[..., None],
) -> APIRouter:
    """Create the user memory router with app dependencies injected."""
    router = APIRouter(tags=["User Memories"])

    def _upsert_memory_rag_chunk(user_id: str, key: str, value: str) -> None:
        """Best-effort profile-memory sync into the user's RAG index."""
        agent = get_agent_fn()
        memory_index = agent._get_memory_index(user_id)
        if not memory_index:
            return
        try:
            memory_index.delete_memory_key(user_id, key)
            memory_index.add_chunk(
                content=f"{key}: {value}",
                metadata={"key": key},
                chunk_type="memory",
                user_id=user_id,
            )
        except Exception as e:
            logger.warning(f"Failed to sync memory '{key}' into RAG index: {e}")

    def _delete_memory_rag_chunk(user_id: str, key: str) -> None:
        """Best-effort removal of a profile memory from the user's RAG index."""
        agent = get_agent_fn()
        memory_index = agent._get_memory_index(user_id)
        if not memory_index:
            return
        try:
            memory_index.delete_memory_key(user_id, key)
        except Exception as e:
            logger.warning(f"Failed to remove memory '{key}' from RAG index: {e}")

    @router.get("/users/{user_id}/memories")
    async def list_memories(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List all memories stored for a user."""
        require_same_user_or_admin_fn(user, user_id)
        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        return {
            "user_id": user_id,
            "memories": [
                {
                    "key": m.key,
                    "value": m.value,
                    "created_at": m.created_at.isoformat(),
                    "accessed_at": m.accessed_at.isoformat(),
                    "access_count": m.access_count,
                }
                for m in profile.memories
            ],
            "count": len(profile.memories),
        }

    @router.post("/users/{user_id}/memories")
    async def save_memory(
        user_id: str,
        request: MemorySaveRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Save or update a memory for a user."""
        require_same_user_or_admin_fn(user, user_id)
        agent = get_agent_fn()
        with agent.profile_manager.atomic_update(user_id) as profile:
            success = profile.add_memory(request.key, request.value)
        if not success:
            raise HTTPException(
                status_code=400,
                detail=f"Memory limit reached ({profile.MAX_MEMORIES})",
            )
        _upsert_memory_rag_chunk(user_id, request.key, request.value)
        return {"status": "ok", "key": request.key}

    @router.delete("/users/{user_id}/memories/{key}")
    async def forget_memory(
        user_id: str,
        key: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Remove a memory by key."""
        require_same_user_or_admin_fn(user, user_id)
        agent = get_agent_fn()
        with agent.profile_manager.atomic_update(user_id) as profile:
            removed = profile.remove_memory(key)
        if not removed:
            raise HTTPException(status_code=404, detail=f"No memory with key '{key}'")
        _delete_memory_rag_chunk(user_id, key)
        return {"status": "ok", "key": key}

    @router.get("/users/{user_id}/memories/search")
    async def search_memories(
        user_id: str,
        q: str = Query(..., description="Search term"),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Search memories by key or value substring."""
        require_same_user_or_admin_fn(user, user_id)
        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        results = profile.search_memories(q)
        return {
            "user_id": user_id,
            "query": q,
            "results": [
                {"key": m.key, "value": m.value, "access_count": m.access_count}
                for m in results
            ],
            "count": len(results),
        }

    return router
