"""RAG (Retrieval Augmented Generation) endpoints."""

import logging
from collections.abc import Callable
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.accounts import AuthenticatedUser

logger = logging.getLogger(__name__)


class RagSettingsUpdate(BaseModel):
    enabled: Optional[bool] = Field(default=None, description="Enable/disable RAG")
    max_chunks: Optional[int] = Field(default=None, ge=1, le=10, description="Max chunks per message")
    include_conversations: Optional[bool] = Field(default=None, description="Include conversation history")
    include_memories: Optional[bool] = Field(default=None, description="Include saved memories")
    include_todos: Optional[bool] = Field(default=None, description="Include TODO completions")
    include_tools: Optional[bool] = Field(default=None, description="Include tool-result chunks")
    auto_flush: Optional[bool] = Field(default=None, description="Auto-flush on context trim")
    retrieval_mode: Optional[str] = Field(default=None, description="Per-user override: 'hybrid' or 'vector'")
    rerank_enabled: Optional[bool] = Field(default=None, description="Per-user override: rerank this user's rag_search results")


def create_rag_router(
    verify_api_key: Callable[..., Any],
    get_agent_fn: Callable[..., Any],
    require_same_user_or_admin_fn: Callable[..., None],
) -> APIRouter:
    """Create the RAG router with app dependencies injected."""
    router = APIRouter(tags=["RAG"])

    @router.get("/users/{user_id}/rag/settings")
    async def get_rag_settings(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get user's RAG configuration."""
        require_same_user_or_admin_fn(user, user_id)
        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        from ...config import get_settings
        rag_prefs = profile.get_rag_preferences()
        settings = get_settings()

        return {
            "enabled": profile.opt_in.rag_enabled,
            "max_chunks": rag_prefs.get("max_chunks", 5),
            "include_conversations": rag_prefs.get("include_conversations", True),
            "include_memories": rag_prefs.get("include_memories", False),
            "include_todos": rag_prefs.get("include_todos", True),
            "include_tools": rag_prefs.get("include_tools", True),
            "auto_flush": rag_prefs.get("auto_flush", True),
            "retrieval_mode": rag_prefs.get("retrieval_mode") or settings.rag_retrieval_mode,
            "rerank_enabled": rag_prefs.get("rerank_enabled", settings.rag_rerank_enabled),
        }

    @router.put("/users/{user_id}/rag/settings")
    async def update_rag_settings(
        user_id: str,
        settings_update: RagSettingsUpdate,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Update user's RAG configuration."""
        require_same_user_or_admin_fn(user, user_id)
        agent = get_agent_fn()

        with agent.profile_manager.atomic_update(user_id) as profile:
            if settings_update.enabled is not None:
                profile.opt_in.rag_enabled = settings_update.enabled

            if settings_update.max_chunks is not None:
                profile.set_rag_preference("max_chunks", settings_update.max_chunks)

            if settings_update.include_conversations is not None:
                profile.set_rag_preference("include_conversations", settings_update.include_conversations)

            if settings_update.include_memories is not None:
                profile.set_rag_preference("include_memories", settings_update.include_memories)

            if settings_update.include_todos is not None:
                profile.set_rag_preference("include_todos", settings_update.include_todos)

            if settings_update.include_tools is not None:
                profile.set_rag_preference("include_tools", settings_update.include_tools)

            if settings_update.auto_flush is not None:
                profile.set_rag_preference("auto_flush", settings_update.auto_flush)

            if settings_update.retrieval_mode is not None:
                profile.set_rag_preference("retrieval_mode", settings_update.retrieval_mode)

            if settings_update.rerank_enabled is not None:
                profile.set_rag_preference("rerank_enabled", settings_update.rerank_enabled)

            from ...config import get_settings
            rag_prefs = profile.get_rag_preferences()
            settings = get_settings()
            return {
                "status": "ok",
                "enabled": profile.opt_in.rag_enabled,
                "max_chunks": rag_prefs.get("max_chunks", 5),
                "include_conversations": rag_prefs.get("include_conversations", True),
                "include_memories": rag_prefs.get("include_memories", False),
                "include_todos": rag_prefs.get("include_todos", True),
                "include_tools": rag_prefs.get("include_tools", True),
                "auto_flush": rag_prefs.get("auto_flush", True),
                "retrieval_mode": rag_prefs.get("retrieval_mode") or settings.rag_retrieval_mode,
                "rerank_enabled": rag_prefs.get("rerank_enabled", settings.rag_rerank_enabled),
            }

    @router.get("/users/{user_id}/rag/stats")
    async def get_rag_stats(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get indexing statistics for a user."""
        require_same_user_or_admin_fn(user, user_id)
        from ...core.memory_index import MemoryIndex

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)

        if not profile.opt_in.rag_enabled:
            return {
                "enabled": False,
                "message": "RAG is not enabled for this user",
            }

        try:
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_") or "default"
            db_path = agent.settings.data_dir / "users" / safe_user_id / "memory.db"

            if not db_path.exists():
                return {
                    "enabled": True,
                    "total_chunks": 0,
                    "by_type": {},
                    "last_indexed": None,
                    "vector_count": 0,
                }

            memory_index = MemoryIndex(db_path)
            try:
                stats = memory_index.get_stats(user_id)
            finally:
                memory_index.close()  # throwaway instance: release its connection
            stats["enabled"] = True
            return stats

        except Exception as e:
            logger.error(f"Failed to get RAG stats for user {user_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/users/{user_id}/rag/search")
    async def search_rag(
        user_id: str,
        q: str = Query(..., min_length=1, description="Search query"),
        max_results: int = Query(default=5, ge=1, le=10),
        around: Optional[str] = Query(
            default=None,
            description="Date to softly bias results toward (ISO at any precision, e.g. 2026, 2026-04, 2026-04-15). Guides ranking only; strong matches from other times still appear.",
        ),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Search a user's RAG index and return structured chunk results."""
        require_same_user_or_admin_fn(user, user_id)
        from ...core.memory_index import parse_anchor_string
        from ...config import get_settings

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)

        if not profile.opt_in.rag_enabled:
            raise HTTPException(
                status_code=400,
                detail=f"RAG is not enabled for user '{user_id}'.",
            )

        memory_index = agent._get_memory_index(user_id)
        if not memory_index:
            raise HTTPException(status_code=500, detail="Could not access memory index")

        settings = get_settings()
        anchor = (
            parse_anchor_string(around)
            if (around and settings.rag_anchor_enabled) else None
        )
        if around and settings.rag_anchor_enabled and anchor is None:
            raise HTTPException(
                status_code=400, detail=f"Could not read the date '{around}'."
            )

        rag_prefs = profile.get_rag_preferences()
        chunk_types: List[str] = []
        if rag_prefs.get("include_conversations", True):
            chunk_types.append("conversation")
        if rag_prefs.get("include_memories", False):
            chunk_types.append("memory")
        if rag_prefs.get("include_todos", True):
            chunk_types.append("todo")
        if rag_prefs.get("include_tools", True):
            chunk_types.append("tool")

        if not chunk_types:
            return {
                "user_id": user_id,
                "query": q,
                "results": [],
                "total": 0,
                "message": "All RAG content types are disabled.",
            }

        results = memory_index.search(
            query=q,
            user_id=user_id,
            limit=max_results,
            chunk_types=chunk_types,
            retrieval_mode=rag_prefs.get("retrieval_mode") or settings.rag_retrieval_mode,
            anchor_start=anchor.start if anchor else None,
            anchor_end=anchor.end if anchor else None,
            anchor_edge_sigma_days=anchor.edge_sigma_days if anchor else None,
            anchor_weight=settings.rag_anchor_weight,
            anchor_floor=settings.rag_anchor_floor,
        )

        return {
            "user_id": user_id,
            "query": q,
            "results": [
                {
                    "id": result.id,
                    "content": result.content,
                    "chunk_type": result.chunk_type,
                    "thread_id": result.thread_id,
                    "created_at": result.created_at.isoformat(),
                    "metadata": result.metadata,
                    "score": result.score,
                }
                for result in results
            ],
            "total": len(results),
        }

    @router.post("/users/{user_id}/rag/reindex")
    async def reindex_user(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Rebuild saved-memory chunks in the user's RAG index."""
        require_same_user_or_admin_fn(user, user_id)
        from ...core.memory_index import MemoryIndex

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)

        if not profile.opt_in.rag_enabled:
            raise HTTPException(
                status_code=400,
                detail="RAG is not enabled for this user. Enable it first.",
            )

        try:
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_") or "default"
            db_path = agent.settings.data_dir / "users" / safe_user_id / "memory.db"
            memory_index = MemoryIndex(db_path)
            try:
                cleared = memory_index.delete_by_type(user_id, "memory")

                indexed_memories = 0
                for memory in profile.memories:
                    memory_index.add_chunk(
                        content=f"{memory.key}: {memory.value}",
                        metadata={"key": memory.key},
                        chunk_type="memory",
                        user_id=user_id,
                    )
                    indexed_memories += 1
            finally:
                memory_index.close()  # throwaway instance: release its connection

            return {
                "status": "ok",
                "cleared_memory_chunks": cleared,
                "indexed_memories": indexed_memories,
                "message": "Index rebuilt. New conversations will be indexed automatically.",
            }

        except Exception as e:
            logger.error(f"Failed to reindex for user {user_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.delete("/users/{user_id}/rag/index")
    async def clear_index(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Clear user's RAG index."""
        require_same_user_or_admin_fn(user, user_id)
        from ...core.memory_index import MemoryIndex

        agent = get_agent_fn()

        try:
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_") or "default"
            db_path = agent.settings.data_dir / "users" / safe_user_id / "memory.db"

            if not db_path.exists():
                return {
                    "status": "ok",
                    "cleared_chunks": 0,
                    "message": "No index found for this user.",
                }

            memory_index = MemoryIndex(db_path)
            try:
                cleared = memory_index.clear_index(user_id)
            finally:
                memory_index.close()  # throwaway instance: release its connection

            return {
                "status": "ok",
                "cleared_chunks": cleared,
            }

        except Exception as e:
            logger.error(f"Failed to clear index for user {user_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    return router
