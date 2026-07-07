"""REST API endpoints for the trigger system.

Mounted as a sub-router on the main FastAPI app at ``/triggers``.
"""

import inspect
import logging
from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..core.accounts import AuthenticatedUser
from ..core.time_utils import ensure_aware_utc, utc_now
from ..core.trigger_manager import (
    TriggerAction,
    TriggerCondition,
    TriggerDefinition,
    TriggerExecution,
    TriggerManager,
    _safe_format,
)
from .sse_consumer import parse_sse_data_line

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class TriggerConditionRequest(BaseModel):
    field: str
    # Mirrors core/conditions.py::ConditionOperator (shared evaluator); keep in
    # sync so the REST surface accepts everything the evaluator supports.
    operator: Literal[
        "equals", "contains", "starts_with", "matches_regex", "not_equals",
        "gt", "gte", "lt", "lte",
    ] = "contains"
    value: str = ""
    case_sensitive: bool = False


class TriggerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    source_type: str = Field(..., min_length=1)
    source_config: dict = Field(default_factory=dict)
    action_type: Literal["agent_prompt", "notify", "create_todo", "run_workflow"] = Field(...)
    action_config: dict = Field(default_factory=dict)
    conditions: List[TriggerConditionRequest] = Field(default_factory=list)
    cooldown_seconds: int = Field(default=0, ge=0)
    enabled: bool = Field(default=True)
    thread_id: Optional[str] = Field(default=None, description="Bind to existing thread instead of creating trigger thread")


class TriggerUpdateRequest(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    source_config: Optional[dict] = None
    action_type: Optional[
        Literal["agent_prompt", "notify", "create_todo", "run_workflow"]
    ] = None
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
# Webhook fire helpers (module level so the dispatch body is unit-testable)
# ---------------------------------------------------------------------------

def _resolve_webhook_trigger(
    manager: TriggerManager, trigger_id: str, secret: Optional[str]
) -> tuple[str, TriggerDefinition]:
    """Resolve ``(owner_id, trigger)`` for a public webhook fire by shared secret.

    Used by the unauthenticated ``/fire/{trigger_id}`` path (external services
    that present the per-trigger shared secret rather than a bearer token).

    Raises:
        HTTPException: 500 if the webhook source is unavailable, 404 if no
            webhook trigger has that id, 403 if the secret matches none of the
            candidates, 409 if the id is ambiguous across owners.
    """
    from .sources import get_source

    source = get_source("webhook")
    if not source or not hasattr(source, "validate_secret"):
        raise HTTPException(status_code=500, detail="Webhook source unavailable")

    candidates = [
        (owner_id, candidate)
        for owner_id, candidate in manager.find_triggers_by_id(trigger_id)
        if candidate.source_type == "webhook"
    ]
    if not candidates:
        raise HTTPException(status_code=404, detail="Trigger not found")

    matches = [
        (owner_id, candidate)
        for owner_id, candidate in candidates
        if source.validate_secret(candidate.source_config, secret)
    ]
    if not matches:
        raise HTTPException(status_code=403, detail="Invalid or missing webhook secret")
    if len(matches) > 1:
        raise HTTPException(
            status_code=409,
            detail="Ambiguous webhook trigger ID; authenticate to fire this trigger",
        )
    return matches[0]


def _dispatch_trigger_fire(
    *,
    settings: Settings,
    manager: TriggerManager,
    user_id: str,
    thread_id: str,
    prompt: str,
    trigger_id: str,
    trigger_name: str,
    action_type: str,
    event_summary: str,
) -> None:
    """Fire a trigger by posting back into this process over HTTP.

    Runs on a fire-and-forget daemon thread launched by ``fire_trigger``.
    Routes through ``POST /chat`` with ``is_self_invoke=True`` so the
    autonomous event publishing uses the same proven path as the
    watchdog/ticker, peeks the SSE stream for a ``prompt_queued`` event to
    record ``status="queued"`` when the target thread was busy, and always logs
    a ``TriggerExecution``.

    A fresh ``httpx.Client`` is used deliberately: it hard-codes
    ``http://localhost:{api_port}`` and the admin service token, so it cannot
    ride ``NymeriaAPIClient``'s token-refresh logic. This mirrors the
    watchdog/ticker autonomous path and is intentional, not an oversight.
    """
    import time as _time

    import httpx

    start = _time.monotonic()
    execution = TriggerExecution(
        trigger_id=trigger_id,
        trigger_name=trigger_name,
        event_count=1,
        events_summary=event_summary,
        action_type=action_type,
    )
    service_token = settings.nymeria_service_token
    if not service_token:
        logger.error(
            "Trigger %s (%s) cannot fire: NYMERIA_SERVICE_TOKEN is not "
            "set. Trigger fires authenticate as the admin service "
            "account and act-as the trigger's user; configure the "
            "token in .env / .env.docker.",
            trigger_id, trigger_name,
        )
        return

    was_queued = False
    try:
        with httpx.Client(timeout=300) as client:
            with client.stream(
                "POST",
                f"http://localhost:{settings.api_port}/chat",
                headers={
                    "Authorization": f"Bearer {service_token}",
                    "X-Nymeria-Act-As": user_id,
                },
                json={
                    "message": prompt,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "is_self_invoke": True,
                    "trigger_override": "trigger",
                    "trigger_id": trigger_id,
                    "trigger_name": trigger_name,
                    # Source attribution so the sub-turn pending-prompt
                    # queue tags this fire as a trigger (not a user
                    # prompt) when the target thread is busy. Required
                    # for queued-prompt header rendering and execution
                    # log status=queued reporting.
                    "source": "trigger",
                    "source_id": trigger_id,
                    "source_label": trigger_name,
                },
            ) as resp:
                resp.raise_for_status()
                # Peek the SSE stream for a ``prompt_queued`` event so
                # we can record execution.status="queued" when the
                # target thread was busy. Body content is otherwise
                # discarded.
                for line in resp.iter_lines():
                    if was_queued:
                        continue
                    evt = parse_sse_data_line(line)
                    if isinstance(evt, dict) and evt.get("type") == "prompt_queued":
                        was_queued = True
        elapsed = _time.monotonic() - start
        execution.status = "queued" if was_queued else "success"
        logger.info(
            f"[TRIGGER] Fired via /chat: trigger={trigger_name} ({trigger_id}), "
            f"thread={thread_id}, elapsed={elapsed:.1f}s, "
            f"status={execution.status}"
        )
    except Exception as e:
        execution.status = "error"
        execution.error_message = str(e)[:200]
        logger.error(f"Trigger fire failed for {trigger_id}: {e}", exc_info=True)
    finally:
        execution.duration_seconds = round(_time.monotonic() - start, 2)
        manager.log_execution(user_id, execution)


def _run_workflow_binding_error(action_config: dict) -> Optional[str]:
    """Bind-time validation for run_workflow trigger actions (REST path)."""
    from ..core.workflows.tool_runtime import workflow_binding_error

    workflow_id = str((action_config or {}).get("workflow_id") or "").strip()
    if not workflow_id:
        return "run_workflow requires 'workflow_id' in action_config"
    params = (action_config or {}).get("params") or {}
    if not isinstance(params, dict):
        return "run_workflow 'params' must be a dict"
    return workflow_binding_error(workflow_id, params, allow_event=True)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def create_trigger_router(
    get_agent_fn,
    verify_api_key_fn,
    require_thread_access_fn=None,
) -> APIRouter:
    """Create the trigger system router.

    Args:
        get_agent_fn: Callable returning the global NymeriaAgent.
        verify_api_key_fn: FastAPI dependency for API key verification.
        require_thread_access_fn: Optional callable ``(user, thread_id)`` that
            raises HTTPException if ``user`` cannot legitimately bind a
            trigger to ``thread_id``. Required to prevent webhook triggers
            from claim-jacking shared-channel threads or guessing
            personal-thread IDs (the ``/triggers/fire`` path posts to
            ``/chat`` with the admin service token + Act-As, which would
            otherwise satisfy ``_require_thread_access`` for any thread).

    Returns:
        Configured APIRouter with trigger CRUD + fire endpoints.
    """
    router = APIRouter(prefix="/triggers", tags=["Triggers"])

    # Cached per-router: TriggerManager loads/watches files on construction,
    # so we build it once instead of on every request.
    _manager: Optional[TriggerManager] = None

    def _get_manager() -> TriggerManager:
        nonlocal _manager
        if _manager is None:
            settings = get_settings()
            _manager = TriggerManager(settings.data_dir)
        return _manager

    async def _optional_authenticated_user(
        request: Request,
        authorization: Optional[str] = Header(default=None),
        x_nymeria_act_as: Optional[str] = Header(default=None),
        settings=Depends(get_settings),
    ) -> Optional[AuthenticatedUser]:
        """Return an authenticated user when a token is supplied.

        Webhook fires are intentionally public for external services, so
        missing auth is allowed here. Invalid auth should still fail instead
        of silently falling back to shared-secret auth.
        """
        if authorization is None:
            return None

        maybe_user = verify_api_key_fn(
            request=request,
            authorization=authorization,
            x_nymeria_act_as=x_nymeria_act_as,
            settings=settings,
        )
        if inspect.isawaitable(maybe_user):
            maybe_user = await maybe_user
        return maybe_user

    # -- CRUD endpoints ---------------------------------------------------

    @router.get("", response_model=List[TriggerResponse])
    async def list_triggers(
        user_id: str = Query(default="default"),
        enabled_only: bool = Query(default=False),
        thread_id: Optional[str] = Query(default=None),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """List all triggers for a user, optionally filtered by thread."""
        user_id = user.id  # Override any client-claimed ?user_id=
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
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Create a new trigger."""
        user_id = user.id  # Override any client-claimed ?user_id=
        # If the trigger is bound to an existing thread, the caller must
        # legitimately own that thread — otherwise a linked non-admin user
        # could create a webhook trigger pointing at a guessed
        # ``discord_<g>_<c>`` / ``telegram_-<id>`` / ``twitch_<c>`` ID and
        # later use the unauthenticated ``/triggers/fire/{id}`` endpoint to
        # inject prompts via the admin service token + Act-As (which the
        # downstream ``/chat`` route honors for shared channels).
        if body.thread_id and require_thread_access_fn is not None:
            require_thread_access_fn(user, body.thread_id)
        manager = _get_manager()
        if body.action_type == "run_workflow":
            binding_error = _run_workflow_binding_error(body.action_config)
            if binding_error:
                raise HTTPException(status_code=400, detail=binding_error)
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
            refreshed = manager.get_trigger(user_id, trigger.id)
            if refreshed is not None:
                trigger = refreshed

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
                logger.warning("Failed to upsert thread metadata for new trigger", exc_info=True)

        return TriggerResponse.from_definition(trigger)

    @router.get("/sources/list")
    async def list_sources(user: AuthenticatedUser = Depends(verify_api_key_fn)):
        """List available trigger source types with enriched metadata."""
        from .sources import list_sources as _list
        return {"sources": _list()}

    @router.post("/sources/reload")
    async def reload_sources(user: AuthenticatedUser = Depends(verify_api_key_fn)):
        """Reload trigger source plugins from disk."""
        from .sources import reload_sources as _reload
        count = _reload()
        return {"sources_loaded": count}

    @router.get("/executions/recent")
    async def get_recent_executions(
        user_id: str = Query(default="default"),
        limit: int = Query(default=50, ge=1, le=200),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Get recent trigger executions across all triggers."""
        user_id = user.id  # Override any client-claimed ?user_id=
        manager = _get_manager()
        return manager.get_executions(user_id, limit=limit)

    @router.get("/{trigger_id}", response_model=TriggerResponse)
    async def get_trigger(
        trigger_id: str,
        user_id: str = Query(default="default"),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Get a single trigger by ID."""
        user_id = user.id  # Override any client-claimed ?user_id=
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
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Update a trigger's configuration."""
        user_id = user.id  # Override any client-claimed ?user_id=
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
            if new_type == "run_workflow":
                binding_error = _run_workflow_binding_error(new_config)
                if binding_error:
                    raise HTTPException(status_code=400, detail=binding_error)
            kwargs["action"] = TriggerAction(type=new_type, config=new_config)

        if not kwargs:
            raise HTTPException(status_code=400, detail="No updates provided")

        ok = manager.update_trigger(user_id, trigger_id, **kwargs)
        if not ok:
            raise HTTPException(status_code=404, detail="Trigger not found")

        trigger = manager.get_trigger(user_id, trigger_id)
        if trigger is None:
            raise HTTPException(status_code=404, detail="Trigger not found after update")
        return TriggerResponse.from_definition(trigger)

    @router.delete("/{trigger_id}", status_code=204)
    async def delete_trigger(
        trigger_id: str,
        user_id: str = Query(default="default"),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Delete a trigger and clean up its thread metadata."""
        user_id = user.id  # Override any client-claimed ?user_id=
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
                logger.warning("Failed to clean up orphaned trigger thread metadata", exc_info=True)

    @router.get("/{trigger_id}/executions")
    async def get_trigger_executions(
        trigger_id: str,
        user_id: str = Query(default="default"),
        limit: int = Query(default=50, ge=1, le=200),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Get execution history for a specific trigger."""
        user_id = user.id  # Override any client-claimed ?user_id=
        manager = _get_manager()
        return manager.get_executions(user_id, trigger_id=trigger_id, limit=limit)

    @router.post("/{trigger_id}/test")
    async def test_trigger(
        trigger_id: str,
        user_id: str = Query(default="default"),
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Test a trigger with sample event data (dry run, no execution)."""
        user_id = user.id  # Override any client-claimed ?user_id=
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
            "fired_at": utc_now().isoformat(),
        }

        action = trigger.action
        if action.type == "agent_prompt":
            template = action.config.get("prompt_template") or action.config.get("prompt") or ""
            rendered = _safe_format(template, template_vars)
        elif action.type == "notify":
            rendered = _safe_format(action.config.get("message_template", ""), template_vars)
        elif action.type == "create_todo":
            rendered = _safe_format(action.config.get("task_template", ""), template_vars)
        elif action.type == "run_workflow":
            rendered = (
                f"run workflow '{action.config.get('workflow_id', '?')}' "
                f"(dry run only, nothing executed)"
            )
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
        auth_user: Optional[AuthenticatedUser] = Depends(_optional_authenticated_user),
    ):
        """Fire a webhook trigger.

        External services authenticate with the per-trigger shared secret.
        Authenticated API callers may fire their own webhook triggers without
        putting that secret in the URL.
        """
        manager = _get_manager()
        if auth_user is not None:
            user_id = auth_user.id
            trigger = manager.get_trigger(user_id, trigger_id)
            if trigger is None:
                raise HTTPException(status_code=404, detail="Trigger not found")
        else:
            user_id, trigger = _resolve_webhook_trigger(manager, trigger_id, secret)

        if not trigger.enabled:
            raise HTTPException(status_code=409, detail="Trigger is disabled")

        if trigger.source_type != "webhook":
            raise HTTPException(status_code=400, detail="Trigger is not a webhook source")

        # Parse request body
        try:
            body = await request.json()
        except Exception:
            body = {}

        event = {
            **body,
            "fired_at": utc_now().isoformat(),
            "source_ip": request.client.host if request.client else "unknown",
        }

        # Atomic cooldown check + fire_count increment. Prevents two
        # concurrent webhook hits from both passing the cooldown check or
        # both computing fire_count=N+1 from the same snapshot and losing
        # an increment.
        with manager.atomic_update(user_id) as store:
            live_trigger = store.get_trigger(trigger_id)
            if live_trigger is None:
                raise HTTPException(status_code=404, detail="Trigger not found")
            now = utc_now()
            if live_trigger.cooldown_seconds and live_trigger.last_fired:
                elapsed = (
                    now - ensure_aware_utc(live_trigger.last_fired)
                ).total_seconds()
                if elapsed < live_trigger.cooldown_seconds:
                    remaining = int(live_trigger.cooldown_seconds - elapsed)
                    raise HTTPException(
                        status_code=429,
                        detail=f"Cooldown active. Retry in {remaining}s.",
                    )
            live_trigger.last_fired = now
            live_trigger.fire_count += 1
            trigger_name = live_trigger.name
            action_config = dict(live_trigger.action.config)
            action_type = live_trigger.action.type
            thread_id = live_trigger.thread_id or f"trigger-{trigger_id}"
            fired_trigger = live_trigger.model_copy(deep=True)

        import threading

        # Non-agent actions (notify / create_todo / run_workflow) execute
        # through fire_action's type dispatch, exactly like the poll path.
        # Historically EVERY webhook fire was rendered into a prompt and
        # relayed to /chat as an agent turn regardless of its action type;
        # that bypass is the routing bug this branch closes. fire_action logs
        # its own TriggerExecution, so nothing is double-logged here.
        if action_type != "agent_prompt":
            from ..core.turn_executor import LocalAgentExecutor

            executor = LocalAgentExecutor(get_agent_fn())
            threading.Thread(
                target=manager.fire_action,
                args=(fired_trigger, event, executor, user_id),
                name=f"trigger-fire-{trigger_id}",
                daemon=True,
            ).start()
            return {
                "status": "fired",
                "trigger_id": trigger_id,
                "trigger_name": trigger_name,
                "action_type": action_type,
            }

        # Route through POST /chat with is_self_invoke=True so the
        # autonomous event publishing uses the same proven path as the
        # watchdog/ticker.  This ensures the frontend receives streaming
        # events through the exact same code that TODO streaming uses.
        settings = get_settings()

        template = (
            action_config.get("prompt_template")
            or action_config.get("prompt")
            or "Trigger {trigger_name} fired."
        )
        template_vars = {
            **event,
            "trigger_id": trigger_id,
            "trigger_name": trigger_name,
        }
        prompt = _safe_format(template, template_vars)

        threading.Thread(
            target=_dispatch_trigger_fire,
            kwargs=dict(
                settings=settings,
                manager=manager,
                user_id=user_id,
                thread_id=thread_id,
                prompt=prompt,
                trigger_id=trigger_id,
                trigger_name=trigger_name,
                action_type=action_type,
                event_summary=str(event)[:200],
            ),
            name=f"trigger-fire-{trigger_id}",
            daemon=True,
        ).start()

        return {
            "status": "fired",
            "trigger_id": trigger_id,
            "trigger_name": trigger_name,
            "action_type": action_type,
        }

    return router
