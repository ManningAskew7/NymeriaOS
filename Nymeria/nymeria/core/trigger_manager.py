"""Core trigger engine -- models, CRUD, polling, and action execution.

Triggers are event-driven automations that react to external events
(webhooks, API changes, incoming data).  They complement recurring TODOs,
which handle time-based autonomous work.

Storage follows the same pattern as TodoManager: one JSON file per user
in ``data_dir/triggers/``.
"""

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)

# Thread-safe locks keyed by user_id
_trigger_locks: Dict[str, threading.RLock] = {}
_locks_lock = threading.Lock()


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
            "create_todo: {task_template, priority?, scheduled_for?}."
        ),
    )


class TriggerDefinition(BaseModel):
    """A single trigger instance created by the user or agent."""

    id: str = Field(..., description="8-char unique identifier")
    name: str = Field(..., max_length=200, description="User-friendly name")
    source_type: str = Field(..., description="Must match a registered source")
    source_config: dict = Field(default_factory=dict, description="Validated against source's config_schema")
    action: TriggerAction
    enabled: bool = Field(default=True)
    cooldown_seconds: int = Field(default=0, ge=0, description="Min seconds between firings")
    last_fired: Optional[datetime] = Field(default=None)
    fire_count: int = Field(default=0, description="Total number of times this trigger has fired")
    state: dict = Field(default_factory=dict, description="Mutable state passed to source.check()")
    thread_id: str = Field(default="", description="Persistent thread for this trigger")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: str = Field(default="agent", description="'agent' or 'user'")


class TriggerStore(BaseModel):
    """Per-user collection of triggers (serialized to JSON)."""

    user_id: str = Field(default="default")
    triggers: List[TriggerDefinition] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    MAX_TRIGGERS: int = 50

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
        with _locks_lock:
            if user_id not in _trigger_locks:
                _trigger_locks[user_id] = threading.RLock()
            return _trigger_locks[user_id]

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
            store.updated_at = datetime.utcnow()
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
                thread_id=f"trigger-{uuid.uuid4()}",
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

    def get_triggers(self, user_id: str) -> List[TriggerDefinition]:
        """Get all triggers for a user (read-only snapshot)."""
        store = self._load(user_id)
        return list(store.triggers)

    def get_trigger(self, user_id: str, trigger_id: str) -> Optional[TriggerDefinition]:
        """Get a single trigger by ID."""
        store = self._load(user_id)
        return store.get_trigger(trigger_id)

    def get_all_users_with_triggers(self) -> List[str]:
        """Get user IDs that have trigger files."""
        users = []
        if self.triggers_dir.exists():
            for p in self.triggers_dir.iterdir():
                if p.is_file() and p.suffix == ".json":
                    users.append(p.stem)
        return sorted(users)

    # -- polling (for poll-based sources) ---------------------------------

    def check_triggers(self, user_id: str) -> List[Tuple[TriggerDefinition, List[dict]]]:
        """Check all enabled triggers for a user.

        Returns list of ``(trigger, events)`` pairs where events is non-empty.
        Persists updated state and last_fired timestamps.
        """
        from ..triggers.sources import get_source, AVAILABLE_SOURCES

        logger.info(
            f"[TRIGGER CHECK] user={user_id}, registered_sources={list(AVAILABLE_SOURCES.keys())}"
        )

        results: List[Tuple[TriggerDefinition, List[dict]]] = []

        with self.atomic_update(user_id) as store:
            now = datetime.utcnow()
            for trigger in store.triggers:
                if not trigger.enabled:
                    continue

                # Cooldown check
                if trigger.cooldown_seconds and trigger.last_fired:
                    elapsed = (now - trigger.last_fired).total_seconds()
                    if elapsed < trigger.cooldown_seconds:
                        continue

                source = get_source(trigger.source_type)
                if source is None:
                    logger.warning(f"Source '{trigger.source_type}' not registered, skipping trigger {trigger.id}")
                    continue

                try:
                    events = source.check(trigger.source_config, trigger.state)
                except Exception as e:
                    logger.error(f"Source check failed for trigger {trigger.id}: {e}")
                    continue

                if events:
                    trigger.last_fired = now
                    trigger.fire_count += len(events)
                    results.append((trigger, events))

        return results

    # -- action execution -------------------------------------------------

    def fire_action(
        self,
        trigger: TriggerDefinition,
        event: dict,
        agent: "NymeriaAgent",
        user_id: str,
    ) -> None:
        """Execute a trigger's action with event data interpolated into templates."""
        action = trigger.action
        # Merge event data with trigger metadata for template interpolation
        template_vars = {
            **event,
            "trigger_id": trigger.id,
            "trigger_name": trigger.name,
            "fired_at": datetime.utcnow().isoformat(),
        }

        try:
            if action.type == "agent_prompt":
                self._fire_agent_prompt(action.config, template_vars, agent, user_id, trigger)
            elif action.type == "notify":
                self._fire_notify(action.config, template_vars)
            elif action.type == "create_todo":
                self._fire_create_todo(action.config, template_vars, user_id)
            else:
                logger.error(f"Unknown action type: {action.type}")
        except Exception as e:
            logger.error(f"Action execution failed for trigger {trigger.id}: {e}", exc_info=True)

    def fire_action_batch(
        self,
        trigger: TriggerDefinition,
        events: List[dict],
        agent: "NymeriaAgent",
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
            # Single event or non-prompt action: fire individually
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
                "fired_at": datetime.utcnow().isoformat(),
            }
            rendered_items.append(f"--- Item {i} ---\n{_safe_format(template, template_vars)}")

        batch_prompt = (
            f"[Batch: {len(events)} events from trigger '{trigger.name}']\n\n"
            + "\n\n".join(rendered_items)
            + "\n\n---\nProcess all items above."
        )

        thread_id = trigger.thread_id or f"trigger-{trigger.id}"
        logger.info(
            f"[TRIGGER] Firing batched agent_prompt ({len(events)} events) "
            f"on thread={thread_id}: {batch_prompt[:120]}..."
        )

        response_parts, buffered_events = self._stream_and_buffer(
            agent, batch_prompt, thread_id, user_id
        )
        response = "".join(response_parts)

        self._publish_trigger_events(
            agent=agent,
            trigger=trigger,
            thread_id=thread_id,
            user_id=user_id,
            prompt=batch_prompt,
            response=response,
            buffered_events=buffered_events,
            event_count=len(events),
        )

        logger.info(f"[TRIGGER] Batched agent_prompt completed, response_len={len(response)}")

    def _fire_agent_prompt(
        self,
        config: dict,
        template_vars: dict,
        agent: "NymeriaAgent",
        user_id: str,
        trigger: TriggerDefinition,
    ) -> None:
        """Send a prompt to the agent, buffering events for mute-aware publishing."""
        template = (
            config.get("prompt_template")
            or config.get("prompt")
            or "Trigger {trigger_name} fired."
        )
        prompt = _safe_format(template, template_vars)
        thread_id = trigger.thread_id or f"trigger-{trigger.id}"

        logger.info(f"[TRIGGER] Firing agent_prompt on thread={thread_id}: {prompt[:100]}...")

        response_parts, buffered_events = self._stream_and_buffer(
            agent, prompt, thread_id, user_id
        )
        response = "".join(response_parts)

        self._publish_trigger_events(
            agent=agent,
            trigger=trigger,
            thread_id=thread_id,
            user_id=user_id,
            prompt=prompt,
            response=response,
            buffered_events=buffered_events,
            event_count=1,
        )

        logger.info(f"[TRIGGER] agent_prompt completed, response_len={len(response)}")

    def _stream_and_buffer(
        self,
        agent: "NymeriaAgent",
        prompt: str,
        thread_id: str,
        user_id: str,
    ) -> Tuple[List[str], List[dict]]:
        """Stream through the agent and buffer events for deferred publishing.

        Returns (response_parts, buffered_events).
        """
        response_parts: List[str] = []
        thinking_parts: List[str] = []
        buffered_events: List[dict] = []

        for chunk in agent.stream(
            message=prompt,
            thread_id=thread_id,
            user_id=user_id,
            _is_self_invoke=True,
        ):
            chunk_type = chunk.get("type")

            if chunk_type == "tool_call":
                buffered_events.append({
                    "event_type": "tool_call",
                    "data": {
                        "id": chunk.get("id"),
                        "name": chunk.get("name"),
                        "args": chunk.get("args", {}),
                    },
                })
            elif chunk_type == "tool_result":
                buffered_events.append({
                    "event_type": "tool_result",
                    "data": {
                        "id": chunk.get("id"),
                        "name": chunk.get("name"),
                        "result": chunk.get("result"),
                    },
                })
            elif chunk_type == "thinking":
                content = chunk.get("content", "")
                if content:
                    thinking_parts.append(content)
                buffered_events.append({
                    "event_type": "thinking",
                    "data": {"content": content},
                })
            elif chunk_type == "response":
                content = chunk.get("content", "")
                if content:
                    response_parts.append(content)
                    buffered_events.append({
                        "event_type": "response",
                        "data": {"content": content},
                    })

        # If no response chunks, promote thinking to response (same as ticker)
        if not response_parts and thinking_parts:
            response_parts = thinking_parts
            for i, evt in enumerate(buffered_events):
                if evt["event_type"] == "thinking":
                    buffered_events[i] = {
                        "event_type": "response",
                        "data": {"content": evt["data"]["content"]},
                    }

        return response_parts, buffered_events

    def _publish_trigger_events(
        self,
        agent: "NymeriaAgent",
        trigger: TriggerDefinition,
        thread_id: str,
        user_id: str,
        prompt: str,
        response: str,
        buffered_events: List[dict],
        event_count: int = 1,
    ) -> None:
        """Check mute flag and publish buffered events to the event bus."""
        from .activity_log import ActivityType, log_activity
        from .event_bus import publish_autonomous_event
        from ..tools.visibility import get_and_clear_mute_flag

        task_id = f"trigger-{trigger.id}"

        mute_info = get_and_clear_mute_flag(thread_id)
        if mute_info and mute_info.get("muted"):
            visibility = "activity"
            logger.info(f"[TRIGGER] Response muted: {mute_info.get('reason', 'no reason')}")
            agent.mark_last_turn_muted(thread_id)
        else:
            visibility = "full"
            # Flush task_started + buffered events to frontend
            publish_autonomous_event(
                event_type="task_started",
                thread_id=thread_id,
                user_id=user_id,
                task_id=task_id,
                data={
                    "prompt": prompt,
                    "trigger_id": trigger.id,
                    "trigger_name": trigger.name,
                },
            )
            for event in buffered_events:
                publish_autonomous_event(
                    event_type=event["event_type"],
                    thread_id=thread_id,
                    user_id=user_id,
                    task_id=task_id,
                    data=event["data"],
                )

        # Always publish task_completed (activity log needs it regardless of visibility)
        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=user_id,
            task_id=task_id,
            data={
                "visibility": visibility,
                "content": response,
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
            },
        )

        # Persist to activity log
        log_activity(
            ActivityType.TRIGGER_COMPLETED,
            f"{trigger.name}: processed {event_count} event(s)",
            user_id=user_id,
            thread_id=thread_id,
            metadata={
                "trigger_id": trigger.id,
                "trigger_name": trigger.name,
                "event_count": event_count,
                "visibility": visibility,
            },
        )

    def _fire_notify(self, config: dict, template_vars: dict) -> None:
        """Send a notification (no LLM call)."""
        from ..tools.notify import notify

        template = config.get("message_template", "Trigger {trigger_name} fired.")
        message = _safe_format(template, template_vars)
        platform = config.get("platform", "auto")

        logger.info(f"[TRIGGER] Sending notification: {message[:100]}...")
        result = notify.invoke({"message": message, "platform": platform})
        logger.info(f"[TRIGGER] Notify result: {result}")

    def _fire_create_todo(self, config: dict, template_vars: dict, user_id: str) -> None:
        """Create a TODO item (no LLM call)."""
        from ..config import get_settings
        from .todo_manager import TodoManager, TodoPriority

        template = config.get("task_template", "Triggered: {trigger_name}")
        task = _safe_format(template, template_vars)
        priority_str = config.get("priority", "medium")
        priority = TodoPriority(priority_str) if priority_str else None

        settings = get_settings()
        todo_manager = TodoManager(settings.data_dir)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.add_item(
                task=task,
                priority=priority,
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
