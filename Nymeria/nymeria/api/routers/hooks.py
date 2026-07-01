"""REST API endpoints for lifecycle hooks.

Mounted as a sub-router at ``/hooks``. Pure CRUD over per-user hook definitions
plus a dry-run render; hooks fire in-process, so there is no webhook/fire
surface (the trigger router's public fire path has no hook analogue).
"""

from __future__ import annotations

import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError

from ...config import get_settings
from ...core.accounts import AuthenticatedUser
from ...core.hook_manager import HookDefinition, HookManager
from ...core.text_format import safe_format

logger = logging.getLogger(__name__)

HookEventName = Literal["prompt_submit", "post_tool_use", "done"]


class HookCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    event: HookEventName
    text: str = Field(..., min_length=1, max_length=10_000)
    matcher: Optional[str] = Field(default=None, description="Tool filter (post_tool_use only)")
    scope: Literal["global", "thread"] = Field(default="thread")
    thread_id: Optional[str] = Field(default=None)
    enabled: bool = Field(default=True)


class HookUpdateRequest(BaseModel):
    name: Optional[str] = None
    event: Optional[HookEventName] = None
    text: Optional[str] = None
    matcher: Optional[str] = None
    enabled: Optional[bool] = None


class HookResponse(BaseModel):
    id: str
    name: str
    event: str
    action: str
    text: str
    matcher: Optional[str] = None
    enabled: bool
    scope: str
    thread_id: str = ""
    created_by: str
    created_at: str
    updated_at: str

    @classmethod
    def from_definition(cls, h: HookDefinition) -> "HookResponse":
        return cls(
            id=h.id,
            name=h.name,
            event=h.event,
            action=h.logic.action,
            text=h.logic.text,
            matcher=h.matcher,
            enabled=h.enabled,
            scope=h.scope,
            thread_id=h.thread_id,
            created_by=h.created_by,
            created_at=h.created_at.isoformat(),
            updated_at=h.updated_at.isoformat(),
        )


def create_hook_router(
    get_agent_fn,
    verify_api_key_fn,
    require_thread_access_fn=None,
) -> APIRouter:
    """Create the lifecycle-hooks CRUD router.

    Args:
        get_agent_fn: Callable returning the global NymeriaAgent (unused today;
            kept for signature parity with the trigger router).
        verify_api_key_fn: FastAPI dependency for API key verification.
        require_thread_access_fn: Optional ``(user, thread_id)`` guard raising
            HTTPException when the caller cannot bind a hook to that thread.
    """
    router = APIRouter(prefix="/hooks", tags=["Hooks"])

    _manager: Optional[HookManager] = None

    def _get_manager() -> HookManager:
        nonlocal _manager
        if _manager is None:
            _manager = HookManager(get_settings().data_dir)
        return _manager

    @router.get("", response_model=List[HookResponse])
    async def list_hooks(
        user_id: str = Query(default="default"),
        enabled_only: bool = Query(default=False),
        thread_id: Optional[str] = Query(default=None),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """List a user's hooks, optionally filtered by enabled state or thread."""
        user_id = user.id  # Override any client-claimed ?user_id=
        hooks = _get_manager().get_hooks(user_id)
        if enabled_only:
            hooks = [h for h in hooks if h.enabled]
        if thread_id:
            hooks = [h for h in hooks if h.scope == "global" or h.thread_id == thread_id]
        return [HookResponse.from_definition(h) for h in hooks]

    @router.post("", response_model=HookResponse, status_code=201)
    async def create_hook(
        body: HookCreateRequest,
        user_id: str = Query(default="default"),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Create a hook."""
        user_id = user.id  # Override any client-claimed ?user_id=
        # A thread-scoped hook needs a thread to bind to; without one it would be
        # stored inert (its thread_id never matches a real turn). Reject early.
        if body.scope == "thread" and not body.thread_id:
            raise HTTPException(
                status_code=400, detail="A thread-scoped hook requires a thread_id."
            )
        # A global hook applies to all threads, so it carries no thread_id.
        thread_id = body.thread_id if body.scope == "thread" else ""
        if body.scope == "thread" and require_thread_access_fn is not None:
            require_thread_access_fn(user, thread_id)
        try:
            hook = _get_manager().add_hook(
                user_id,
                name=body.name,
                event=body.event,
                text=body.text,
                matcher=body.matcher,
                scope=body.scope,
                thread_id=thread_id,
                enabled=body.enabled,
                created_by="user",
            )
        except (ValidationError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        if hook is None:
            raise HTTPException(status_code=400, detail="Hook limit reached for this user.")
        return HookResponse.from_definition(hook)

    @router.get("/{hook_id}", response_model=HookResponse)
    async def get_hook(
        hook_id: str,
        user_id: str = Query(default="default"),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Get one hook by ID."""
        user_id = user.id
        hook = _get_manager().get_hook(user_id, hook_id)
        if hook is None:
            raise HTTPException(status_code=404, detail="Hook not found")
        return HookResponse.from_definition(hook)

    @router.patch("/{hook_id}", response_model=HookResponse)
    async def update_hook(
        hook_id: str,
        body: HookUpdateRequest,
        user_id: str = Query(default="default"),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Update a hook's configuration."""
        user_id = user.id
        manager = _get_manager()
        updates = body.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No updates provided")
        try:
            ok = manager.update_hook(user_id, hook_id, **updates)
        except (ValidationError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not ok:
            raise HTTPException(status_code=404, detail="Hook not found")
        hook = manager.get_hook(user_id, hook_id)
        if hook is None:
            raise HTTPException(status_code=404, detail="Hook not found after update")
        return HookResponse.from_definition(hook)

    @router.delete("/{hook_id}", status_code=204)
    async def delete_hook(
        hook_id: str,
        user_id: str = Query(default="default"),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Delete a hook."""
        user_id = user.id
        if not _get_manager().delete_hook(user_id, hook_id):
            raise HTTPException(status_code=404, detail="Hook not found")

    @router.post("/{hook_id}/test")
    async def test_hook(
        hook_id: str,
        user_id: str = Query(default="default"),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Dry-run render of a hook's text against sample data (no fire)."""
        user_id = user.id
        hook = _get_manager().get_hook(user_id, hook_id)
        if hook is None:
            raise HTTPException(status_code=404, detail="Hook not found")
        sample = {
            "event": hook.event, "thread_id": "thread-123", "user_id": "you",
            "is_autonomous": "False", "holder_kind": "interactive",
            "trigger_label": "User Message", "prompt": "the user's message",
            "tool_name": "Edit", "tool_result": "the tool's output",
            "tool_status": "success", "tool_args": '{"file": "a.py"}',
            "final_text": "the assistant's final reply",
        }
        return {"hook_id": hook.id, "event": hook.event, "rendered": safe_format(hook.logic.text, sample)}

    return router
