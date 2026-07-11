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
from typing import Annotated, Dict, Iterable, List, Literal, Optional, Set, Tuple, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .conditions import HookCondition
from .hook_spec import TOOL_EVENTS, event_actions, text_actions
from .keyed_locks import KeyedRLockMap
from .storage_paths import (
    quarantine_corrupt_file,
    read_store_fingerprint,
    record_store_fingerprint,
    safe_path_segment,
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
HookEventName = Literal["prompt_submit", "pre_tool_use", "post_tool_use", "done"]
EVENT_ACTIONS: Dict[str, set] = event_actions()

# Actions whose sole logic config is a single ``text`` field (a bare ``text`` is
# a convenience alias for ``params={"text": ...}``). Shared by every authoring
# surface via ``params_from_fields``.
TEXT_ACTIONS = text_actions()

# Actions gated behind an admin account AND a deployment opt-in flag, because
# authoring one is remote code execution on the backend host. Shared by every
# authoring surface via ``run_command_authoring_error``.
GATED_ACTIONS = frozenset({"run_command"})


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
            "(prompt_submit/pre_tool_use, which run in-band) to 60s; observe "
            "events (post_tool_use/done, off-turn) keep the full range."
        ),
    )


# Discriminated union on ``action``. Store-compatible with legacy inject_context
# records ({"action":"inject_context","text":...}). Adding an action is a new
# variant here + an ``ACTIONS``/``ACTION_PLANES`` entry + an ``EVENT_ACTIONS``
# row. The type-alias name stays ``HookLogic`` so importers do not churn.
HookLogic = Annotated[
    Union[
        InjectContextLogic, BlockIfMatchesLogic, RewriteArgLogic,
        RequireApprovalLogic,
        NotifyLogic, CreateTodoLogic, WebhookLogic, RunCommandLogic,
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
        description="Pipe-list tool-name filter (tool events only), e.g. 'Edit|Write'",
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
            raise ValueError(
                f"Action {self.logic.action!r} is not valid for event {self.event!r}"
            )
        # Normalize an empty/whitespace matcher to None (match every tool). An
        # empty-string matcher would parse to [""] and silently match nothing,
        # disabling a tool hook a caller meant to apply to all tools.
        if self.matcher is not None and not self.matcher.strip():
            self.matcher = None
        # A matcher (tool-name filter) only makes sense on a tool event; on any
        # other event a set matcher would silently never match (no tool_name),
        # disabling the hook.
        if self.event not in TOOL_EVENTS and self.matcher:
            self.matcher = None
        return self


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
        # ((mtime_ns, size) or None, hooks). Size is part of the key because
        # the kernel's file-timestamp clock is coarse: a write landing in the
        # same granule as the previous one would be invisible to an
        # mtime-only key.
        self._read_cache: Dict[
            str, Tuple[Optional[Tuple[int, int]], List[HookDefinition]]
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
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(store.model_dump(mode="json"), indent=2, default=str),
                encoding="utf-8",
            )
            temp.replace(path)
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
        if params is None and text is not None:
            params = {"prompt": text} if action == "require_approval" else {"text": text}
        logic = build_logic(action, params)  # ValueError on bad action/params
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
            if len(store.hooks) >= store.MAX_HOOKS:
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
        """
        with self.atomic_update(user_id) as store:
            hook = store.get_hook(hook_id)
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
                data["logic"] = {"action": action, **params}
            for key, value in kwargs.items():
                if key in data and key not in ("id", "created_at"):
                    data[key] = value
            data["updated_at"] = utc_now()
            updated = HookDefinition.model_validate(data)  # re-validates legality
            # Replace in place, preserving order.
            store.hooks = [updated if h.id == hook_id else h for h in store.hooks]
        return True

    def delete_hook(self, user_id: str, hook_id: str, *, purge_log: bool = True) -> bool:
        """Remove a hook permanently (and, by default, its execution-log entries).

        ``purge_log=False`` keeps the log entries: the single_use self-cleanup
        path uses it so a spent one-shot hook's fire stays visible in
        ``/hook log`` (orphaned entries age out via the log cap).
        """
        with self.atomic_update(user_id) as store:
            before = len(store.hooks)
            store.hooks = [h for h in store.hooks if h.id != hook_id]
            deleted = len(store.hooks) < before
        if deleted and purge_log:
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
        """All hooks for a user (fresh read, for authoring/listing)."""
        return list(self._load(user_id).hooks)

    def get_hook(self, user_id: str, hook_id: str) -> Optional[HookDefinition]:
        """A single hook by ID (fresh read)."""
        return self._load(user_id).get_hook(hook_id)

    def get_hooks_cached(self, user_id: str) -> List[HookDefinition]:
        """All hooks for a user, fingerprint-cached for the hot per-turn path.

        Reparses only when the backing file's (mtime_ns, size) fingerprint
        changed, so a turn that resolves the registry several times (and
        cross-instance writes from the tool/REST layers) both stay correct
        without a disk read every turn.
        """
        path = self._path_for(user_id)
        try:
            st = path.stat() if path.exists() else None
        except OSError:
            st = None
        sig = (st.st_mtime_ns, st.st_size) if st is not None else None
        with self._get_lock(user_id):
            cached = self._read_cache.get(user_id)
            if cached is not None and cached[0] == sig:
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
            expected = read_store_fingerprint(path)
            if expected is not None and sig is not None and sig != expected:
                try:
                    from .activity_log import log_external_edit

                    log_external_edit(
                        "hooks", "hook store file edited on disk", user_id=user_id
                    )
                except Exception:  # noqa: BLE001 - audit must never break a turn
                    logger.debug("Failed to record hooks external-edit audit", exc_info=True)
                record_store_fingerprint(path)
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
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(entries, default=str), encoding="utf-8")
            temp.replace(path)
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
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(kept, default=str), encoding="utf-8")
            temp.replace(path)
        return removed
