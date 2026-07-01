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
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .conditions import HookCondition
from .keyed_locks import KeyedRLockMap
from .storage_paths import safe_path_segment
from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

# Thread-safe locks keyed by user_id.
_hook_locks = KeyedRLockMap()

# The events an action may attach to, and the actions legal for each. Mirrors
# ``core/hooks/base.py`` EVENT_OUTCOME_TYPES: adding an action or event is a
# local edit here. ``pre_tool_use`` carries the mutate-plane guardrail actions
# (deny/rewrite); the observe-plane actions (notify/create_todo/webhook) land in
# a later slice on the post_tool_use/done events.
HookEventName = Literal["prompt_submit", "pre_tool_use", "post_tool_use", "done"]
EVENT_ACTIONS: Dict[str, set] = {
    "prompt_submit": {"inject_context"},
    "pre_tool_use": {"block_if_matches", "rewrite_arg"},
    "post_tool_use": {"inject_context", "notify", "create_todo", "webhook"},
    "done": {"inject_context", "notify", "create_todo", "webhook"},
}

# Actions whose sole logic config is a single ``text`` field (a bare ``text`` is
# a convenience alias for ``params={"text": ...}``). Shared by every authoring
# surface via ``params_from_fields``.
TEXT_ACTIONS = ("inject_context", "notify", "create_todo")


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


# Discriminated union on ``action``. Store-compatible with legacy inject_context
# records ({"action":"inject_context","text":...}). Adding an action is a new
# variant here + an ``ACTIONS``/``ACTION_PLANES`` entry + an ``EVENT_ACTIONS``
# row. The type-alias name stays ``HookLogic`` so importers do not churn.
HookLogic = Annotated[
    Union[
        InjectContextLogic, BlockIfMatchesLogic, RewriteArgLogic,
        NotifyLogic, CreateTodoLogic, WebhookLogic,
    ],
    Field(discriminator="action"),
]

# Build a logic variant from an action name + a params dict (used by add_hook).
HOOK_LOGIC_BY_ACTION: Dict[str, type[BaseModel]] = {
    "inject_context": InjectContextLogic,
    "block_if_matches": BlockIfMatchesLogic,
    "rewrite_arg": RewriteArgLogic,
    "notify": NotifyLogic,
    "create_todo": CreateTodoLogic,
    "webhook": WebhookLogic,
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
    logic: HookLogic
    enabled: bool = Field(default=True, description="Per-hook global default (see enable model)")
    scope: Literal["global", "thread"] = Field(
        default="thread", description="'global' = all threads; 'thread' = only thread_id"
    )
    thread_id: str = Field(default="", description="Bound thread when scope == 'thread'")
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
        if self.event not in ("pre_tool_use", "post_tool_use") and self.matcher:
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
) -> Optional[dict]:
    """Assemble the logic params dict for ``action`` from flat authoring fields.

    One source of truth for the flat-field -> params mapping across the REST
    router, the ``/hook`` command, and the agent tool. Returns ``None`` when no
    logic field was supplied (an update that touches only name/enabled/etc.);
    the manager validates the assembled params.
    """
    if action in TEXT_ACTIONS:
        return {"text": text or ""} if text is not None else None
    if action == "webhook":
        params: dict = {}
        if url is not None:
            params["url"] = url
        if text is not None:
            params["text"] = text
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


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class HookManager:
    """CRUD + persistence for lifecycle hooks (one JSON file per user)."""

    def __init__(self, data_dir: Path):
        self.hooks_dir = Path(data_dir) / "hooks"
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        # mtime-keyed read cache for the hot per-turn path: (mtime_ns, hooks).
        self._read_cache: Dict[str, Tuple[int, List[HookDefinition]]] = {}
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

    def _load(self, user_id: str) -> HookStore:
        path = self._path_for(user_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return HookStore.model_validate(data)
            except Exception as e:  # noqa: BLE001 - never let a bad file break a turn
                logger.error("Failed to load hooks for %s: %s", user_id, e)
                return HookStore(user_id=user_id)
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
        scope: str = "thread",
        thread_id: Optional[str] = None,
        enabled: bool = True,
        created_by: str = "agent",
    ) -> Optional[HookDefinition]:
        """Create a hook. Raises ``ValueError``/``ValidationError`` on an invalid
        definition; returns ``None`` if the per-user cap is reached.

        ``action`` + ``params`` build the logic variant. ``text`` is a
        convenience alias: when given (and ``params`` is not), it becomes
        ``{"text": text}`` for the text actions.
        """
        if params is None and text is not None:
            params = {"text": text}
        logic = build_logic(action, params)  # ValueError on bad action/params
        # Construct first so validation (event/action legality, matcher
        # normalization) runs before we touch the store.
        hook = HookDefinition(
            id=uuid.uuid4().hex[:8],
            name=name,
            event=event,  # type: ignore[arg-type]
            matcher=matcher,
            logic=logic,  # type: ignore[arg-type]
            enabled=enabled,
            scope=scope,  # type: ignore[arg-type]
            thread_id=thread_id or "",
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
                    params["text"] = new_text
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

    def delete_hook(self, user_id: str, hook_id: str) -> bool:
        """Remove a hook permanently."""
        with self.atomic_update(user_id) as store:
            before = len(store.hooks)
            store.hooks = [h for h in store.hooks if h.id != hook_id]
            return len(store.hooks) < before

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
            return deleted

    def get_hooks(self, user_id: str) -> List[HookDefinition]:
        """All hooks for a user (fresh read, for authoring/listing)."""
        return list(self._load(user_id).hooks)

    def get_hook(self, user_id: str, hook_id: str) -> Optional[HookDefinition]:
        """A single hook by ID (fresh read)."""
        return self._load(user_id).get_hook(hook_id)

    def get_hooks_cached(self, user_id: str) -> List[HookDefinition]:
        """All hooks for a user, mtime-cached for the hot per-turn resolve path.

        Reparses only when the backing file's mtime changed, so a turn that
        resolves the registry several times (and cross-instance writes from the
        tool/REST layers) both stay correct without a disk read every turn.
        """
        path = self._path_for(user_id)
        try:
            mtime = path.stat().st_mtime_ns if path.exists() else 0
        except OSError:
            mtime = 0
        with self._get_lock(user_id):
            cached = self._read_cache.get(user_id)
            if cached is not None and cached[0] == mtime:
                return cached[1]
            hooks = list(self._load(user_id).hooks)
            self._read_cache[user_id] = (mtime, hooks)
            return hooks
