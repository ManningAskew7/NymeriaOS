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
    "inject_context", "block_if_matches", "rewrite_arg", "require_approval",
    "notify", "create_todo", "webhook", "run_command", "run_workflow",
]


class HookCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    event: HookEventName
    action: HookActionName = Field(default="inject_context")  # type: ignore[bad-assignment]
    # Per-action params (flat; only the fields for `action` are read).
    text: Optional[str] = Field(
        default=None, max_length=10_000,
        description="inject_context/notify/create_todo/webhook body; require_approval prompt",
    )
    conditions: Optional[List[HookCondition]] = Field(
        default=None,
        description="block_if_matches / rewrite_arg / require_approval gate (matches tool args)",
    )
    reason: Optional[str] = Field(default=None, max_length=500, description="block_if_matches")
    updates: Optional[Dict[str, str]] = Field(default=None, description="rewrite_arg")
    url: Optional[str] = Field(default=None, max_length=2_000, description="webhook target URL")
    command: Optional[str] = Field(
        default=None, max_length=4_000, description="run_command shell command (admin + flag gated)"
    )
    workflow_id: Optional[str] = Field(
        default=None, max_length=200,
        description="run_workflow: published workflow tool id (binding validated at authoring)",
    )
    workflow_params: Optional[dict] = Field(
        default=None,
        description="run_workflow: static bound params (per-fire dynamics ride the 'event' param)",
    )
    on_fault: Optional[Literal["allow", "deny"]] = Field(
        default=None,
        description="run_workflow, pre_tool_use only: proceed or fail closed when the workflow faults",
    )
    timeout_seconds: Optional[float] = Field(
        default=None, ge=1.0, le=600.0,
        description=(
            "run_command subprocess budget (1..300), require_approval window "
            "(10..600), or run_workflow wall clock (5..600); each action's "
            "logic model enforces its own bounds"
        ),
    )
    matcher: Optional[str] = Field(default=None, description="Tool-name filter (tool events)")
    fire_conditions: Optional[List[HookCondition]] = Field(
        default=None,
        description=(
            "Definition-level fire gate (any event/action), evaluated before "
            "the logic runs: meta fields, args.* tool args, context-usage "
            "numbers (context_tokens/context_pct_of_trigger/...); empty = "
            "always fire"
        ),
    )
    once: bool = Field(
        default=False,
        description=(
            "Fire once per gate crossing; re-arms when fire_conditions stop "
            "matching"
        ),
    )
    single_use: bool = Field(
        default=False,
        description="Delete the hook after its first successful run (log kept)",
    )
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
    command: Optional[str] = None
    workflow_id: Optional[str] = None
    workflow_params: Optional[dict] = None
    on_fault: Optional[Literal["allow", "deny"]] = None
    timeout_seconds: Optional[float] = None
    matcher: Optional[str] = None
    fire_conditions: Optional[List[HookCondition]] = None
    once: Optional[bool] = None
    single_use: Optional[bool] = None
    enabled: Optional[bool] = None
    # Note: no `scope`/`thread_id` here. Re-scoping a hook to a thread needs a
    # thread_id (and its access gate), which a partial PATCH cannot supply
    # without silently binding to "" (an inert orphan); a re-scope is a
    # delete + create.


class HookApprovalResolveRequest(BaseModel):
    """Approve or deny a held tool call (require_approval hook)."""

    approved: bool
    note: Optional[str] = Field(default=None, max_length=500)


class HookTemplateInstallRequest(BaseModel):
    """Install a bundled hook template (all overrides optional)."""

    scope: Optional[Literal["global", "thread"]] = Field(
        default=None, description="Override the template's default scope"
    )
    thread_id: Optional[str] = Field(
        default=None, description="Thread binding (required when scope is 'thread')"
    )
    text: Optional[str] = Field(
        default=None, max_length=10_000,
        description="Override the template's text body / approval prompt",
    )
    enabled: Optional[bool] = Field(default=None)


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
    fire_conditions: List[dict] = Field(default_factory=list)
    once: bool = False
    single_use: bool = False
    enabled: bool
    scope: str
    thread_id: str = ""
    template: str = ""
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
            fire_conditions=[c.model_dump() for c in h.fire_conditions],
            once=h.once,
            single_use=h.single_use,
            enabled=h.enabled,
            scope=h.scope,
            thread_id=h.thread_id,
            template=h.template,
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

    def _reject_gated_action(action: str, user: AuthenticatedUser) -> None:
        """403/400 if the caller may not author a gated action (run_command)."""
        from ...core.hook_manager import run_command_authoring_error
        reason = run_command_authoring_error(action, is_admin=user.role == "admin")
        if reason is None:
            return
        # Match run_command_authoring_error's precedence: the deployment flag is
        # checked FIRST, so a flag-off deployment is a 400 (config) regardless of
        # role, and an enabled-but-not-admin caller is a 403 (authz). Deriving the
        # status from role alone would 403 a non-admin with the flag-off message.
        flag_on = getattr(get_settings(), "hooks_run_command_enabled", False)
        status = 403 if (flag_on and user.role != "admin") else 400
        raise HTTPException(status_code=status, detail=reason)

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
        _reject_gated_action(body.action, user)
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
            command=body.command, timeout_seconds=body.timeout_seconds,
            workflow_id=body.workflow_id, workflow_params=body.workflow_params,
            on_fault=body.on_fault,
        )
        try:
            hook = _get_manager().add_hook(
                user_id,
                name=body.name,
                event=body.event,
                action=body.action,
                params=params,
                matcher=body.matcher,
                fire_conditions=body.fire_conditions,
                once=body.once,
                single_use=body.single_use,
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
        from ...core.hook_manager import GATED_ACTIONS, HOOK_LOGIC_BY_ACTION, HookStore
        from ...core.hook_spec import (
            ACTION_SPECS,
            EVENTS,
            TOOL_EVENTS,
            event_actions,
            plane_by_event,
        )

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
                # Full event -> plane map (run_command flips per event; every
                # other action reports its single plane for every legal event).
                "plane_by_event": plane_by_event(name),
                "events": list(spec.events),
                "text_action": spec.text_action,
                # True when authoring is admin + HOOKS_RUN_COMMAND_ENABLED gated.
                "gated": name in GATED_ACTIONS,
                "params_schema": schema,
            }
        from ...core.hooks.bridge import (
            FIRE_CONDITION_CONTEXT_FIELDS,
            FIRE_CONDITION_META_FIELDS,
        )

        return {
            "events": {
                e: {"tool_event": e in TOOL_EVENTS, "actions": sorted(legality[e])}
                for e in EVENTS
            },
            "actions": actions,
            "operators": list(get_args(ConditionOperator)),
            # Definition-level fire gate (any event/action): fire_conditions
            # evaluate against these fields (tool args under "args.<name>";
            # numeric context fields take the gt/gte/lt/lte operators), and
            # `once` fires once per gate crossing with automatic re-arm.
            "fire_gate": {
                "fields": ["fire_conditions", "once"],
                "meta_fields": list(FIRE_CONDITION_META_FIELDS),
                "context_fields": list(FIRE_CONDITION_CONTEXT_FIELDS),
                "args_prefix": "args.",
            },
            # Definition lifecycle fields (distinct from the fire gate):
            # single_use deletes the hook after its first successful run.
            "lifecycle": {"fields": ["single_use"]},
            "max_hooks": HookStore.model_fields["MAX_HOOKS"].default,
        }

    @router.get("/approvals")
    async def list_hook_approvals(
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Pending ``require_approval`` holds (admins all, others own-only).

        Each entry is a held tool call awaiting the user's decision; resolve
        with ``POST /hooks/approvals/{record_id}/resolve`` before its
        ``expires_at`` (no answer within the window denies the call).
        """
        from ...core.hook_approvals import list_pending, public_entry

        records = await run_in_threadpool(
            list_pending, None if user.role == "admin" else user.id
        )
        return {"approvals": [public_entry(r) for r in records]}

    @router.post("/approvals/{record_id}/resolve")
    async def resolve_hook_approval(
        record_id: str,
        body: HookApprovalResolveRequest,
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Approve or deny a held tool call (owner or admin).

        404 covers both a missing record and another user's record (existence
        is not leaked). 409 means the hold is already over: the decision lost
        a race, the window expired, or the waiter is gone (restart/abort);
        stale records are cleaned up on the spot.
        """
        from ...core.hook_approvals import (
            delete_record,
            get_hook_approval_coordinator,
            load_record,
            publish_resolved_event,
        )

        record = await run_in_threadpool(load_record, record_id)
        if record is None or (
            user.role != "admin" and record.get("user_id") != user.id
        ):
            raise HTTPException(status_code=404, detail="Approval not found")
        woke = get_hook_approval_coordinator().resolve(
            record_id,
            approved=body.approved,
            resolved_by=user.id,
            note=body.note or "",
        )
        if not woke:
            # Nothing is waiting: the hold already ended (timeout/abort race)
            # or the waiter died with the record behind (restart). Clean up so
            # the stale row disappears from every surface.
            await run_in_threadpool(delete_record, record_id)
            publish_resolved_event(record, outcome="stale", resolved_by=user.id)
            raise HTTPException(
                status_code=409,
                detail="This approval is no longer pending (it timed out, was "
                "resolved elsewhere, or its turn ended).",
            )
        return {
            "ok": True,
            "record_id": record_id,
            "decision": "approved" if body.approved else "denied",
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

    @router.get("/templates")
    async def list_hook_templates(
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """The bundled hook-template catalog (install via the sibling POST)."""
        from ...core.hook_templates import load_templates

        templates = await run_in_threadpool(load_templates)
        return {
            "templates": [
                {
                    "id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "notes": t.notes,
                    "hook": t.hook,
                }
                for t in templates
            ]
        }

    @router.post("/templates/{template_id}/install")
    async def install_hook_template(
        template_id: str,
        body: HookTemplateInstallRequest,
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Install a bundled template as a real hook for the caller.

        Idempotent: reinstalling a template already installed with the same
        scope binding returns the existing hook with ``created: false``.
        """
        from ...core.hook_templates import install_template

        scope = body.scope
        # The binding is used whenever the EFFECTIVE scope (override or the
        # template's default) is thread-scoped, so access-check it whenever
        # the caller supplies one.
        thread_id = body.thread_id
        if scope == "thread" and not thread_id:
            raise HTTPException(
                status_code=400,
                detail="A thread-scoped install requires a thread_id.",
            )
        if thread_id and require_thread_access_fn is not None:
            require_thread_access_fn(user, thread_id)
        try:
            hook, created = await run_in_threadpool(
                lambda: install_template(
                    _get_manager(),
                    user.id,
                    template_id,
                    scope=scope,
                    thread_id=thread_id,
                    text=body.text,
                    enabled=body.enabled,
                    created_by="user",
                    is_admin=user.role == "admin",
                )
            )
        except (ValidationError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e))
        if hook is None:
            raise HTTPException(status_code=400, detail="Hook limit reached for this user.")
        return {"created": created, "hook": HookResponse.from_definition(hook)}

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
        # Guard a switch TO a gated action AND any behavior edit of an existing
        # gated hook (admin + deployment flag). Enabled/name-only updates stay
        # ungated (toggling never changes what the hook executes); switching
        # away from a gated action is privilege-reducing and stays ungated.
        from ...core.hook_manager import gated_update_action
        touched = set(body.model_dump(exclude_none=True)) - {"action"}
        gate_on = gated_update_action(existing.logic.action, body.action, touched)
        if gate_on is not None:
            _reject_gated_action(gate_on, user)
        # Plain field updates (name/event/matcher/enabled); the logic fields
        # (text/conditions/reason/updates/url/command/action) are assembled
        # separately by ``build_update_kwargs`` so a partial PATCH keeps
        # unspecified siblings.
        scalars = body.model_dump(
            exclude_none=True,
            exclude={
                "action", "conditions", "reason", "updates", "text", "url",
                "command", "timeout_seconds",
                "workflow_id", "workflow_params", "on_fault",
            },
        )
        updates = build_update_kwargs(
            existing, action=body.action, text=body.text, conditions=body.conditions,
            reason=body.reason, updates=body.updates, url=body.url,
            command=body.command, timeout_seconds=body.timeout_seconds,
            workflow_id=body.workflow_id, workflow_params=body.workflow_params,
            on_fault=body.on_fault, scalars=scalars,
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
        elif logic.action == "require_approval":
            gate = (
                " AND ".join(f"{c.field} {c.operator} {c.value!r}" for c in logic.conditions)
                if logic.conditions else "always"
            )
            prompt = safe_format(
                logic.prompt or "Approve tool call {tool_name}?", sample
            )
            result["rendered"] = (
                f"Holds {hook.matcher or 'any tool'} ({gate}) and asks: {prompt!r} "
                f"(window {logic.timeout_seconds:.0f}s; no answer = deny). "
                "No call is held by this preview."
            )
        elif logic.action == "run_command":
            from ...core.hook_spec import plane_for
            plane = plane_for("run_command", hook.event)
            result["rendered"] = (
                f"Runs {logic.command!r} ({plane} plane, timeout {logic.timeout_seconds}s) "
                f"with the hook context as JSON on stdin. No command is executed by this preview."
            )
        elif logic.action == "run_workflow":
            from ...core.hook_spec import plane_for
            plane = plane_for("run_workflow", hook.event)
            fault = f", on_fault={logic.on_fault}" if hook.event == "pre_tool_use" else ""
            result["rendered"] = (
                f"Runs workflow {logic.workflow_id!r} ({plane} plane, wall clock "
                f"{logic.timeout_seconds:.0f}s{fault}); the hook context rides the "
                "workflow's 'event' parameter when declared. No workflow is run by "
                "this preview."
            )
        return result

    return router
