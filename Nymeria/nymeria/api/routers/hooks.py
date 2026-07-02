"""REST API endpoints for lifecycle hooks.

Mounted as a sub-router at ``/hooks``. Pure CRUD over per-user hook definitions
plus a dry-run render; hooks fire in-process, so there is no webhook/fire
surface (the trigger router's public fire path has no hook analogue).
"""

from __future__ import annotations

import logging
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from ...config import get_settings
from ...core.accounts import AuthenticatedUser
from ...core.conditions import HookCondition
from ...core.hook_manager import (
    HookDefinition,
    HookManager,
    build_update_kwargs,
    params_from_fields,
)
from ...core.text_format import safe_format

logger = logging.getLogger(__name__)

HookEventName = Literal["prompt_submit", "pre_tool_use", "post_tool_use", "done"]
HookActionName = Literal[
    "inject_context", "block_if_matches", "rewrite_arg", "notify", "create_todo", "webhook"
]


class HookCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    event: HookEventName
    action: HookActionName = Field(default="inject_context")  # type: ignore[bad-assignment]
    # Per-action params (flat; only the fields for `action` are read).
    text: Optional[str] = Field(
        default=None, max_length=10_000, description="inject_context/notify/create_todo/webhook body"
    )
    conditions: Optional[List[HookCondition]] = Field(
        default=None, description="block_if_matches / rewrite_arg gate (matches tool args)"
    )
    reason: Optional[str] = Field(default=None, max_length=500, description="block_if_matches")
    updates: Optional[Dict[str, str]] = Field(default=None, description="rewrite_arg")
    url: Optional[str] = Field(default=None, max_length=2_000, description="webhook target URL")
    matcher: Optional[str] = Field(default=None, description="Tool-name filter (tool events)")
    scope: Literal["global", "thread"] = Field(default="thread")
    thread_id: Optional[str] = Field(default=None)
    enabled: bool = Field(default=True)


class HookUpdateRequest(BaseModel):
    name: Optional[str] = None
    event: Optional[HookEventName] = None
    action: Optional[HookActionName] = None
    text: Optional[str] = None
    conditions: Optional[List[HookCondition]] = None
    reason: Optional[str] = None
    updates: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    matcher: Optional[str] = None
    enabled: Optional[bool] = None
    # Note: no `scope`/`thread_id` here. Re-scoping a hook to a thread needs a
    # thread_id (and its access gate), which a partial PATCH cannot supply
    # without silently binding to "" (an inert orphan); a re-scope is a
    # delete + create.


class HookResponse(BaseModel):
    id: str
    name: str
    event: str
    action: str
    # ``logic`` is the full action-specific config (discriminated on ``action``);
    # ``text`` is kept for the text action / back-compat and is "" otherwise.
    logic: dict
    text: str = ""
    matcher: Optional[str] = None
    enabled: bool
    scope: str
    thread_id: str = ""
    created_by: str
    created_at: str
    updated_at: str

    @classmethod
    def from_definition(cls, h: HookDefinition) -> "HookResponse":
        logic = h.logic.model_dump()
        return cls(
            id=h.id,
            name=h.name,
            event=h.event,
            action=h.logic.action,
            logic=logic,
            text=logic.get("text", "") or "",
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
        params = params_from_fields(
            body.action, text=body.text, conditions=body.conditions,
            reason=body.reason, updates=body.updates, url=body.url,
        )
        try:
            hook = _get_manager().add_hook(
                user_id,
                name=body.name,
                event=body.event,
                action=body.action,
                params=params,
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

    # NOTE: /schema and /executions are registered before the /{hook_id} routes
    # so the literal paths win (FastAPI matches in registration order; later,
    # "executions" would bind as a hook_id).
    @router.get("/schema")
    async def hooks_schema(
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """The machine-readable authoring taxonomy.

        Derived from the ``core/hook_spec.py`` single source plus the pydantic
        logic variants, so clients (the GUI form, bot command builders) can
        render event/action pickers and per-action param fields from data
        instead of hardcoding the legality map. ``params_schema`` is each
        action's JSON schema minus the ``action`` discriminator.
        """
        from typing import get_args

        from ...core.conditions import ConditionOperator
        from ...core.hook_manager import HOOK_LOGIC_BY_ACTION, HookStore
        from ...core.hook_spec import ACTION_SPECS, EVENTS, TOOL_EVENTS, event_actions

        legality = event_actions()
        actions = {}
        for name, spec in ACTION_SPECS.items():
            schema = HOOK_LOGIC_BY_ACTION[name].model_json_schema()
            schema.get("properties", {}).pop("action", None)
            required = [f for f in schema.get("required", []) if f != "action"]
            if required:
                schema["required"] = required
            else:
                schema.pop("required", None)
            actions[name] = {
                "plane": spec.plane,
                "events": list(spec.events),
                "text_action": spec.text_action,
                "params_schema": schema,
            }
        return {
            "events": {
                e: {"tool_event": e in TOOL_EVENTS, "actions": sorted(legality[e])}
                for e in EVENTS
            },
            "actions": actions,
            "operators": list(get_args(ConditionOperator)),
            "max_hooks": HookStore.model_fields["MAX_HOOKS"].default,
        }

    @router.get("/executions")
    async def list_hook_executions(
        hook_id: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Recent hook executions (newest first), optionally for one hook.

        Each entry records one hook run: status ``ok`` / ``no_op`` (ran,
        produced nothing) / ``error`` / ``timeout`` / ``saturated`` (dispatch
        pool starved; on pre_tool_use this failed closed) / ``illegal``, plus
        an outcome/error detail summary and the run duration.
        """
        # get_executions blocks on the write-behind flush barrier plus file
        # I/O; keep it off the event loop.
        return await run_in_threadpool(
            _get_manager().get_executions, user.id, hook_id=hook_id, limit=limit
        )

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
        existing = manager.get_hook(user_id, hook_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Hook not found")
        # Plain field updates (name/event/matcher/enabled); the logic fields
        # (text/conditions/reason/updates/url/action) are assembled separately by
        # ``build_update_kwargs`` so a partial PATCH keeps unspecified siblings.
        scalars = body.model_dump(
            exclude_none=True,
            exclude={"action", "conditions", "reason", "updates", "text", "url"},
        )
        updates = build_update_kwargs(
            existing, action=body.action, text=body.text, conditions=body.conditions,
            reason=body.reason, updates=body.updates, url=body.url, scalars=scalars,
        )
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
        """Dry-run preview of a hook against sample data (no fire).

        For ``inject_context`` this renders the template; for the guardrail
        actions it describes what the hook would do.
        """
        user_id = user.id
        hook = _get_manager().get_hook(user_id, hook_id)
        if hook is None:
            raise HTTPException(status_code=404, detail="Hook not found")
        logic = hook.logic
        sample = {
            "event": hook.event, "thread_id": "thread-123", "user_id": "you",
            "is_autonomous": "False", "holder_kind": "interactive",
            "trigger_label": "User Message", "prompt": "the user's message",
            "tool_name": "Edit", "tool_result": "the tool's output",
            "tool_status": "success", "tool_args": '{"file": "a.py"}',
            "final_text": "the assistant's final reply",
        }
        result = {"hook_id": hook.id, "event": hook.event, "action": logic.action}
        if logic.action in ("inject_context", "notify", "create_todo"):
            result["rendered"] = safe_format(getattr(logic, "text", ""), sample)
        elif logic.action == "webhook":
            result["rendered"] = (
                f"POST {safe_format(logic.url, sample)} with body "
                f"{safe_format(logic.text, sample)!r}"
            )
        elif logic.action == "block_if_matches":
            result["rendered"] = (
                f"Denies {hook.matcher or 'any tool'} when "
                + (
                    " AND ".join(f"{c.field} {c.operator} {c.value!r}" for c in logic.conditions)
                    if logic.conditions else "always"
                )
                + f" (reason: {logic.reason or 'blocked by a lifecycle hook'})"
            )
        elif logic.action == "rewrite_arg":
            result["rendered"] = (
                f"Rewrites {list(logic.updates.keys())} on {hook.matcher or 'any tool'}"
            )
        return result

    return router
