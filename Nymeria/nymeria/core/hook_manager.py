"""Lifecycle-hook definitions: the persisted record + per-user store.

A ``HookDefinition`` is a user- or agent-authored record ("when this event
fires, run this logic"). This module owns ONLY the record model, its
validation, and JSON persistence, mirroring ``TriggerManager`` (one file per
user under ``data_dir/hooks/``). It stays deliberately store-only: it does not
import the hooks engine (``core/hooks/``) or the agent. The bridge
(``core/hooks/bridge.py``) turns an enabled definition into an active dispatch
hook; the enable resolver lives in ``core/agent_safety.py``.

This pass ships one canned action, ``inject_context``, over three events
(``prompt_submit``/``post_tool_use``/``done``). PreToolUse is intentionally not
an injection target.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from .keyed_locks import KeyedRLockMap
from .storage_paths import safe_path_segment
from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

# Thread-safe locks keyed by user_id.
_hook_locks = KeyedRLockMap()

# The events an ``inject_context`` (or any future) action may attach to, and the
# actions legal for each. Mirrors ``core/hooks/base.py`` EVENT_OUTCOME_TYPES:
# adding an action or event is a local edit here. PreToolUse is excluded (no
# injection target).
HookEventName = Literal["prompt_submit", "post_tool_use", "done"]
EVENT_ACTIONS: Dict[str, set] = {
    "prompt_submit": {"inject_context"},
    "post_tool_use": {"inject_context"},
    "done": {"inject_context"},
}


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class HookLogic(BaseModel):
    """The logic a hook runs. One canned action in this pass.

    Flat (action + params) rather than a tagged union because there is exactly
    one action today. When a second action lands, migrate this to a
    discriminated union on ``action`` (or add optional per-action fields); the
    ``EVENT_ACTIONS`` map above is the other extension point.
    """

    action: Literal["inject_context"] = "inject_context"
    text: str = Field(
        ...,
        min_length=1,
        max_length=10_000,
        description="Static or {placeholder} template text",
    )


class HookDefinition(BaseModel):
    """A single lifecycle hook created by the user or agent."""

    id: str = Field(..., description="8-char unique identifier")
    name: str = Field(..., max_length=200, description="User-friendly name")
    event: HookEventName = Field(..., description="Lifecycle event this hook attaches to")
    matcher: Optional[str] = Field(
        default=None,
        description="Pipe-list tool-name filter (post_tool_use only), e.g. 'Edit|Write'",
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
        # disabling a post_tool_use hook a caller meant to apply to all tools.
        if self.matcher is not None and not self.matcher.strip():
            self.matcher = None
        # A matcher only makes sense on a tool event; on any other event a set
        # matcher would silently never match (no tool_name), disabling the hook.
        if self.event != "post_tool_use" and self.matcher:
            self.matcher = None
        return self


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
        text: str,
        matcher: Optional[str] = None,
        scope: str = "thread",
        thread_id: Optional[str] = None,
        enabled: bool = True,
        created_by: str = "agent",
    ) -> Optional[HookDefinition]:
        """Create a hook. Raises ``ValueError``/``ValidationError`` on an invalid
        definition; returns ``None`` if the per-user cap is reached."""
        # Construct first so validation (event/action legality, matcher
        # normalization, non-empty text) runs before we touch the store.
        hook = HookDefinition(
            id=uuid.uuid4().hex[:8],
            name=name,
            event=event,  # type: ignore[arg-type]
            matcher=matcher,
            logic=HookLogic(text=text),
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

        ``text`` rebuilds ``logic``; ``event``/``matcher``/``text`` changes
        re-run model validation via ``model_validate`` so an illegal
        combination is rejected rather than silently persisted.
        """
        with self.atomic_update(user_id) as store:
            hook = store.get_hook(hook_id)
            if hook is None:
                return False
            data = hook.model_dump()
            if "text" in kwargs:
                text = kwargs.pop("text")
                data["logic"] = {"action": data["logic"]["action"], "text": text}
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
