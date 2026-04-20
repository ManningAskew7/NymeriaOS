"""REST API endpoints for the trigger system.

Mounted as a sub-router on the main FastAPI app at ``/triggers``.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..core.trigger_manager import (
    TriggerAction,
    TriggerCondition,
    TriggerDefinition,
    TriggerManager,
    _safe_format,
)

if TYPE_CHECKING:
    from ..core.agent import NymeriaAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class TriggerConditionRequest(BaseModel):
    field: str
    operator: str = "contains"
    value: str = ""
    case_sensitive: bool = False


class TriggerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    source_type: str = Field(..., min_length=1)
    source_config: dict = Field(default_factory=dict)
    action_type: str = Field(...)
    action_config: dict = Field(default_factory=dict)
    conditions: List[TriggerConditionRequest] = Field(default_factory=list)
    cooldown_seconds: int = Field(default=0, ge=0)
    enabled: bool = Field(default=True)
    thread_id: Optional[str] = Field(default=None, description="Bind to existing thread instead of creating trigger thread")


class TriggerUpdateRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    source_config: Optional[dict] = None
    action_type: Optional[str] = None
    action_config: Optional[dict] = None
    conditions: Optional[List[TriggerConditionRequest]] = None
    cooldown_seconds: Optional[int] = None


class TriggerResponse(BaseModel):
    id: str
    name: str
    source_type: str
    source_config: dict
    action: TriggerAction
    conditions: List[TriggerCondition] = Field(default_factory=list)
    enabled: bool
    cooldown_seconds: int
    last_fired: Optional[str] = None
    fire_count: int = 0
    thread_id: str = ""
    created_at: str
    created_by: str
    consecutive_errors: int = 0
    last_error: Optional[str] = None
    health_status: str = "healthy"

    @classmethod
    def from_definition(cls, t: TriggerDefinition) -> "TriggerResponse":
        return cls(
            id=t.id,
            name=t.name,
            source_type=t.source_type,
            source_config=t.source_config,
            action=t.action,
            conditions=t.conditions,
            enabled=t.enabled,
            cooldown_seconds=t.cooldown_seconds,
            last_fired=t.last_fired.isoformat() if t.last_fired else None,
            fire_count=t.fire_count,
            thread_id=t.thread_id,
            created_at=t.created_at.isoformat(),
            created_by=t.created_by,
            consecutive_errors=t.consecutive_errors,
            last_error=t.last_error,
            health_status=t.health_status,
        )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_trigger_router(get_agent_fn, verify_api_key_fn) -> APIRouter:
    """Create the trigger system router.

    Args:
        get_agent_fn: Callable returning the global NymeriaAgent.
        verify_api_key_fn: FastAPI dependency for API key verification.

    Returns:
        Configured APIRouter with trigger CRUD + fire endpoints.
    """
    router = APIRouter(prefix="/triggers", tags=["Triggers"])

    def _get_manager() -> TriggerManager:
        settings = get_settings()
        return TriggerManager(settings.data_dir)

    # -- CRUD endpoints ---------------------------------------------------

    @router.get("", response_model=List[TriggerResponse])
    async def list_triggers(
        user_id: str = Query(default="default"),
        enabled_only: bool = Query(default=False),
        thread_id: Optional[str] = Query(default=None),
        _: bool = Depends(verify_api_key_fn),
    ):
        """List all triggers for a user, optionally filtered by thread."""
        manager = _get_manager()
        triggers = manager.get_triggers(user_id)
        if enabled_only:
            triggers = [t for t in triggers if t.enabled]
        if thread_id:
            triggers = [t for t in triggers if t.thread_id == thread_id]
        return [TriggerResponse.from_definition(t) for t in triggers]

    @router.post("", response_model=TriggerResponse, status_code=201)
    async def create_trigger(
        body: TriggerCreateRequest,
        user_id: str = Query(default="default"),
        _: bool = Depends(verify_api_key_fn),
    ):
        """Create a new trigger."""
        manager = _get_manager()
        action = TriggerAction(type=body.action_type, config=body.action_config)

        trigger = manager.add_trigger(
            user_id=user_id,
            name=body.name,
            source_type=body.source_type,
            source_config=body.source_config,
            action=action,
            cooldown_seconds=body.cooldown_seconds,
            enabled=body.enabled,
            created_by="user",
            thread_id=body.thread_id,
        )

        if trigger is None:
            raise HTTPException(
                status_code=400,
                detail="Failed to create trigger. Check source_type and config.",
            )

        # Apply conditions
        if body.conditions:
            conditions = [
                TriggerCondition(
                    field=c.field,
                    operator=c.operator,
                    value=c.value,
                    case_sensitive=c.case_sensitive,
                )
                for c in body.conditions
            ]
            manager.update_trigger(user_id, trigger.id, conditions=conditions)
            trigger = manager.get_trigger(user_id, trigger.id)

        # Create thread metadata only when trigger has its own thread (not bound to existing)
        if not body.thread_id:
            try:
                agent = get_agent_fn()
                agent.thread_metadata_manager.upsert_thread(
                    user_id, trigger.thread_id,
                    title=f"Trigger: {body.name}",
                    title_source="platform",
                    platform="trigger",
                )
            except Exception:
                pass

        return TriggerResponse.from_definition(trigger)

    @router.get("/sources/list")
    async def list_sources(_: bool = Depends(verify_api_key_fn)):
        """List available trigger source types with enriched metadata."""
        from .sources import list_sources as _list
        return {"sources": _list()}

    @router.post("/sources/reload")
    async def reload_sources(_: bool = Depends(verify_api_key_fn)):
        """Reload trigger source plugins from disk."""
        from .sources import reload_sources as _reload
        count = _reload()
        return {"sources_loaded": count}

    @router.get("/executions/recent")
    async def get_recent_executions(
        user_id: str = Query(default="default"),
        limit: int = Query(default=50, ge=1, le=200),
        _: bool = Depends(verify_api_key_fn),
    ):
        """Get recent trigger executions across all triggers."""
        manager = _get_manager()
        return manager.get_executions(user_id, limit=limit)

    @router.get("/{trigger_id}", response_model=TriggerResponse)
    async def get_trigger(
        trigger_id: str,
        user_id: str = Query(default="default"),
        _: bool = Depends(verify_api_key_fn),
    ):
        """Get a single trigger by ID."""
        manager = _get_manager()
        trigger = manager.get_trigger(user_id, trigger_id)
        if trigger is None:
            raise HTTPException(status_code=404, detail="Trigger not found")
        return TriggerResponse.from_definition(trigger)

    @router.patch("/{trigger_id}", response_model=TriggerResponse)
    async def update_trigger(
        trigger_id: str,
        body: TriggerUpdateRequest,
        user_id: str = Query(default="default"),
        _: bool = Depends(verify_api_key_fn),
    ):
        """Update a trigger's configuration."""
        manager = _get_manager()

        kwargs: Dict[str, Any] = {}
        if body.name is not None:
            kwargs["name"] = body.name
        if body.enabled is not None:
            kwargs["enabled"] = body.enabled
        if body.cooldown_seconds is not None:
            kwargs["cooldown_seconds"] = body.cooldown_seconds
        if body.conditions is not None:
            kwargs["conditions"] = [
                TriggerCondition(
                    field=c.field,
                    operator=c.operator,
                    value=c.value,
                    case_sensitive=c.case_sensitive,
                )
                for c in body.conditions
            ]

        if body.source_config is not None:
            # Validate against source schema before accepting
            existing = manager.get_trigger(user_id, trigger_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="Trigger not found")
            from .sources import get_source
            source = get_source(existing.source_type)
            if source:
                ok, msg = source.validate_config(body.source_config)
                if not ok:
                    raise HTTPException(status_code=400, detail=f"Invalid source config: {msg}")
            kwargs["source_config"] = body.source_config

        if body.action_type is not None or body.action_config is not None:
            existing = manager.get_trigger(user_id, trigger_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="Trigger not found")
            new_type = body.action_type or existing.action.type
            new_config = body.action_config if body.action_config is not None else existing.action.config
            kwargs["action"] = TriggerAction(type=new_type, config=new_config)

        if not kwargs:
            raise HTTPException(status_code=400, detail="No updates provided")

        ok = manager.update_trigger(user_id, trigger_id, **kwargs)
        if not ok:
            raise HTTPException(status_code=404, detail="Trigger not found")

        trigger = manager.get_trigger(user_id, trigger_id)
        return TriggerResponse.from_definition(trigger)

    @router.delete("/{trigger_id}", status_code=204)
    async def delete_trigger(
        trigger_id: str,
        user_id: str = Query(default="default"),
        _: bool = Depends(verify_api_key_fn),
    ):
        """Delete a trigger and clean up its thread metadata."""
        manager = _get_manager()
        trigger = manager.get_trigger(user_id, trigger_id)
        ok = manager.delete_trigger(user_id, trigger_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Trigger not found")

        # Clean up orphaned thread metadata
        if trigger and trigger.thread_id:
            try:
                agent = get_agent_fn()
                agent.thread_metadata_manager.delete_thread(user_id, trigger.thread_id)
            except Exception:
                pass

    @router.get("/{trigger_id}/executions")
    async def get_trigger_executions(
        trigger_id: str,
        user_id: str = Query(default="default"),
        limit: int = Query(default=50, ge=1, le=200),
        _: bool = Depends(verify_api_key_fn),
    ):
        """Get execution history for a specific trigger."""
        manager = _get_manager()
        return manager.get_executions(user_id, trigger_id=trigger_id, limit=limit)

    @router.post("/{trigger_id}/test")
    async def test_trigger(
        trigger_id: str,
        user_id: str = Query(default="default"),
        _: bool = Depends(verify_api_key_fn),
    ):
        """Test a trigger with sample event data (dry run, no execution)."""
        manager = _get_manager()
        trigger = manager.get_trigger(user_id, trigger_id)
        if trigger is None:
            raise HTTPException(status_code=404, detail="Trigger not found")

        from .sources import get_source
        source = get_source(trigger.source_type)
        if source is None:
            raise HTTPException(status_code=400, detail=f"Source '{trigger.source_type}' not available")

        sample_event = source.get_sample_event(trigger.source_config)
        template_vars = {
            **sample_event,
            "trigger_id": trigger.id,
            "trigger_name": trigger.name,
            "fired_at": datetime.utcnow().isoformat(),
        }

        action = trigger.action
        if action.type == "agent_prompt":
            template = action.config.get("prompt_template") or action.config.get("prompt") or ""
            rendered = _safe_format(template, template_vars)
        elif action.type == "notify":
            rendered = _safe_format(action.config.get("message_template", ""), template_vars)
        elif action.type == "create_todo":
            rendered = _safe_format(action.config.get("task_template", ""), template_vars)
        else:
            rendered = ""

        # Check if conditions would pass
        conditions_pass = True
        if trigger.conditions:
            conditions_pass = TriggerManager._evaluate_conditions(sample_event, trigger.conditions)

        return {
            "sample_event": sample_event,
            "rendered_output": rendered,
            "action_type": action.type,
            "template_variables_used": list(template_vars.keys()),
            "conditions_pass": conditions_pass,
        }

    # -- Webhook fire endpoint --------------------------------------------

    @router.post("/fire/{trigger_id}")
    async def fire_trigger(
        trigger_id: str,
        request: Request,
        secret: Optional[str] = Query(default=None),
        user_id: str = Query(default="default"),
    ):
        """Fire a webhook trigger.

        This endpoint does NOT require API key auth -- it's designed to be
        called by external services (Tasker, IFTTT, Zapier, n8n, etc.).
        Authentication is via the optional per-trigger shared secret.
        """
        manager = _get_manager()
        trigger = manager.get_trigger(user_id, trigger_id)

        if trigger is None:
            raise HTTPException(status_code=404, detail="Trigger not found")

        if not trigger.enabled:
            raise HTTPException(status_code=409, detail="Trigger is disabled")

        if trigger.source_type != "webhook":
            raise HTTPException(status_code=400, detail="Trigger is not a webhook source")

        # Validate secret
        from .sources import get_source
        source = get_source("webhook")
        if source and hasattr(source, "validate_secret"):
            if not source.validate_secret(trigger.source_config, secret):
                raise HTTPException(status_code=403, detail="Invalid secret")

        # Parse request body
        try:
            body = await request.json()
        except Exception:
            body = {}

        event = {
            **body,
            "fired_at": datetime.utcnow().isoformat(),
            "source_ip": request.client.host if request.client else "unknown",
        }

        # Cooldown check
        if trigger.cooldown_seconds and trigger.last_fired:
            elapsed = (datetime.utcnow() - trigger.last_fired).total_seconds()
            if elapsed < trigger.cooldown_seconds:
                remaining = int(trigger.cooldown_seconds - elapsed)
                raise HTTPException(
                    status_code=429,
                    detail=f"Cooldown active. Retry in {remaining}s.",
                )

        manager.update_trigger(user_id, trigger_id, last_fired=datetime.utcnow(), fire_count=trigger.fire_count + 1)

        import threading
        agent = get_agent_fn()

        def _fire():
            try:
                manager.fire_action(trigger, event, agent, user_id)
            except Exception as e:
                logger.error(f"Trigger fire failed for {trigger_id}: {e}", exc_info=True)

        threading.Thread(target=_fire, name=f"trigger-fire-{trigger_id}", daemon=True).start()

        return {
            "status": "fired",
            "trigger_id": trigger_id,
            "trigger_name": trigger.name,
            "action_type": trigger.action.type,
        }

    return router
