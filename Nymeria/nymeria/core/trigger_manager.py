"""Core trigger engine -- models, CRUD, polling, and action execution.

Triggers are event-driven automations that react to external events
(webhooks, API changes, incoming data).  They complement recurring TODOs,
which handle time-based autonomous work.

Storage follows the same pattern as TodoManager: one JSON file per user
in ``data_dir/triggers/``.
"""

import json
import logging
import re
import threading
import time as _time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from .keyed_locks import KeyedRLockMap
from .time_utils import ensure_aware_utc, utc_now

if TYPE_CHECKING:
    from .agent import NymeriaAgent
    from .turn_executor import TurnExecutor

logger = logging.getLogger(__name__)

# Thread-safe locks keyed by user_id
_trigger_locks = KeyedRLockMap()

MAX_EXECUTION_LOG = 200


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TriggerAction(BaseModel):
    """What to do when a trigger fires."""

    type: Literal["agent_prompt", "notify", "create_todo"] = Field(
        ..., description="Action type"
    )
    config: dict = Field(
        default_factory=dict,
        description=(
            "Action-specific config. "
            "agent_prompt: {prompt_template (or prompt), thread_id?}. "
            "notify: {message_template, platform?}. "
            "create_todo: {task_template, scheduled_for?}."
        ),
    )


class TriggerCondition(BaseModel):
    """A filter condition evaluated against each event before firing."""

    field: str = Field(..., description="Event field name to check")
    operator: Literal["equals", "contains", "starts_with", "matches_regex", "not_equals"] = Field(
        default="contains"
    )
    value: str = Field(default="")
    case_sensitive: bool = Field(default=False)


class TriggerDefinition(BaseModel):
    """A single trigger instance created by the user or agent."""

    id: str = Field(..., description="8-char unique identifier")
    name: str = Field(..., max_length=200, description="User-friendly name")
    source_type: str = Field(..., description="Must match a registered source")
    source_config: dict = Field(default_factory=dict, description="Validated against source's config_schema")
    action: TriggerAction
    conditions: List[TriggerCondition] = Field(default_factory=list, description="Event filters (AND logic)")
    enabled: bool = Field(default=True)
    cooldown_seconds: int = Field(default=0, ge=0, description="Min seconds between firings")
    last_fired: Optional[datetime] = Field(default=None)
    fire_count: int = Field(default=0, description="Total number of times this trigger has fired")
    state: dict = Field(default_factory=dict, description="Mutable state passed to source.check()")
    thread_id: str = Field(default="", description="Persistent thread for this trigger")
    created_at: datetime = Field(default_factory=utc_now)
    created_by: str = Field(default="agent", description="'agent' or 'user'")
    # Health tracking
    consecutive_errors: int = Field(default=0)
    last_error: Optional[str] = Field(default=None)
    last_error_at: Optional[datetime] = Field(default=None)
    health_status: Literal["healthy", "degraded", "failing"] = Field(default="healthy")
    # Pending events deferred because the thread was busy
    pending_events: List[dict] = Field(default_factory=list)

    @field_validator("last_fired", "created_at", "last_error_at")
    @classmethod
    def _datetimes_as_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        return ensure_aware_utc(value)


class TriggerExecution(BaseModel):
    """A single trigger execution record for audit logging."""

    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    trigger_id: str = ""
    trigger_name: str = ""
    timestamp: datetime = Field(default_factory=utc_now)
    status: Literal["success", "error", "partial", "deferred", "queued"] = "success"
    event_count: int = 1
    events_summary: str = ""
    response_summary: str = ""
    error_message: Optional[str] = None
    duration_seconds: float = 0.0
    action_type: str = ""

    @field_validator("timestamp")
    @classmethod
    def _timestamp_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)


class TriggerStore(BaseModel):
    """Per-user collection of triggers (serialized to JSON)."""

    user_id: str = Field(default="default")
    triggers: List[TriggerDefinition] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=utc_now)

    MAX_TRIGGERS: int = 50

    @field_validator("updated_at")
    @classmethod
    def _updated_at_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)

    def get_trigger(self, trigger_id: str) -> Optional[TriggerDefinition]:
        for t in self.triggers:
            if t.id == trigger_id:
                return t
        return None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class TriggerManager:
    """CRUD and execution engine for triggers."""

    def __init__(self, data_dir: Path):
        self.triggers_dir = data_dir / "triggers"
        self.triggers_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"TriggerManager initialized: {self.triggers_dir}")

    # -- locking ----------------------------------------------------------

    def _get_lock(self, user_id: str) -> threading.RLock:
        return _trigger_locks.get(user_id)

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """Context manager for atomic trigger store updates."""
        lock = self._get_lock(user_id)
        with lock:
            store = self._load(user_id)
            try:
                yield store
            finally:
                self._save(store)

    # -- persistence ------------------------------------------------------

    def _path_for(self, user_id: str) -> Path:
        safe = "".join(c for c in user_id if c.isalnum() or c in "-_") or "default"
        return self.triggers_dir / f"{safe}.json"

    def _load(self, user_id: str) -> TriggerStore:
        path = self._path_for(user_id)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                store = TriggerStore.model_validate(data)
                # Migrate: backfill thread_id for existing triggers
                migrated = False
                for trigger in store.triggers:
                    if not trigger.thread_id:
                        trigger.thread_id = f"trigger-{uuid.uuid4()}"
                        migrated = True
                if migrated:
                    self._save(store)
                return store
            except Exception as e:
                logger.error(f"Failed to load triggers for {user_id}: {e}")
                return TriggerStore(user_id=user_id)
        return TriggerStore(user_id=user_id)

    def _save(self, store: TriggerStore) -> bool:
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
        except Exception as e:
            logger.error(f"Failed to save triggers for {store.user_id}: {e}")
            return False

    # -- CRUD -------------------------------------------------------------

    def add_trigger(
        self,
        user_id: str,
        name: str,
        source_type: str,
        source_config: dict,
        action: TriggerAction,
        cooldown_seconds: int = 0,
        enabled: bool = True,
        created_by: str = "agent",
        thread_id: Optional[str] = None,
        conditions: Optional[List[TriggerCondition]] = None,
    ) -> Optional[TriggerDefinition]:
        """Create a new trigger. Returns the definition or None if at limit."""
        from ..triggers.sources import get_source

        # Validate source exists
        source = get_source(source_type)
        if source is None:
            logger.error(f"Unknown trigger source: {source_type}")
            return None

        # Validate config
        ok, msg = source.validate_config(source_config)
        if not ok:
            logger.error(f"Invalid source config for {source_type}: {msg}")
            return None

        with self.atomic_update(user_id) as store:
            if len(store.triggers) >= store.MAX_TRIGGERS:
                logger.warning(f"Trigger limit reached for user {user_id}")
                return None

            trigger_id = uuid.uuid4().hex[:8]
            trigger = TriggerDefinition(
                id=trigger_id,
                name=name,
                source_type=source_type,
                source_config=source_config,
                action=action,
                cooldown_seconds=cooldown_seconds,
                enabled=enabled,
                created_by=created_by,
                thread_id=thread_id or f"trigger-{uuid.uuid4()}",
                conditions=conditions or [],
            )
            # Inject trigger_id into state so sources can identify it
            trigger.state["trigger_id"] = trigger.id
            store.triggers.append(trigger)

        logger.info(f"Created trigger '{name}' ({trigger.id}) for user {user_id}")
        return trigger

    def update_trigger(self, user_id: str, trigger_id: str, **kwargs) -> bool:
        """Update fields on an existing trigger."""
        with self.atomic_update(user_id) as store:
            trigger = store.get_trigger(trigger_id)
            if trigger is None:
                return False

            for key, value in kwargs.items():
                if hasattr(trigger, key) and key not in ("id", "created_at"):
                    setattr(trigger, key, value)

        return True

    def delete_trigger(self, user_id: str, trigger_id: str) -> bool:
        """Remove a trigger permanently."""
        with self.atomic_update(user_id) as store:
            before = len(store.triggers)
            store.triggers = [t for t in store.triggers if t.id != trigger_id]
            return len(store.triggers) < before

    def delete_triggers_for_thread(self, user_id: str, thread_id: str) -> List[str]:
        """
        Remove all triggers owned by ``user_id`` that target ``thread_id``.

        Returns:
            Deleted trigger IDs.
        """
        lock = self._get_lock(user_id)
        with lock:
            store = self._load(user_id)
            deleted_ids = [t.id for t in store.triggers if t.thread_id == thread_id]
            if deleted_ids:
                store.triggers = [t for t in store.triggers if t.thread_id != thread_id]
                if not self._save(store):
                    raise RuntimeError(
                        f"Failed to save trigger cleanup for user {user_id}"
                    )
            return deleted_ids

    def get_triggers(self, user_id: str) -> List[TriggerDefinition]:
        """Get all triggers for a user (read-only snapshot)."""
        store = self._load(user_id)
        return list(store.triggers)

    def get_trigger(self, user_id: str, trigger_id: str) -> Optional[TriggerDefinition]:
        """Get a single trigger by ID."""
        store = self._load(user_id)
        return store.get_trigger(trigger_id)

    def find_triggers_by_id(self, trigger_id: str) -> List[Tuple[str, TriggerDefinition]]:
        """Find trigger-id matches across user stores.

        Trigger IDs are short random values and are unique only within a
        user's trigger store. Public webhook fires cannot trust a caller's
        ``?user_id=`` hint, so they search by trigger ID and then authenticate
        against the trigger's own shared secret before selecting an owner.
        """
        matches: List[Tuple[str, TriggerDefinition]] = []
        for user_id in self.get_all_users_with_triggers():
            trigger = self.get_trigger(user_id, trigger_id)
            if trigger is not None:
                matches.append((user_id, trigger))
        return matches

    def get_all_users_with_triggers(self) -> List[str]:
        """Get user IDs that have trigger files."""
        users = []
        if self.triggers_dir.exists():
            for p in self.triggers_dir.iterdir():
                # Skip per-user execution logs (*_executions.json) -- they
                # sit in the same dir but are not trigger stores. Treating
                # them as user files makes the ticker wipe the log every
                # poll cycle via atomic_update's save-on-exit.
                if p.is_file() and p.suffix == ".json" and not p.stem.endswith("_executions"):
                    users.append(p.stem)
        return sorted(users)

    # -- conditions -------------------------------------------------------

    @staticmethod
    def _evaluate_conditions(event: dict, conditions: List[TriggerCondition]) -> bool:
        """Return True if ALL conditions pass (AND logic)."""
        for cond in conditions:
            event_value = str(event.get(cond.field, ""))
            compare_value = cond.value
            if not cond.case_sensitive:
                event_value = event_value.lower()
                compare_value = compare_value.lower()

            if cond.operator == "equals" and event_value != compare_value:
                return False
            elif cond.operator == "not_equals" and event_value == compare_value:
                return False
            elif cond.operator == "contains" and compare_value not in event_value:
                return False
            elif cond.operator == "starts_with" and not event_value.startswith(compare_value):
                return False
            elif cond.operator == "matches_regex":
                try:
                    flags = 0 if cond.case_sensitive else re.IGNORECASE
                    if not re.search(cond.value, str(event.get(cond.field, "")), flags):
                        return False
                except re.error:
                    return False
        return True

    # -- execution log ----------------------------------------------------

    def _executions_path(self, user_id: str) -> Path:
        safe = "".join(c for c in user_id if c.isalnum() or c in "-_") or "default"
        return self.triggers_dir / f"{safe}_executions.json"

    def log_execution(self, user_id: str, execution: TriggerExecution) -> None:
        """Append an execution record, capping at MAX_EXECUTION_LOG entries."""
        path = self._executions_path(user_id)
        try:
            entries: list = []
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                # Defensive: tolerate stale non-list files from earlier bugs
                # instead of crashing when a TriggerStore dict was mistakenly
                # written here.
                entries = loaded if isinstance(loaded, list) else []
            entries.append(execution.model_dump(mode="json"))
            if len(entries) > MAX_EXECUTION_LOG:
                entries = entries[-MAX_EXECUTION_LOG:]
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(entries, default=str), encoding="utf-8")
            temp.replace(path)
        except Exception as e:
            logger.warning(f"Failed to log trigger execution: {e}")

    def get_executions(
        self,
        user_id: str,
        trigger_id: str | None = None,
        limit: int = 50,
    ) -> List[dict]:
        """Read execution history, optionally filtered by trigger_id."""
        path = self._executions_path(user_id)
        if not path.exists():
            return []
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                return []
            entries = loaded
            if trigger_id:
                entries = [e for e in entries if isinstance(e, dict) and e.get("trigger_id") == trigger_id]
            return list(reversed(entries[-limit:]))
        except Exception as e:
            logger.warning(f"Failed to read trigger executions: {e}")
            return []

    def delete_executions_for_triggers(self, user_id: str, trigger_ids: List[str]) -> int:
        """Remove execution-log entries for deleted triggers."""
        if not trigger_ids:
            return 0

        path = self._executions_path(user_id)
        if not path.exists():
            return 0

        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list):
                return 0
            trigger_set = set(trigger_ids)
            kept = [
                e for e in loaded
                if not (isinstance(e, dict) and e.get("trigger_id") in trigger_set)
            ]
            deleted = len(loaded) - len(kept)
            if deleted:
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(kept, default=str), encoding="utf-8")
                temp.replace(path)
            return deleted
        except Exception as e:
            logger.warning(f"Failed to delete trigger executions for {user_id}: {e}")
            return 0

    # -- polling (for poll-based sources) ---------------------------------

    def check_triggers(
        self,
        user_id: str,
        agent: Optional["NymeriaAgent"] = None,
    ) -> List[Tuple[TriggerDefinition, List[dict]]]:
        """Check all enabled triggers for a user.

        Returns list of ``(trigger, events)`` pairs where events is non-empty.
        Persists updated state and last_fired timestamps.
        Tracks health status per trigger on source errors.

        When *agent* is provided, performs a non-blocking busy check on each
        trigger's thread before returning events.  Events destined for a busy
        thread are stored in ``pending_events`` and retried next cycle, so
        the caller never blocks a thread-pool slot waiting for a lock.
        """
        from ..triggers.sources import get_source, AVAILABLE_SOURCES

        logger.info(
            f"[TRIGGER CHECK] user={user_id}, registered_sources={list(AVAILABLE_SOURCES.keys())}"
        )

        results: List[Tuple[TriggerDefinition, List[dict]]] = []

        with self.atomic_update(user_id) as store:
            now = utc_now()
            for trigger in store.triggers:
                if not trigger.enabled:
                    continue

                # Cooldown check
                if trigger.cooldown_seconds and trigger.last_fired:
                    elapsed = (
                        now - ensure_aware_utc(trigger.last_fired)
                    ).total_seconds()
                    if elapsed < trigger.cooldown_seconds:
                        continue

                # Exponential backoff for failing triggers
                if trigger.health_status == "failing" and trigger.consecutive_errors % 10 != 0:
                    continue

                source = get_source(trigger.source_type)
                if source is None:
                    logger.warning(f"Source '{trigger.source_type}' not registered, skipping trigger {trigger.id}")
                    continue

                try:
                    events = source.check(trigger.source_config, trigger.state)
                    # Reset health on success
                    if trigger.consecutive_errors > 0:
                        trigger.consecutive_errors = 0
                        trigger.health_status = "healthy"
                        trigger.last_error = None
                except Exception as e:
                    trigger.consecutive_errors += 1
                    trigger.last_error = str(e)[:200]
                    trigger.last_error_at = now
                    if trigger.consecutive_errors >= 5:
                        trigger.health_status = "failing"
                    elif trigger.consecutive_errors >= 2:
                        trigger.health_status = "degraded"
                    logger.error(f"Source check failed for trigger {trigger.id}: {e}")
                    continue

                # Apply conditions filter
                if events and trigger.conditions:
                    events = [e for e in events if self._evaluate_conditions(e, trigger.conditions)]

                # Merge any previously deferred events
                if trigger.pending_events:
                    events = trigger.pending_events + (events or [])
                    trigger.pending_events = []

                if not events:
                    continue

                # --- Thread busy check (agent_prompt actions only) ---
                # Non-agent actions (notify, create_todo) don't need a thread
                # lock, so they fire immediately regardless.
                thread_id = trigger.thread_id or f"trigger-{trigger.id}"
                if (
                    trigger.action.type == "agent_prompt"
                    and agent is not None
                    and agent._thread_locks.is_thread_busy(thread_id)
                ):
                    # Thread is busy: route through the pending-prompt
                    # queue so the running turn halts at its next
                    # sub-turn boundary and absorbs us. Falls back to
                    # the old "defer to pending_events" path only when
                    # enqueue fails or the queue backend is missing.
                    enqueued = self._enqueue_busy_trigger(
                        trigger=trigger,
                        events=events,
                        agent=agent,
                        user_id=user_id,
                        thread_id=thread_id,
                    )
                    if not enqueued:
                        trigger.pending_events = (trigger.pending_events + events)[-50:]
                        lock_info = agent._thread_locks.get_lock_info(thread_id)
                        held = lock_info.get("held_seconds", "?") if lock_info else "?"
                        logger.info(
                            f"[TRIGGER] Thread {thread_id} is busy (held {held}s), "
                            f"deferring {len(events)} event(s) for trigger "
                            f"'{trigger.name}' ({trigger.id})"
                        )
                        self.log_execution(user_id, TriggerExecution(
                            trigger_id=trigger.id,
                            trigger_name=trigger.name,
                            event_count=len(events),
                            events_summary=f"Deferred: thread busy (held {held}s)",
                            action_type=trigger.action.type,
                            status="deferred",
                        ))
                    continue

                trigger.last_fired = now
                trigger.fire_count += len(events)
                results.append((trigger, events))

        return results

    def _enqueue_busy_trigger(
        self,
        *,
        trigger: TriggerDefinition,
        events: List[dict],
        agent: "NymeriaAgent",
        user_id: str,
        thread_id: str,
    ) -> bool:
        """Enqueue the templated agent prompt for a busy thread.

        Returns True if at least one prompt was enqueued (so the caller
        can advance ``last_fired`` / ``fire_count``); False on any
        failure, signalling the caller should fall back to the legacy
        ``pending_events`` defer path.

        Each event is templated and enqueued separately so per-event
        history visibility is preserved (the same way fire_action would
        have rendered them before batching).
        """
        try:
            from .pending_prompt_queue import get_pending_queue, make_pending_prompt
        except Exception as e:
            logger.debug(
                f"Pending-prompt queue unavailable, falling back to defer: {e}"
            )
            return False

        action = trigger.action
        template = (
            action.config.get("prompt_template")
            or action.config.get("prompt")
            or "Trigger {trigger_name} fired."
        )
        backend = get_pending_queue()
        enqueued = 0
        for event in events:
            template_vars = {
                **event,
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
                "fired_at": utc_now().isoformat(),
            }
            prompt_text = _safe_format(template, template_vars)
            pending = make_pending_prompt(
                message=prompt_text,
                source="trigger",
                source_id=trigger.id,
                source_label=trigger.name,
                user_id=user_id,
                is_autonomous=True,
                fanout_mailbox=None,
                consumer_loop=None,
            )
            try:
                backend.enqueue(thread_id, pending)
                enqueued += 1
            except Exception as e:
                logger.warning(
                    f"Failed to enqueue trigger event for {trigger.id}: {e}"
                )

        if not enqueued:
            return False

        lock_info = agent._thread_locks.get_lock_info(thread_id)
        held = lock_info.get("held_seconds", "?") if lock_info else "?"
        logger.info(
            f"[TRIGGER] Thread {thread_id} busy (held {held}s); "
            f"queued {enqueued} prompt(s) for trigger "
            f"'{trigger.name}' ({trigger.id})"
        )
        self.log_execution(user_id, TriggerExecution(
            trigger_id=trigger.id,
            trigger_name=trigger.name,
            event_count=enqueued,
            events_summary=f"Queued: thread busy (held {held}s)",
            action_type=trigger.action.type,
            status="queued",
        ))
        # Advance ``last_fired`` so the cooldown clock starts now and
        # we don't immediately re-enqueue on the next poll cycle.
        trigger.last_fired = utc_now()
        trigger.fire_count += enqueued
        return True

    # -- action execution -------------------------------------------------

    def fire_action(
        self,
        trigger: TriggerDefinition,
        event: dict,
        agent: "NymeriaAgent | TurnExecutor",
        user_id: str,
    ) -> None:
        """Execute a trigger's action with event data interpolated into templates.

        ``agent`` may be a local ``NymeriaAgent`` (slim) or any
        ``TurnExecutor`` (Docker worker, which uses ``APIClientExecutor``
        to relay the turn into the API runtime).
        """
        action = trigger.action
        template_vars = {
            **event,
            "trigger_id": trigger.id,
            "trigger_name": trigger.name,
            "fired_at": utc_now().isoformat(),
        }

        start = _time.monotonic()
        execution = TriggerExecution(
            trigger_id=trigger.id,
            trigger_name=trigger.name,
            event_count=1,
            events_summary=str(event)[:200],
            action_type=action.type,
        )

        try:
            if action.type == "agent_prompt":
                self._fire_agent_prompt(action.config, template_vars, agent, user_id, trigger)
            elif action.type == "notify":
                self._fire_notify(action.config, template_vars, user_id, trigger)
            elif action.type == "create_todo":
                self._fire_create_todo(action.config, template_vars, user_id)
            else:
                logger.error(f"Unknown action type: {action.type}")
            execution.status = "success"
        except Exception as e:
            execution.status = "error"
            execution.error_message = str(e)[:200]
            logger.error(
                f"[TRIGGER] Action failed for trigger '{trigger.name}' ({trigger.id}): {e}",
                exc_info=True,
            )
            self._publish_trigger_error(trigger, user_id, str(e))
        finally:
            execution.duration_seconds = round(_time.monotonic() - start, 2)
            self.log_execution(user_id, execution)

    def fire_action_batch(
        self,
        trigger: TriggerDefinition,
        events: List[dict],
        agent: "NymeriaAgent | TurnExecutor",
        user_id: str,
    ) -> None:
        """Execute a trigger's action for a batch of events.

        For ``agent_prompt`` actions, multiple events are merged into a single
        prompt so only ONE LLM call is made per poll cycle (instead of N).
        For other action types, each event is fired individually.
        """
        if not events:
            return

        if len(events) == 1 or trigger.action.type != "agent_prompt":
            for event in events:
                self.fire_action(trigger, event, agent, user_id)
            return

        # Batch agent_prompt: render each event with the template, then
        # combine them into a single prompt.
        action = trigger.action
        template = (
            action.config.get("prompt_template")
            or action.config.get("prompt")
            or "Trigger {trigger_name} fired."
        )
        rendered_items = []

        for i, event in enumerate(events, 1):
            template_vars = {
                **event,
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
                "fired_at": utc_now().isoformat(),
            }
            rendered_items.append(f"--- Item {i} ---\n{_safe_format(template, template_vars)}")

        batch_prompt = (
            f"[Batch: {len(events)} events from trigger '{trigger.name}']\n\n"
            + "\n\n".join(rendered_items)
            + "\n\n---\nProcess all items above."
        )

        all_attachments: List[Dict[str, str]] = []
        for event in events:
            event_atts = event.get("attachments")
            if event_atts:
                all_attachments.extend(event_atts)

        thread_id = trigger.thread_id or f"trigger-{trigger.id}"
        _start = _time.monotonic()
        att_note = f", attachments={len(all_attachments)}" if all_attachments else ""
        logger.info(
            f"[TRIGGER] === START === thread={thread_id}, user={user_id}, "
            f"trigger={trigger.name} ({trigger.id}), batched={len(events)} events{att_note}"
        )

        execution = TriggerExecution(
            trigger_id=trigger.id,
            trigger_name=trigger.name,
            event_count=len(events),
            events_summary=str(events[0])[:200],
            action_type=action.type,
        )

        try:
            task_id = f"trigger-{trigger.id}"

            response_parts, _thinking_parts, iteration_limit_hit = self._stream_live(
                agent, batch_prompt, thread_id, user_id, task_id,
                attachments=all_attachments or None,
                task_started_data={
                    "prompt": batch_prompt,
                    "trigger_id": trigger.id,
                    "trigger_name": trigger.name,
                    "batch_size": len(events),
                },
                source_id=trigger.id,
                source_label=trigger.name,
            )
            response = "".join(response_parts)

            execution.status = "partial" if iteration_limit_hit else "success"
            execution.response_summary = response[:200]

            self._publish_trigger_completion(
                trigger=trigger,
                thread_id=thread_id,
                user_id=user_id,
                response=response,
                event_count=len(events),
                partial=iteration_limit_hit,
            )

            _elapsed = _time.monotonic() - _start
            logger.info(
                f"[TRIGGER] === END === thread={thread_id}, "
                f"trigger={trigger.name}, batched={len(events)}, "
                f"response_len={len(response)}, partial={iteration_limit_hit}, "
                f"elapsed={_elapsed:.1f}s"
            )
        except Exception as e:
            execution.status = "error"
            execution.error_message = str(e)[:200]
            logger.error(
                f"[TRIGGER] Batched action failed for trigger '{trigger.name}' ({trigger.id}): {e}",
                exc_info=True,
            )
            self._publish_trigger_error(trigger, user_id, str(e))
        finally:
            execution.duration_seconds = round(_time.monotonic() - _start, 2)
            self.log_execution(user_id, execution)

    def _fire_agent_prompt(
        self,
        config: dict,
        template_vars: dict,
        agent: "NymeriaAgent | TurnExecutor",
        user_id: str,
        trigger: TriggerDefinition,
    ) -> None:
        """Send a prompt to the agent, streaming events live."""
        template = (
            config.get("prompt_template")
            or config.get("prompt")
            or "Trigger {trigger_name} fired."
        )
        prompt = _safe_format(template, template_vars)
        thread_id = trigger.thread_id or f"trigger-{trigger.id}"
        task_id = f"trigger-{trigger.id}"

        # Extract attachments from event data (e.g. email attachments from Outlook trigger)
        event_attachments = template_vars.get("attachments")

        _start = _time.monotonic()
        att_note = f", attachments={len(event_attachments)}" if event_attachments else ""
        logger.info(
            f"[TRIGGER] === START === thread={thread_id}, user={user_id}, "
            f"trigger={trigger.name} ({trigger.id}){att_note}, prompt={prompt[:100]}..."
        )

        response_parts, _thinking_parts, iteration_limit_hit = self._stream_live(
            agent, prompt, thread_id, user_id, task_id,
            attachments=event_attachments,
            task_started_data={
                "prompt": prompt,
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
            },
            source_id=trigger.id,
            source_label=trigger.name,
        )
        response = "".join(response_parts)

        self._publish_trigger_completion(
            trigger=trigger,
            thread_id=thread_id,
            user_id=user_id,
            response=response,
            event_count=1,
            partial=iteration_limit_hit,
        )

        _elapsed = _time.monotonic() - _start
        logger.info(
            f"[TRIGGER] === END === thread={thread_id}, "
            f"trigger={trigger.name}, response_len={len(response)}, "
            f"partial={iteration_limit_hit}, elapsed={_elapsed:.1f}s"
        )

    def _stream_live(
        self,
        agent: "NymeriaAgent | TurnExecutor",
        prompt: str,
        thread_id: str,
        user_id: str,
        task_id: str,
        attachments: Optional[List[Dict[str, str]]] = None,
        task_started_data: Optional[Dict[str, Any]] = None,
        source_id: Optional[str] = None,
        source_label: Optional[str] = None,
    ) -> Tuple[List[str], List[str], bool]:
        """Stream through the agent, publishing each event live.

        If *task_started_data* is provided, the ``task_started`` event is
        published when the first chunk arrives (i.e. after the thread lock
        is acquired), not before.  This prevents the frontend from entering
        streaming mode while the user's chat is still active.

        Returns (response_parts, thinking_parts, iteration_limit_hit).
        """
        from .event_bus import publish_agent_stream_chunk, publish_autonomous_event
        from .pending_prompt_queue import PENDING_QUEUE_META_EVENT_TYPES
        from .stream_bridge import stream_and_collect

        started_published = False

        def handle_chunk(chunk: Dict[str, Any], collection) -> None:
            nonlocal started_published
            # Skip queue-meta events when deciding whether the API has
            # actually started working on our prompt. The autonomous
            # queuer in agent.astream() observes fanout, so prompt_injected
            # and prompt_absorbed arrive on this stream before any
            # response chunks — firing task_started on those would publish
            # an empty trigger completion. See pending_prompt_queue.py.
            if (
                not started_published
                and task_started_data is not None
                and chunk.get("type") not in PENDING_QUEUE_META_EVENT_TYPES
            ):
                publish_autonomous_event(
                    event_type="task_started",
                    thread_id=thread_id,
                    user_id=user_id,
                    task_id=task_id,
                    data=task_started_data,
                )
                started_published = True

            chunk_type = chunk.get("type")
            logger.debug(
                f"[TRIGGER] thread={thread_id}: chunk #{collection.chunk_count} "
                f"type={chunk_type}"
            )
            publish_agent_stream_chunk(
                chunk,
                thread_id=thread_id,
                user_id=user_id,
                task_id=task_id,
            )

            if chunk_type == "error":
                error_content = chunk.get("content", "")
                error_code = chunk.get("code", "unknown")
                logger.error(
                    f"[TRIGGER] Stream error on thread {thread_id}: "
                    f"code={error_code}, content={error_content}"
                )

            elif chunk_type == "iteration_limit":
                logger.warning(
                    f"[TRIGGER] Iteration limit on thread {thread_id}: "
                    f"scope={chunk.get('scope')}, "
                    f"reason={chunk.get('reason')}, "
                    f"max_iterations={chunk.get('max_iterations')}. "
                    f"Using partial response."
                )

        def stream_error_message(chunk: Dict[str, Any]) -> str:
            error_content = chunk.get("content", "")
            error_code = chunk.get("code", "unknown")
            return error_content or f"Trigger stream error (code={error_code})"

        astream_kwargs: Dict[str, Any] = {
            "message": prompt,
            "thread_id": thread_id,
            "user_id": user_id,
            "_is_self_invoke": True,
            "attachments": attachments,
            "source": "trigger",
        }
        if source_id is not None:
            astream_kwargs["source_id"] = source_id
        if source_label is not None:
            astream_kwargs["source_label"] = source_label
        result = stream_and_collect(
            agent,
            astream_kwargs=astream_kwargs,
            on_chunk=handle_chunk,
            error_message_factory=stream_error_message,
        )

        # If no response chunks, promote thinking to response
        response_parts = result.response_parts
        if not response_parts and result.thinking_parts:
            response_parts = result.thinking_parts

        return response_parts, result.thinking_parts, result.iteration_limit_hit

    def _publish_trigger_completion(
        self,
        trigger: TriggerDefinition,
        thread_id: str,
        user_id: str,
        response: str,
        event_count: int = 1,
        partial: bool = False,
    ) -> None:
        """Publish task_completed and log to activity feed."""
        from .activity_log import ActivityType, log_activity
        from .event_bus import publish_autonomous_event

        task_id = f"trigger-{trigger.id}"

        completed_data = {
            "content": response,
            "trigger_id": trigger.id,
            "trigger_name": trigger.name,
        }
        if partial:
            completed_data["partial"] = True

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=user_id,
            task_id=task_id,
            data=completed_data,
        )

        activity_msg = f"{trigger.name}: processed {event_count} event(s)"
        if partial:
            activity_msg += " (partial — hit iteration limit)"

        log_activity(
            ActivityType.TRIGGER_COMPLETED,
            activity_msg,
            user_id=user_id,
            thread_id=thread_id,
            metadata={
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
                "event_count": event_count,
                "partial": partial,
            },
        )

    def _publish_trigger_error(
        self,
        trigger: TriggerDefinition,
        user_id: str,
        error_msg: str,
    ) -> None:
        """Log trigger failure to activity feed and publish SSE error event."""
        from .activity_log import ActivityType, log_activity
        from .event_bus import publish_autonomous_event

        thread_id = trigger.thread_id or f"trigger-{trigger.id}"
        task_id = f"trigger-{trigger.id}"

        # Truncate to avoid leaking sensitive provider diagnostics
        safe_error = error_msg[:300] if error_msg else "Unknown error"

        # Activity log entry
        log_activity(
            ActivityType.TRIGGER_COMPLETED,
            f"{trigger.name}: error — {safe_error[:200]}",
            user_id=user_id,
            thread_id=thread_id,
            metadata={
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
                "status": "error",
                "error": safe_error,
            },
        )

        # SSE event so frontend can exit streaming state
        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=user_id,
            task_id=task_id,
            data={
                "content": f"Trigger '{trigger.name}' failed: {safe_error}",
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
                "status": "error",
                "error": safe_error,
            },
        )

    def _fire_notify(
        self,
        config: dict,
        template_vars: dict,
        user_id: str,
        trigger: TriggerDefinition,
    ) -> None:
        """Send a notification (no LLM call)."""
        from ..tools.notify import notify

        template = config.get("message_template", "Trigger {trigger_name} fired.")
        message = _safe_format(template, template_vars)
        platform = config.get("platform") or "auto"
        thread_id = trigger.thread_id or f"trigger-{trigger.id}"

        logger.info(f"[TRIGGER] Sending notification: {message[:100]}...")
        result = notify.invoke(
            {"message": message, "platform": platform},
            config={"configurable": {"user_id": user_id, "thread_id": thread_id}},
        )
        logger.info(f"[TRIGGER] Notify result: {result}")

    def _fire_create_todo(self, config: dict, template_vars: dict, user_id: str) -> None:
        """Create a TODO item (no LLM call)."""
        from ..config import get_settings
        from .todo_manager import TodoManager

        template = config.get("task_template", "Triggered: {trigger_name}")
        task = _safe_format(template, template_vars)

        settings = get_settings()
        todo_manager = TodoManager(settings.data_dir)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.add_item(
                task=task,
                created_by="trigger",
                thread_id=f"trigger_{template_vars.get('trigger_id', 'auto')}",
            )
            if item:
                logger.info(f"[TRIGGER] Created TODO {item.id}: {task[:80]}")

                # Sync schedule if the TODO has scheduled_for
                scheduled_for_str = config.get("scheduled_for")
                if scheduled_for_str:
                    from ..core.time_utils import parse_scheduled_time
                    parsed = parse_scheduled_time(scheduled_for_str)
                    if parsed:
                        todo_list.update_item(item.id, scheduled_for=parsed)
            else:
                logger.warning("[TRIGGER] Failed to create TODO (at limit?)")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_format(template: str, variables: dict) -> str:
    """Format a template string, ignoring missing keys."""
    try:
        return template.format_map(_DefaultDict(variables))
    except Exception:
        return template


class _DefaultDict(dict):
    """Dict that returns ``{key}`` for missing keys instead of raising."""

    def __missing__(self, key):
        return f"{{{key}}}"
