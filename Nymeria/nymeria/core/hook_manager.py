"""Lifecycle-hook definitions: the persisted record + per-user store.

A ``HookDefinition`` is a user- or agent-authored record ("when this event
fires, run this logic"). This module owns ONLY the record model, its
validation, and JSON persistence, mirroring ``TriggerManager`` (one file per
user under ``data_dir/hooks/``). It stays deliberately store-only: it does not
import the hooks engine (``core/hooks/``) or the agent. The bridge
(``core/hooks/bridge.py``) turns an enabled definition into an active dispatch
hook; the enable resolver lives in ``core/agent_safety.py``.

``HookLogic`` is a discriminated union on ``action``: ``inject_context`` (inject
a string on prompt_submit/post_tool_use/done), plus the ``pre_tool_use``
guardrail actions ``block_if_matches`` (deny a tool call) and ``rewrite_arg``
(modify its args). The ``EVENT_ACTIONS`` map gates which actions attach to which
event; the ``ACTIONS``/``ACTION_PLANES`` tables in ``core/hooks/actions.py`` map
each action to its runtime function and plane.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Dict, Iterable, List, Literal, Optional, Set, Tuple, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .conditions import HookCondition
from .hook_spec import (
    MATCHER_EVENTS,
    event_actions,
    system_actions,
    system_event_actions,
    text_actions,
)
from .keyed_locks import KeyedRLockMap
from .prompts import DEFAULT_TURN_METADATA_TEMPLATE, TURN_METADATA_TEMPLATE_PATTERN
from .storage_paths import (
    FileFingerprint,
    compare_fingerprint,
    quarantine_corrupt_file,
    read_store_fingerprint,
    record_store_fingerprint,
    upgrade_legacy_store_fingerprint,
    safe_path_segment,
    write_text_atomic,
)
from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

# Thread-safe locks keyed by user_id.
_hook_locks = KeyedRLockMap()

# Execution-log cap per user (matches the trigger execution log).
MAX_HOOK_EXECUTION_LOG = 200

# Single-worker write-behind executor for the execution log. Hooks fire at
# per-tool-call frequency (a PRE guardrail runs inside every tool call), so the
# in-band cost of recording must be a list append + submit, never file I/O; the
# worker drains a user's pending entries in one coalesced read-modify-write.
# One worker also serializes every log-file write (flush, read-barrier, purge),
# so there is no multi-writer race on the file.
_log_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hook-exec-log")

# The events an action may attach to, and the actions legal for each, both
# derived from the ``core/hook_spec.py`` single source (which mirrors
# ``core/hooks/base.py`` EVENT_OUTCOME_TYPES; ``tests/test_hook_spec.py`` pins
# the lockstep). ``pre_tool_use`` carries the mutate-plane guardrail actions
# (deny/rewrite); the observe-plane actions (notify/create_todo/webhook) attach
# to the "after something happened" events (post_tool_use/done).
HookEventName = Literal[
    "prompt_submit", "pre_tool_use", "post_tool_use", "done", "command_submit"
]
EVENT_ACTIONS: Dict[str, set] = event_actions()

# Actions whose sole logic config is a single ``text`` field (a bare ``text`` is
# a convenience alias for ``params={"text": ...}``). Shared by every authoring
# surface via ``params_from_fields``.
TEXT_ACTIONS = text_actions()

# Actions gated behind an admin account AND a deployment opt-in flag, because
# authoring one is remote code execution on the backend host. Shared by every
# authoring surface via ``run_command_authoring_error``.
GATED_ACTIONS = frozenset({"run_command"})

# Actions reserved for built-in system hook definitions (backlog #66): users
# can never create a hook with one or switch an existing hook to one; the
# system definitions themselves validate against SYSTEM_EVENT_ACTIONS.
SYSTEM_ACTIONS = frozenset(system_actions())
_SYSTEM_EVENT_ACTIONS: Dict[str, set] = system_event_actions()

# The reserved id of the system turn-metadata hook. Human-readable and
# collision-free by construction (add_hook generates 8-char hex ids). The
# definition is VIRTUAL until edited: get_hooks/get_hook synthesize the
# built-in default when no stored record carries this id, update_hook
# materializes it copy-on-write, and delete_hook resets it to defaults.
SYSTEM_TURN_METADATA_ID = "turn-metadata"
SYSTEM_HOOK_IDS = frozenset({SYSTEM_TURN_METADATA_ID})

# Fields locked on a system hook definition: its identity (event/action) and
# binding (scope/thread) are fixed, and single_use would let one successful
# run self-delete (reset) it, which is nonsensical for a standing system
# block. Editable: name, enabled, text (template), fire_conditions, once.
_SYSTEM_LOCKED_FIELDS = frozenset({"event", "scope", "thread_id", "single_use"})


def run_command_authoring_error(action: str, *, is_admin: Optional[bool]) -> Optional[str]:
    """Reason a gated action may not be authored, or ``None`` if allowed.

    Two conditions, both required for a gated action: the deployment enabled it
    (``HOOKS_RUN_COMMAND_ENABLED``) AND the caller is an admin. Distinct copy
    per failure so the surface can 403 vs 400 appropriately. ``is_admin`` is the
    already-resolved caller privilege (each surface resolves it its own way);
    ``None`` is treated as trusted (a local no-account CLI/agent context).
    """
    if action not in GATED_ACTIONS:
        return None
    from ..config import get_settings
    if not getattr(get_settings(), "hooks_run_command_enabled", False):
        return (
            f"The '{action}' action is disabled on this deployment. An operator "
            "must set HOOKS_RUN_COMMAND_ENABLED=true to allow it."
        )
    if is_admin is False:
        return f"The '{action}' action is admin-only (it runs shell commands on the host)."
    return None


def run_workflow_authoring_error(params: Optional[dict]) -> Optional[str]:
    """Reason a ``run_workflow`` logic binding is unsound, or ``None`` if fine.

    Bind-time validation shared by every authoring surface via
    ``add_hook``/``update_hook`` (the single chokepoint), mirroring the
    trigger surfaces' ``run_workflow`` action validation: the workflow must
    exist as a published workflow tool, its current revision must be
    approved, the bound params must all be declared, and every required
    parameter must be covered (``event`` counts as covered because the fire
    point supplies it when declared). Execution re-gates regardless, so a
    store-file-planted record referencing an unapproved workflow stays inert;
    this catches misconfiguration where it is authored. Function-local import
    keeps this store-only module free of the workflows engine at import time.
    """
    params = params or {}
    workflow_id = str(params.get("workflow_id") or "").strip()
    if not workflow_id:
        return "run_workflow requires 'workflow_id'."
    bound = params.get("params") or {}
    if not isinstance(bound, dict):
        return "run_workflow 'params' must be a dict."
    from .workflows.tool_runtime import workflow_binding_error
    return workflow_binding_error(workflow_id, bound, allow_event=True)


# Update fields a caller may touch on an existing gated hook WITHOUT passing
# the gate: toggling or renaming never changes what the hook executes, and the
# per-hook enable switch is a first-class product surface (GUI toggles).
UNGATED_UPDATE_FIELDS = frozenset({"enabled", "name"})


def gated_update_action(
    existing_action: Optional[str],
    requested_action: Optional[str],
    touched_fields: Iterable[str],
) -> Optional[str]:
    """The action an update must be gate-checked against, or ``None`` if free.

    Shared by every authoring surface so the update-gate rule cannot drift:
    a switch TO a gated action is always gated, and an in-place edit of an
    existing gated hook is gated unless it only touches
    ``UNGATED_UPDATE_FIELDS`` (without this, the create-time admin gate could
    be sidestepped by editing a stored run_command hook's command in place,
    e.g. by an owner whose admin role was later revoked). Switching AWAY from
    a gated action is privilege-reducing and stays ungated.
    """
    if requested_action is not None:
        return requested_action if requested_action in GATED_ACTIONS else None
    if existing_action in GATED_ACTIONS and set(touched_fields) - UNGATED_UPDATE_FIELDS:
        return existing_action
    return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class InjectContextLogic(BaseModel):
    """Render a (static or templated) string and inject it into context."""

    action: Literal["inject_context"] = "inject_context"
    text: str = Field(
        ...,
        min_length=1,
        max_length=10_000,
        description="Static or {placeholder} template text",
    )


class BlockIfMatchesLogic(BaseModel):
    """Deny a tool call (pre_tool_use) when all conditions match its args."""

    action: Literal["block_if_matches"] = "block_if_matches"
    conditions: List[HookCondition] = Field(
        default_factory=list,
        description="AND-ed filters against the tool args; empty = always deny",
    )
    reason: str = Field(
        default="",
        max_length=500,
        description="Shown to the model on deny ({placeholder} templated)",
    )


class RewriteArgLogic(BaseModel):
    """Rewrite one or more tool-call args (pre_tool_use) when conditions match."""

    action: Literal["rewrite_arg"] = "rewrite_arg"
    conditions: List[HookCondition] = Field(
        default_factory=list,
        description="AND-ed gate against the tool args; empty = always rewrite",
    )
    updates: Dict[str, str] = Field(
        default_factory=dict,
        description="arg-name -> {placeholder}-templated new value",
    )


class RequireApprovalLogic(BaseModel):
    """Hold a tool call (pre_tool_use) until the user approves or denies it.

    The matched call blocks in-band for up to ``timeout_seconds`` (the
    approval window, an author-side field, never agent-chosen); no answer
    within the window is a DENY with a hardened no-consent message to the
    model. Resolve surfaces: REST, the ``/hook approve|deny`` command, the
    desktop/mobile tool-call card, chat-platform buttons, and the CLI form.
    Not admin-gated: an approval hook only holds its owner's own tool calls.
    """

    action: Literal["require_approval"] = "require_approval"
    conditions: List[HookCondition] = Field(
        default_factory=list,
        description="AND-ed gate against the tool args; empty = always ask",
    )
    prompt: str = Field(
        default="",
        max_length=500,
        description="Approval prompt shown to the user ({placeholder} templated)",
    )
    timeout_seconds: float = Field(
        default=180.0,
        ge=10.0,
        le=600.0,
        description="Approval window; no answer within it denies the call",
    )


class NotifyLogic(BaseModel):
    """Deliver an in-app + push notification (observe plane)."""

    action: Literal["notify"] = "notify"
    text: str = Field(
        ...,
        min_length=1,
        max_length=2_000,
        description="Notification text ({placeholder} templated)",
    )


class CreateTodoLogic(BaseModel):
    """Create a user TODO from the rendered text (observe plane)."""

    action: Literal["create_todo"] = "create_todo"
    text: str = Field(
        ...,
        min_length=1,
        max_length=2_000,
        description="TODO task text ({placeholder} templated)",
    )


class WebhookLogic(BaseModel):
    """POST a JSON payload to a URL (observe plane, SSRF-safe egress)."""

    action: Literal["webhook"] = "webhook"
    url: str = Field(..., min_length=1, max_length=2_000, description="POST target URL")
    text: str = Field(
        default="",
        max_length=10_000,
        description="Payload text -> {\"text\": ...} ({placeholder} templated)",
    )


class RunCommandLogic(BaseModel):
    """Run a shell command; its stdout drives the outcome (admin + flag gated).

    Per-event plane (see ``hook_spec.plane_for``): a mutate guardrail/injector on
    prompt_submit/pre_tool_use, a fire-and-forget side effect on
    post_tool_use/done. The hook context is passed to the process as JSON on
    stdin. Authoring is admin-only and requires ``HOOKS_RUN_COMMAND_ENABLED``;
    the action re-checks the flag at execution time.
    """

    action: Literal["run_command"] = "run_command"
    command: str = Field(
        ...,
        min_length=1,
        max_length=4_000,
        description="Shell command; receives the hook context as JSON on stdin",
    )
    timeout_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        description=(
            "Subprocess wall-clock budget. The action clamps mutate-plane events "
            "(prompt_submit/pre_tool_use/command_submit, which run in-band) to "
            "60s; observe events (post_tool_use/done, off-turn) keep the full "
            "range."
        ),
    )


class RunWorkflowLogic(BaseModel):
    """Run a saved nym workflow; its result drives the outcome (backlog #80).

    The workflow logic substrate: the hook's logic is a published,
    admin-approved workflow tool run out-of-process. Per-event plane mirrors
    ``run_command`` (mutate injector/guardrail on prompt_submit/pre_tool_use,
    fire-and-forget on post_tool_use/done). The hook context is passed as the
    workflow's ``event`` parameter when its signature declares one (the
    trigger ``run_workflow`` convention). Authoring is NOT admin-gated: the
    per-revision approval gate (recomputed at every execution) is the
    control, matching the trigger and scheduled-TODO firing surfaces; the
    binding itself is validated at authoring via
    ``run_workflow_authoring_error``.
    """

    action: Literal["run_workflow"] = "run_workflow"
    workflow_id: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Published workflow tool id to run",
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Static bound parameters (no {placeholder} templating; per-fire "
            "dynamics ride the 'event' parameter when the workflow declares it)"
        ),
    )
    timeout_seconds: float = Field(
        default=60.0,
        ge=5.0,
        le=600.0,
        description=(
            "Workflow wall-clock budget for this hook, both planes (the "
            "workflow's own configured wall clock still applies when lower)"
        ),
    )
    on_fault: Literal["allow", "deny"] = Field(
        default="allow",
        description=(
            "Guardrail events (pre_tool_use/command_submit) only: whether the "
            "gated tool call or command proceeds when the workflow faults "
            "(error/timeout/suspension). 'allow' surfaces a diagnostic note; "
            "'deny' fails closed"
        ),
    )


class TurnMetadataLogic(BaseModel):
    """Render the built-in ``[Time:]/[Trigger:]`` turn-metadata block (#66).

    The logic of the reserved system ``turn-metadata`` hook (system action:
    users cannot author another hook with it). ``text`` is the template,
    constrained to the fixed two-line frame so the history-strip regex in
    ``core/agent_history.py`` keeps matching whatever it emits: interiors are
    customizable ({time}/{trigger} plus the standard hook vars), the frame is
    not. The turn-entry seam additionally re-validates the RENDERED block
    against the strip pattern and falls back to the built-in block on a
    mismatch, so a placeholder that expands to ``]``/newlines at render time
    cannot leak metadata into compaction.
    """

    action: Literal["turn_metadata"] = "turn_metadata"
    text: str = Field(
        default=DEFAULT_TURN_METADATA_TEMPLATE,
        min_length=1,
        max_length=300,
        description=(
            "Turn-metadata template: exactly '[Time: ...]' newline "
            "'[Trigger: ...]' with customizable interiors (no ']' or "
            "newlines inside); {time} and {trigger} are substituted per turn"
        ),
    )

    @field_validator("text")
    @classmethod
    def _validate_frame(cls, value: str) -> str:
        if not TURN_METADATA_TEMPLATE_PATTERN.fullmatch(value):
            raise ValueError(
                "turn-metadata template must be exactly two lines, "
                "'[Time: <interior>]' then '[Trigger: <interior>]', with "
                "non-empty interiors containing no ']' and no newlines "
                "(placeholders like {time} and {trigger} are allowed)"
            )
        return value


# Discriminated union on ``action``. Store-compatible with legacy inject_context
# records ({"action":"inject_context","text":...}). Adding an action is a new
# variant here + an ``ACTIONS``/``ACTION_PLANES`` entry + an ``EVENT_ACTIONS``
# row. The type-alias name stays ``HookLogic`` so importers do not churn.
HookLogic = Annotated[
    Union[
        InjectContextLogic, BlockIfMatchesLogic, RewriteArgLogic,
        RequireApprovalLogic,
        NotifyLogic, CreateTodoLogic, WebhookLogic, RunCommandLogic,
        RunWorkflowLogic, TurnMetadataLogic,
    ],
    Field(discriminator="action"),
]

# Build a logic variant from an action name + a params dict (used by add_hook).
HOOK_LOGIC_BY_ACTION: Dict[str, type[BaseModel]] = {
    "inject_context": InjectContextLogic,
    "block_if_matches": BlockIfMatchesLogic,
    "rewrite_arg": RewriteArgLogic,
    "require_approval": RequireApprovalLogic,
    "notify": NotifyLogic,
    "create_todo": CreateTodoLogic,
    "webhook": WebhookLogic,
    "run_command": RunCommandLogic,
    "run_workflow": RunWorkflowLogic,
    "turn_metadata": TurnMetadataLogic,
}


def build_logic(action: str, params: Optional[dict]) -> BaseModel:
    """Construct the logic variant for ``action`` from ``params``.

    Raises ``ValueError`` on an unknown action or invalid params (callers map
    that to a human error string / HTTP 400).
    """
    model = HOOK_LOGIC_BY_ACTION.get(action)
    if model is None:
        raise ValueError(
            f"Unknown hook action {action!r}. Valid: {', '.join(sorted(HOOK_LOGIC_BY_ACTION))}."
        )
    try:
        return model(**(params or {}))
    except ValidationError as e:
        raise ValueError(str(e)) from e


class HookDefinition(BaseModel):
    """A single lifecycle hook created by the user or agent."""

    id: str = Field(..., description="8-char unique identifier")
    name: str = Field(..., max_length=200, description="User-friendly name")
    event: HookEventName = Field(..., description="Lifecycle event this hook attaches to")
    matcher: Optional[str] = Field(
        default=None,
        description=(
            "Pipe-list name filter: tool names on the tool events "
            "('Edit|Write'), command paths on command_submit "
            "('tools list|provider *'; a trailing * matches a family)"
        ),
    )
    # Definition-level fire gate (the WHEN layer). Evaluated by the engine
    # BEFORE the logic runs, so it gates every logic substrate uniformly
    # (canned actions now, the workflow substrate later). Distinct from the
    # guardrail actions' per-logic ``conditions``, which match tool args and
    # are part of that logic's semantics.
    fire_conditions: List[HookCondition] = Field(
        default_factory=list,
        description=(
            "AND-ed engine-level fire gate evaluated against the event's "
            "context data (meta fields, args.* tool args, context-usage "
            "numbers) before the logic runs; empty = always fire"
        ),
    )
    once: bool = Field(
        default=False,
        description=(
            "Fire once per gate crossing: after firing, stays silent while "
            "fire_conditions keep matching and re-arms when they stop matching"
        ),
    )
    single_use: bool = Field(
        default=False,
        description=(
            "Delete this hook after its first successful run (the recorder "
            "removes the definition on an 'ok' status, keeping its log "
            "entries). Unlike 'once', whose fired-state is in-memory and "
            "re-arms on restart, a single_use hook cannot fire twice."
        ),
    )
    logic: HookLogic
    enabled: bool = Field(default=True, description="Per-hook global default (see enable model)")
    scope: Literal["global", "thread"] = Field(
        default="thread", description="'global' = all threads; 'thread' = only thread_id"
    )
    thread_id: str = Field(default="", description="Bound thread when scope == 'thread'")
    template: str = Field(
        default="",
        max_length=100,
        description=(
            "Bundled template id this hook was installed from "
            "('' = hand-authored); makes installs idempotent"
        ),
    )
    created_by: str = Field(default="agent", description="'agent' or 'user'")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _datetimes_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)

    @model_validator(mode="after")
    def _validate_event_action(self) -> "HookDefinition":
        allowed = EVENT_ACTIONS.get(self.event)
        if allowed is None:
            raise ValueError(f"Unsupported hook event: {self.event!r}")
        if self.logic.action not in allowed:
            # System actions are excluded from the authoring legality map but
            # legal on their spec'd events, so the built-in system definitions
            # (and their materialized copies) validate. add_hook/update_hook
            # separately refuse to author/switch-to a system action.
            if self.logic.action not in _SYSTEM_EVENT_ACTIONS.get(self.event, set()):
                raise ValueError(
                    f"Action {self.logic.action!r} is not valid for event {self.event!r}"
                )
        # Normalize an empty/whitespace matcher to None (match every tool). An
        # empty-string matcher would parse to [""] and silently match nothing,
        # disabling a tool hook a caller meant to apply to all tools.
        if self.matcher is not None and not self.matcher.strip():
            self.matcher = None
        # A matcher only makes sense on a matcher event (tool events match the
        # tool name, command_submit matches the command path); on any other
        # event a set matcher would silently never match, disabling the hook.
        if self.event not in MATCHER_EVENTS and self.matcher:
            self.matcher = None
        return self


def system_turn_metadata_definition() -> HookDefinition:
    """The built-in system turn-metadata hook, in its default (pristine) shape.

    Synthesized fresh per call: the definition is VIRTUAL until a user edits
    it (copy-on-write materialization in ``update_hook``), so "no stored
    record" is a self-healing pristine state and no user store is ever
    written implicitly. The turn-entry seam takes its byte-identical legacy
    fast path whenever no stored record exists.
    """
    return HookDefinition(
        id=SYSTEM_TURN_METADATA_ID,
        name="Turn metadata",
        event="prompt_submit",
        logic=TurnMetadataLogic(),
        enabled=True,
        scope="global",
        thread_id="",
        created_by="system",
    )


def is_system_hook_id(hook_id: Optional[str]) -> bool:
    """True when ``hook_id`` names a built-in system hook definition."""
    return hook_id in SYSTEM_HOOK_IDS


# ---------------------------------------------------------------------------
# Flat-field <-> params mapping (shared by REST / command / tool authoring)
# ---------------------------------------------------------------------------

def params_from_fields(
    action: str,
    *,
    text: Optional[str] = None,
    conditions: Optional[List[HookCondition]] = None,
    reason: Optional[str] = None,
    updates: Optional[Dict[str, str]] = None,
    url: Optional[str] = None,
    command: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    workflow_id: Optional[str] = None,
    workflow_params: Optional[Dict[str, Any]] = None,
    on_fault: Optional[str] = None,
) -> Optional[dict]:
    """Assemble the logic params dict for ``action`` from flat authoring fields.

    One source of truth for the flat-field -> params mapping across the REST
    router, the ``/hook`` command, and the agent tool. Returns ``None`` when no
    logic field was supplied (an update that touches only name/enabled/etc.);
    the manager validates the assembled params.
    """
    if action in TEXT_ACTIONS:
        return {"text": text or ""} if text is not None else None
    if action == "require_approval":
        # Reuses the existing flat fields: ``text`` authors the approval
        # ``prompt`` (no surface needs a new parameter), ``conditions`` and
        # ``timeout_seconds`` map directly.
        params = {}
        if text is not None:
            params["prompt"] = text
        if conditions is not None:
            params["conditions"] = [c.model_dump() for c in conditions]
        if timeout_seconds is not None:
            params["timeout_seconds"] = timeout_seconds
        return params or None
    if action == "webhook":
        params: dict = {}
        if url is not None:
            params["url"] = url
        if text is not None:
            params["text"] = text
        return params or None
    if action == "run_command":
        params = {}
        if command is not None:
            params["command"] = command
        if timeout_seconds is not None:
            params["timeout_seconds"] = timeout_seconds
        return params or None
    if action == "run_workflow":
        params = {}
        if workflow_id is not None:
            params["workflow_id"] = workflow_id
        if workflow_params is not None:
            params["params"] = workflow_params
        if timeout_seconds is not None:
            params["timeout_seconds"] = timeout_seconds
        if on_fault is not None:
            params["on_fault"] = on_fault
        return params or None
    params = {}
    if conditions is not None:
        params["conditions"] = [c.model_dump() for c in conditions]
    if action == "block_if_matches" and reason is not None:
        params["reason"] = reason
    if action == "rewrite_arg" and updates is not None:
        params["updates"] = updates
    return params or None


def build_update_kwargs(
    existing: "HookDefinition",
    *,
    action: Optional[str] = None,
    text: Optional[str] = None,
    conditions: Optional[List[HookCondition]] = None,
    reason: Optional[str] = None,
    updates: Optional[Dict[str, str]] = None,
    url: Optional[str] = None,
    command: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    workflow_id: Optional[str] = None,
    workflow_params: Optional[Dict[str, Any]] = None,
    on_fault: Optional[str] = None,
    scalars: Optional[dict] = None,
) -> dict:
    """Merge flat authoring fields into a kwargs dict for ``update_hook``.

    ``scalars`` are the already-filtered plain fields (name/event/matcher/
    enabled). A partial update must not wipe unspecified sibling sub-fields: when
    the action is unchanged, provided logic fields merge onto the stored params;
    on an action switch the provided fields stand alone. Returns the kwargs to
    splat into ``HookManager.update_hook`` (empty when nothing changed).
    """
    out: dict = dict(scalars or {})
    effective_action = action or existing.logic.action
    switching = action is not None and action != existing.logic.action
    provided = params_from_fields(
        effective_action, text=text, conditions=conditions,
        reason=reason, updates=updates, url=url,
        command=command, timeout_seconds=timeout_seconds,
        workflow_id=workflow_id, workflow_params=workflow_params,
        on_fault=on_fault,
    )
    if provided is not None:
        if switching:
            params = provided
        else:
            params = existing.logic.model_dump(exclude={"action"})
            params.update(provided)
    else:
        params = None
    if action is not None:
        out["action"] = action
    if params is not None:
        out["params"] = params
    return out


class HookStore(BaseModel):
    """Per-user collection of hooks (serialized to JSON)."""

    user_id: str = Field(default="default")
    hooks: List[HookDefinition] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    MAX_HOOKS: int = 50

    @field_validator("updated_at")
    @classmethod
    def _updated_at_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)

    def get_hook(self, hook_id: str) -> Optional[HookDefinition]:
        for h in self.hooks:
            if h.id == hook_id:
                return h
        return None


class HookExecution(BaseModel):
    """One hook run, recorded for the per-user diagnostic log.

    Mirrors ``TriggerExecution``: this is what lets a user tell "fired and did
    nothing" (a ``no_op`` entry) apart from "never fired" (no entry), and a
    guardrail's spurious fail-closed deny (``saturated``/``timeout``/``error``)
    apart from a deliberate one (``ok`` with a deny detail).
    """

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    hook_id: str = ""
    hook_name: str = ""
    event: str = ""
    plane: Literal["mutate", "observe", ""] = ""
    status: Literal["ok", "no_op", "error", "timeout", "saturated", "illegal"] = "ok"
    detail: str = ""  # outcome/error summary, capped by the recorder
    duration_seconds: float = 0.0
    thread_id: str = ""
    tool_name: str = ""
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("timestamp")
    @classmethod
    def _timestamp_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)


def make_execution_recorder(manager: "HookManager", user_id: str):
    """Build the engine-side recorder callable bound to a user's execution log.

    The engine calls it as ``recorder(reg, ctx, status=..., detail=...,
    duration=...)`` after each hook run. ``reg``/``ctx`` are duck-typed (this
    module keeps its no-engine-imports rule): ``reg`` carries
    ``definition_id``/``name``/``observe``/``single_use``, ``ctx`` carries the
    event and turn fields. Recording never raises into a turn.

    The recorder is also the single_use cleanup seam: an ``ok`` run of a
    ``single_use`` registration deletes its definition SYNCHRONOUSLY (not via
    the write-behind log executor), so "definition absent" reliably means
    "already fired" for callers that claim-by-delete (the ``/done`` race
    guard), and a restart can never re-fire a spent one-shot hook. Log
    entries are kept (``purge_log=False``) so the fire stays visible.
    """

    def _recorder(reg, ctx, *, status: str, detail: str, duration: float) -> None:
        try:
            event = getattr(ctx, "event", None)
            event_name = getattr(event, "value", None) or str(event or "")
            hook_id = getattr(reg, "definition_id", None) or ""
            manager.log_execution(
                user_id,
                HookExecution(
                    hook_id=hook_id,
                    hook_name=getattr(reg, "name", "") or "",
                    event=event_name,
                    plane="observe" if getattr(reg, "observe", False) else "mutate",
                    status=status,  # type: ignore[arg-type] - validated by pydantic
                    detail=(detail or "")[:300],
                    duration_seconds=round(float(duration), 4),
                    thread_id=getattr(ctx, "thread_id", "") or "",
                    tool_name=getattr(ctx, "tool_name", None) or "",
                ),
            )
            # single_use self-cleanup: only a successful run consumes the hook
            # (a no_op/error/timeout keeps it armed, mirroring the once gate's
            # failed-fire asymmetry).
            if hook_id and status == "ok" and getattr(reg, "single_use", False):
                if manager.delete_hook(user_id, hook_id, purge_log=False):
                    logger.info(
                        "Deleted spent single_use hook %s for user %s", hook_id, user_id
                    )
        except Exception:  # noqa: BLE001 - recording must never raise into a turn
            logger.warning("hook execution recording failed", exc_info=True)

    return _recorder


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class HookManager:
    """CRUD + persistence for lifecycle hooks (one JSON file per user)."""

    def __init__(self, data_dir: Path):
        self.hooks_dir = Path(data_dir) / "hooks"
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        # Fingerprint-keyed read cache for the hot per-turn path:
        # (FileFingerprint or None, hooks). The kernel's file-timestamp clock
        # is coarse (~1ms), so a write landing in the same granule as the
        # previous one is invisible to an mtime-only key; adding size narrows
        # that but does nothing for the same-size rewrite this store sees most
        # (an equal-length timeout, priority, or name edit). The fingerprint
        # therefore carries a content hash while a file is too recent for
        # mtime to be trusted: see ``storage_paths.compare_fingerprint``.
        self._read_cache: Dict[
            str, Tuple[Optional[FileFingerprint], List[HookDefinition]]
        ] = {}
        # Write-behind execution-log buffer (drained by the shared log executor).
        self._pending_executions: Dict[str, List[HookExecution]] = {}
        self._pending_lock = threading.Lock()
        logger.info("HookManager initialized: %s", self.hooks_dir)

    # -- locking ----------------------------------------------------------

    def _get_lock(self, user_id: str) -> threading.RLock:
        return _hook_locks.get(user_id)

    @staticmethod
    def _snapshot(store: HookStore) -> str:
        return json.dumps(store.model_dump(mode="json"), sort_keys=True, default=str)

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """Atomic store update: persist only when the block changed the store.

        Raises ``RuntimeError`` if a needed save fails (a disk error is
        surfaced, not silently dropped). An exception inside the block
        propagates first and is never masked.
        """
        lock = self._get_lock(user_id)
        with lock:
            store = self._load(user_id)
            before = self._snapshot(store)
            try:
                yield store
            finally:
                saved_ok = True
                if self._snapshot(store) != before:
                    saved_ok = self._save(store)
            if not saved_ok:
                raise RuntimeError(f"Failed to persist hooks for user {user_id}")

    # -- persistence ------------------------------------------------------

    def _path_for(self, user_id: str) -> Path:
        return self.hooks_dir / f"{safe_path_segment(user_id)}.json"

    @staticmethod
    def _quarantine_corrupt(path: Path) -> Optional[Path]:
        """Move an unparseable store file aside so its bytes survive.

        Without this, the load falls back to an empty store and the next save
        overwrites the original file: a user's whole hook set silently vanishes.
        Delegates to the shared ``quarantine_corrupt_file`` helper (a
        ``quarantine/`` sibling directory), which every file-backed resource
        store now uses. Returns the quarantine path, or ``None`` if the rename
        failed (which degrades to the old replace-with-empty behavior; loading
        still never raises into a turn).
        """
        return quarantine_corrupt_file(path)

    def _load(self, user_id: str) -> HookStore:
        path = self._path_for(user_id)
        # Read + parse + quarantine run under the per-user lock (an RLock, so
        # locked callers like atomic_update re-enter freely). Unlocked readers
        # (get_hooks / get_hook) would otherwise race a concurrent locked
        # write: parse a stale corrupt file, lose to a repairing _save, then
        # quarantine-rename the freshly written valid store aside.
        with self._get_lock(user_id):
            if not path.exists():
                return HookStore(user_id=user_id)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return HookStore.model_validate(data)
            except OSError as e:
                # Unreadable, not corrupt. Quarantine means "these bytes do not
                # parse"; a file we could not READ has no known contents, and
                # moving it aside over fd exhaustion, a share lock or a uid
                # mismatch would destroy a live store to fix nothing. Degrade
                # to empty for this call and leave the file where it is.
                logger.error("Could not read hooks for %s: %s", user_id, e)
                return HookStore(user_id=user_id)
            except Exception as e:  # noqa: BLE001 - never let a bad file break a turn
                quarantine = self._quarantine_corrupt(path)
                logger.error(
                    "Failed to load hooks for %s: %s (%s)",
                    user_id,
                    e,
                    f"corrupt file preserved at {quarantine}"
                    if quarantine
                    else "quarantine rename failed; file left in place",
                )
                try:
                    from .activity_log import log_external_edit

                    log_external_edit(
                        "hooks",
                        "corrupt hook store "
                        + (f"quarantined as quarantine/{quarantine.name}" if quarantine
                           else "could not be quarantined"),
                        user_id=user_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug("Failed to record hooks quarantine audit", exc_info=True)
                return HookStore(user_id=user_id)

    def _save(self, store: HookStore) -> bool:
        path = self._path_for(store.user_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            store.updated_at = utc_now()
            write_text_atomic(
                path,
                json.dumps(store.model_dump(mode="json"), indent=2, default=str),
            )
            # Record the write in the shared .sig sidecar so loaders (in any
            # instance or process) can tell manager writes from raw on-disk
            # edits (resource-filesystem-layout plan, slice 4).
            record_store_fingerprint(path)
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to save hooks for %s: %s", store.user_id, e)
            return False

    # -- CRUD -------------------------------------------------------------

    def add_hook(
        self,
        user_id: str,
        *,
        name: str,
        event: str,
        action: str = "inject_context",
        params: Optional[dict] = None,
        text: Optional[str] = None,
        matcher: Optional[str] = None,
        fire_conditions: Optional[List[HookCondition]] = None,
        once: bool = False,
        single_use: bool = False,
        scope: str = "thread",
        thread_id: Optional[str] = None,
        enabled: bool = True,
        created_by: str = "agent",
        template: str = "",
    ) -> Optional[HookDefinition]:
        """Create a hook. Raises ``ValueError``/``ValidationError`` on an invalid
        definition; returns ``None`` if the per-user cap is reached.

        ``action`` + ``params`` build the logic variant. ``text`` is a
        convenience alias: when given (and ``params`` is not), it becomes
        ``{"text": text}`` for the text actions and ``{"prompt": text}`` for
        ``require_approval`` (matching ``params_from_fields``, so every
        surface's bare-text authoring lands on the right field).
        """
        if action in SYSTEM_ACTIONS:
            raise ValueError(
                f"Action {action!r} is reserved for the built-in system hook. "
                f"Edit the '{SYSTEM_TURN_METADATA_ID}' hook instead of creating one."
            )
        if params is None and text is not None:
            params = {"prompt": text} if action == "require_approval" else {"text": text}
        logic = build_logic(action, params)  # ValueError on bad action/params
        if action == "run_workflow":
            binding_error = run_workflow_authoring_error(params)
            if binding_error:
                raise ValueError(binding_error)
        # Construct first so validation (event/action legality, matcher
        # normalization) runs before we touch the store.
        hook = HookDefinition(
            id=uuid.uuid4().hex[:8],
            name=name,
            event=event,  # type: ignore[arg-type]
            matcher=matcher,
            fire_conditions=fire_conditions or [],
            once=once,
            single_use=single_use,
            logic=logic,  # type: ignore[arg-type]
            enabled=enabled,
            scope=scope,  # type: ignore[arg-type]
            thread_id=thread_id or "",
            template=template,
            created_by=created_by,
        )
        with self.atomic_update(user_id) as store:
            # Materialized system hooks are cap-exempt both ways: they neither
            # count against nor consume the user's authorable-hook budget.
            if sum(1 for h in store.hooks if h.id not in SYSTEM_HOOK_IDS) >= store.MAX_HOOKS:
                logger.warning("Hook limit reached for user %s", user_id)
                return None
            store.hooks.append(hook)
        logger.info("Created hook '%s' (%s) for user %s", name, hook.id, user_id)
        return hook

    def update_hook(self, user_id: str, hook_id: str, **kwargs) -> bool:
        """Update fields on an existing hook.

        Logic edits come via ``action`` (switch action), ``params`` (replace the
        logic params), and/or ``text`` (convenience for the text actions).
        ``event``/``matcher``/logic changes re-run model validation via
        ``model_validate`` so an illegal combination is rejected rather than
        silently persisted. Raises ``ValueError`` on an unknown action.

        System hooks (``SYSTEM_HOOK_IDS``): the first edit MATERIALIZES the
        virtual built-in default into the store (copy-on-write, cap-exempt),
        then applies the update; identity/binding fields
        (``_SYSTEM_LOCKED_FIELDS``) and action switches are rejected. Ordinary
        hooks may never switch TO a system action.
        """
        if hook_id in SYSTEM_HOOK_IDS:
            locked = _SYSTEM_LOCKED_FIELDS & set(kwargs)
            if locked:
                raise ValueError(
                    f"Field(s) {', '.join(sorted(locked))} cannot be changed on the "
                    f"system hook '{hook_id}'."
                )
            if kwargs.get("action") not in (None, "turn_metadata"):
                raise ValueError(
                    f"The system hook '{hook_id}' cannot switch action."
                )
        elif kwargs.get("action") in SYSTEM_ACTIONS:
            raise ValueError(
                f"Action {kwargs['action']!r} is reserved for the built-in system "
                f"hook '{SYSTEM_TURN_METADATA_ID}'."
            )
        with self.atomic_update(user_id) as store:
            hook = store.get_hook(hook_id)
            materialize = False
            if hook is None and hook_id == SYSTEM_TURN_METADATA_ID:
                # Copy-on-write: the edit lands on a materialized copy of the
                # virtual default. Deliberately cap-exempt (not user-authored
                # growth); the store is only touched after validation passes,
                # so a rejected edit leaves the hook virtual (pristine).
                hook = system_turn_metadata_definition()
                materialize = True
            if hook is None:
                return False
            data = hook.model_dump()
            new_action = kwargs.pop("action", None)
            new_params = kwargs.pop("params", None)
            new_text = kwargs.pop("text", None)
            if new_action is not None or new_params is not None or new_text is not None:
                action = new_action or data["logic"]["action"]
                if new_params is not None:
                    params = dict(new_params)
                elif new_action is not None:
                    # Switching action with no params: start from an empty variant.
                    params = {}
                else:
                    # Keep the existing params (minus the discriminator) so a
                    # text-only edit overlays onto them.
                    params = {k: v for k, v in data["logic"].items() if k != "action"}
                if new_text is not None:
                    # Same action-aware alias as add_hook: bare text authors the
                    # approval prompt on require_approval hooks.
                    params["prompt" if action == "require_approval" else "text"] = new_text
                # Validate the variant now (clear error) before re-validating the
                # whole definition below.
                build_logic(action, params)
                if action == "run_workflow":
                    binding_error = run_workflow_authoring_error(params)
                    if binding_error:
                        raise ValueError(binding_error)
                data["logic"] = {"action": action, **params}
            for key, value in kwargs.items():
                if key in data and key not in ("id", "created_at"):
                    data[key] = value
            data["updated_at"] = utc_now()
            updated = HookDefinition.model_validate(data)  # re-validates legality
            if materialize:
                store.hooks.append(updated)
            else:
                # Replace in place, preserving order.
                store.hooks = [updated if h.id == hook_id else h for h in store.hooks]
        return True

    def delete_hook(self, user_id: str, hook_id: str, *, purge_log: bool = True) -> bool:
        """Remove a hook permanently (and, by default, its execution-log entries).

        ``purge_log=False`` keeps the log entries: the single_use self-cleanup
        path uses it so a spent one-shot hook's fire stays visible in
        ``/hook history`` (orphaned entries age out via the log cap).

        System hooks are never truly deleted: removing the stored record
        RESETS the hook to its built-in defaults (the virtual definition
        reappears in the authoring views). Log entries are kept so fault and
        fallback history survives a reset; a pristine system hook (nothing
        stored) returns False.
        """
        with self.atomic_update(user_id) as store:
            before = len(store.hooks)
            store.hooks = [h for h in store.hooks if h.id != hook_id]
            deleted = len(store.hooks) < before
        if deleted and purge_log and hook_id not in SYSTEM_HOOK_IDS:
            self.delete_executions_for_hooks(user_id, [hook_id])
        return deleted

    def delete_hooks_for_thread(self, user_id: str, thread_id: str) -> List[str]:
        """Remove all thread-scoped hooks bound to ``thread_id``. Returns deleted IDs."""
        lock = self._get_lock(user_id)
        with lock:
            store = self._load(user_id)
            deleted = [h.id for h in store.hooks if h.scope == "thread" and h.thread_id == thread_id]
            if deleted:
                store.hooks = [
                    h for h in store.hooks
                    if not (h.scope == "thread" and h.thread_id == thread_id)
                ]
                if not self._save(store):
                    raise RuntimeError(f"Failed to save hook cleanup for user {user_id}")
        if deleted:
            self.delete_executions_for_hooks(user_id, deleted)
        return deleted

    def get_hooks(self, user_id: str) -> List[HookDefinition]:
        """All hooks for a user (fresh read, for authoring/listing).

        Includes the virtual system turn-metadata definition when no stored
        record carries its reserved id, so every list surface (the tool, the
        ``/hook`` command, REST) exposes it for inspection without any store
        write. The hot per-turn read (``get_hooks_cached``) deliberately stays
        a raw store view: the turn-entry seam treats "no stored record" as
        the byte-identical built-in fast path.
        """
        hooks = list(self._load(user_id).hooks)
        if not any(h.id == SYSTEM_TURN_METADATA_ID for h in hooks):
            hooks.append(system_turn_metadata_definition())
        return hooks

    def get_hook(self, user_id: str, hook_id: str) -> Optional[HookDefinition]:
        """A single hook by ID (fresh read; system ids resolve to the virtual
        built-in default when nothing is stored)."""
        hook = self._load(user_id).get_hook(hook_id)
        if hook is None and hook_id == SYSTEM_TURN_METADATA_ID:
            return system_turn_metadata_definition()
        return hook

    def get_hooks_cached(self, user_id: str) -> List[HookDefinition]:
        """All hooks for a user, fingerprint-cached for the hot per-turn path.

        Reparses only when the backing file's fingerprint changed, so a turn
        that resolves the registry several times (and cross-instance writes
        from the tool/REST layers) both stay correct without a disk read every
        turn.

        This cache has no other rescan path, so a missed change is permanent
        and keeps a stale hook firing (a standing prompt injection, a
        pre_tool_use guardrail, or an approval gate) after the store said
        otherwise. That is why the fingerprint is content-exact for recently
        written files rather than plain (mtime, size): see
        ``storage_paths.compare_fingerprint``.
        """
        path = self._path_for(user_id)
        with self._get_lock(user_id):
            cached = self._read_cache.get(user_id)
            previous = cached[0] if cached is not None else None
        sig, changed = compare_fingerprint(path, previous)
        with self._get_lock(user_id):
            cached = self._read_cache.get(user_id)
            if cached is not None and not changed:
                return cached[1]
            # A file fingerprint differing from the recorded manager write is
            # a raw on-disk edit: audit it (a written hook is a standing
            # prompt injection, so this is the one durable record it changed
            # outside the tool chokepoints). The .sig sidecar is shared across
            # manager instances and processes (authoring goes through the
            # tools/REST managers, this engine instance only reads), and
            # re-recording it here acknowledges the edit so it is audited
            # once, not once per reader. An absent sidecar (no manager write
            # on record) means no audit: fail-safe, never false-positive.
            # Compared against the sidecar as the baseline, not with a bare
            # differ: when the sidecar carries a content hash, that forces this
            # side to hash too, so an edit that reused the manager write's
            # coarse mtime and byte count is still caught.
            expected = read_store_fingerprint(path)
            _, edited_on_disk = compare_fingerprint(path, expected)
            if expected is not None and sig is not None and edited_on_disk:
                try:
                    from .activity_log import log_external_edit

                    log_external_edit(
                        "hooks", "hook store file edited on disk", user_id=user_id
                    )
                except Exception:  # noqa: BLE001 - audit must never break a turn
                    logger.debug("Failed to record hooks external-edit audit", exc_info=True)
                record_store_fingerprint(path)
            else:
                # A pre-hash sidecar compares on (mtime, size) alone, so it
                # must be upgraded even when nothing changed, or this store
                # stays on the degraded comparison forever.
                upgrade_legacy_store_fingerprint(path, expected)
            hooks = list(self._load(user_id).hooks)
            self._read_cache[user_id] = (sig, hooks)
            return hooks

    # -- execution log (write-behind) ---------------------------------------

    def _executions_path(self, user_id: str) -> Path:
        return self.hooks_dir / f"{safe_path_segment(user_id)}_executions.json"

    def log_execution(self, user_id: str, execution: HookExecution) -> None:
        """Queue an execution record (write-behind; no file I/O in-band).

        Appends to the in-memory buffer and schedules a coalescing flush on the
        shared single-worker executor. Never raises and never blocks beyond the
        buffer append: this runs inside dispatch, including the PRE mutate path
        that sits inside every tool call.
        """
        try:
            with self._pending_lock:
                self._pending_executions.setdefault(user_id, []).append(execution)
            _log_executor.submit(self._flush_executions, user_id)
        except Exception as e:  # noqa: BLE001 - recording must never raise
            logger.warning("Failed to queue hook execution entry: %s", e)

    def _flush_executions(self, user_id: str) -> None:
        """Drain a user's pending entries into the log file (executor-only).

        Runs only on the single log-executor worker, which serializes every
        write to the file; a burst of firings coalesces into one
        read-modify-write because the first flush drains everything pending.
        """
        try:
            with self._pending_lock:
                pending = self._pending_executions.pop(user_id, [])
            if not pending:
                return  # an earlier coalesced flush already drained us
            path = self._executions_path(user_id)
            entries: list = []
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                    entries = loaded if isinstance(loaded, list) else []
                except (OSError, ValueError):
                    # A corrupt log file must not wedge logging forever (every
                    # flush would raise and drop its drained batch): reset it.
                    logger.warning(
                        "hook execution log for %s unreadable; resetting it", user_id
                    )
            entries.extend(e.model_dump(mode="json") for e in pending)
            if len(entries) > MAX_HOOK_EXECUTION_LOG:
                entries = entries[-MAX_HOOK_EXECUTION_LOG:]
            write_text_atomic(path, json.dumps(entries, default=str))
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to flush hook executions for %s: %s", user_id, e)

    def flush_execution_log(self, user_id: str, timeout: float = 5.0) -> None:
        """Synchronously drain a user's pending entries (read barrier / tests)."""
        try:
            _log_executor.submit(self._flush_executions, user_id).result(timeout=timeout)
        except Exception:  # noqa: BLE001 - a stuck flush must not break callers
            logger.warning("hook execution log flush barrier failed", exc_info=True)

    def get_executions(
        self,
        user_id: str,
        hook_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        """Read execution history, newest first, optionally for one hook."""
        self.flush_execution_log(user_id)
        path = self._executions_path(user_id)
        if not path.exists():
            return []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                return []
            entries = loaded
            if hook_id:
                entries = [
                    e for e in entries
                    if isinstance(e, dict) and e.get("hook_id") == hook_id
                ]
            return list(reversed(entries[-max(1, limit):]))
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to read hook executions for %s: %s", user_id, e)
            return []

    def delete_executions_for_hooks(self, user_id: str, hook_ids: List[str]) -> int:
        """Remove log entries for deleted hooks. Returns the removed file count."""
        if not hook_ids:
            return 0
        try:
            future = _log_executor.submit(
                self._purge_executions, user_id, set(hook_ids)
            )
            return future.result(timeout=5.0)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to purge hook executions for %s: %s", user_id, e)
            return 0

    def _purge_executions(self, user_id: str, hook_ids: Set[str]) -> int:
        """Drop pending + persisted entries for ``hook_ids`` (executor-only)."""
        with self._pending_lock:
            pending = self._pending_executions.get(user_id)
            if pending:
                self._pending_executions[user_id] = [
                    e for e in pending if e.hook_id not in hook_ids
                ]
        path = self._executions_path(user_id)
        if not path.exists():
            return 0
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning(
                "hook execution log for %s unreadable; skipping purge", user_id
            )
            return 0
        if not isinstance(loaded, list):
            return 0
        kept = [
            e for e in loaded
            if not (isinstance(e, dict) and e.get("hook_id") in hook_ids)
        ]
        removed = len(loaded) - len(kept)
        if removed:
            write_text_atomic(path, json.dumps(kept, default=str))
        return removed
