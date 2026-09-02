"""Core trigger engine -- models, CRUD, polling, and action execution.

Triggers are event-driven automations that react to external events
(webhooks, API changes, incoming data).  They complement recurring TODOs,
which handle time-based autonomous work.

Storage follows the same pattern as TodoManager: one JSON file per user
in ``data_dir/triggers/``.
"""

import inspect
import json
import logging
import threading
import time as _time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

from .conditions import HookCondition as TriggerCondition  # re-export (shared model)
from .conditions import evaluate_conditions
from .keyed_locks import KeyedRLockMap
from .storage_paths import (
    compare_fingerprint,
    quarantine_corrupt_file,
    read_store_fingerprint,
    record_store_fingerprint,
    upgrade_legacy_store_fingerprint,
    safe_path_segment,
    write_text_atomic,
)
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

    type: Literal["agent_prompt", "notify", "create_todo", "run_workflow"] = Field(
        ..., description="Action type"
    )
    config: dict = Field(
        default_factory=dict,
        description=(
            "Action-specific config. "
            "agent_prompt: {prompt_template (or prompt), thread_id?}. "
            "notify: {message_template, platform?}. "
            "create_todo: {task_template, scheduled_for?}. "
            "run_workflow: {workflow_id, params?} (the raw event dict is "
            "passed as the workflow's 'event' parameter when it declares one)."
        ),
    )


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
    # Health tracking. ONE counter fed by two outcome planes (#154):
    # source checks and action fires. `last_error_kind` scopes the reset: a
    # successful SOURCE check says nothing about a broken ACTION, so it must
    # not heal an action-failure streak (source checks succeed every poll,
    # which would zero the streak before it could ever reach a threshold);
    # an ACTION success implies the whole pipeline worked and resets either.
    consecutive_errors: int = Field(default=0)
    last_error: Optional[str] = Field(default=None)
    last_error_at: Optional[datetime] = Field(default=None)
    last_error_kind: Optional[Literal["source", "action"]] = Field(default=None)
    health_status: Literal["healthy", "degraded", "failing"] = Field(default="healthy")
    # Action-failure policy (#264), PARALLEL to consecutive_errors and never
    # shared with it. Two reasons, both forced rather than stylistic:
    # `consecutive_errors` counts backed-off polls as well as errors (see the
    # backoff in check_triggers, deliberate), so no honest threshold can be
    # read off it; and it is fed by BOTH planes, while auto-pause is an
    # action-plane verdict only (a source outage self-heals, so pausing on it
    # would stop triggers that would have recovered). Reset by an action
    # success or an explicit resume; never touched by the source plane or the
    # backoff. Same rule as the #247 delivery streak, see
    # core/delivery_accounting.py.
    action_failures: int = Field(default=0)
    # When the #264 policy last spoke about this trigger. A trigger that
    # FLAPS (fail, fail, succeed, repeat) never reaches the pause threshold
    # because each success resets the streak, so without this it would alert
    # once per episode forever, on external destinations that deliberately
    # bypass the in-app notification level. NOT reset by a success (that is
    # the flap), only by an explicit resume.
    last_policy_alert_at: Optional[datetime] = Field(default=None)
    # Set when the policy gave up on this trigger. Distinct from `enabled`,
    # which stays purely the user's own toggle, so a paused trigger can be
    # told apart from one the user switched off (and re-enabling never
    # silently resumes a trigger the system stopped). Cleared only by
    # resume_trigger().
    auto_paused_at: Optional[datetime] = Field(default=None)
    # #306, SOURCE plane only, and deliberately NOT `last_policy_alert_at`.
    # Sharing that field looked free (a failing source means the action never
    # runs, so the two planes rarely fail at once) but the quantity actually
    # shared is a THREE-HOUR WINDOW, not an instant: a source blip that
    # stamped it would then suppress the action plane's alert stage, and
    # because that gate is `count == alert_after` exactly, the alert would be
    # DROPPED rather than delayed. Nothing clears either stamp on recovery.
    last_source_alert_at: Optional[datetime] = Field(default=None)
    # Pending events deferred because the thread was busy
    pending_events: List[dict] = Field(default_factory=list)

    @field_validator(
        "last_fired", "created_at", "last_error_at", "auto_paused_at",
        "last_source_alert_at",
        "last_policy_alert_at",
    )
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


def describe_resume(trigger_id: str, summary: dict) -> str:
    """One copy of the resume outcome copy for every surface.

    Resume is idempotent, so the honest message depends on what was
    actually there: a pause, a stale failing streak, or nothing. The
    agent tool and the slash command both render this; keeping two
    hand-written copies in step was not going to happen.
    """
    if summary["was_paused"]:
        head = (
            f"Resumed trigger {trigger_id} after "
            f"{summary['action_failures']} failed action(s)."
        )
    elif summary["previous_health"] != "healthy":
        head = (
            f"Trigger {trigger_id} was not paused; cleared its "
            f"{summary['previous_health']} health "
            f"({summary['consecutive_errors']} error(s)), so it polls at "
            f"full rate again."
        )
    else:
        head = f"Trigger {trigger_id} was already healthy; nothing to clear."
    if not summary["enabled"]:
        # Resume is orthogonal to the user's toggle, so say so rather
        # than let the caller assume the trigger is now running.
        head += " It is still DISABLED, so it will not run until enabled."
    return head


def _source_accepts_thread_id(source: Any) -> bool:
    """Whether ``source.check`` takes the ``thread_id`` keyword.

    Sources are hot-reloadable plugin files, so one authored against the
    older ``check(config, state, user_id)`` contract may still be installed;
    the poll loop keeps calling it the old way instead of failing every cycle.
    """
    try:
        params = inspect.signature(source.check).parameters
    except (TypeError, ValueError):
        return False
    return "thread_id" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _apply_health_outcome(
    trigger: TriggerDefinition,
    error: Optional[str],
    now: datetime,
    *,
    kind: Literal["source", "action"],
) -> bool:
    """Apply one source-check or action outcome to the shared health fields.

    The ONE copy of the health LABEL thresholds for both writers
    (``check_triggers`` for source checks, ``_record_action_health`` for
    action fires). Returns True on the transition into "failing".

    That transition drives an owner alert on the SOURCE plane only. The
    action plane stopped using it when the #264 policy landed: that plane
    alerts and auto-pauses off its own ``action_failures`` counter, and
    firing this alert too would make three notifications for one broken
    trigger. See ``_record_action_health``.

    Reset scoping (#154): a successful source check must NOT heal an
    action-failure streak (see the field comment on
    ``TriggerDefinition.consecutive_errors``); an action success resets
    unconditionally.
    """
    if error is None:
        if kind == "source" and trigger.last_error_kind == "action":
            return False
        if trigger.consecutive_errors > 0:
            trigger.consecutive_errors = 0
            trigger.health_status = "healthy"
            trigger.last_error = None
            trigger.last_error_kind = None
        return False

    trigger.consecutive_errors += 1
    trigger.last_error = error[:200]
    trigger.last_error_at = now
    trigger.last_error_kind = kind
    previous = trigger.health_status
    if trigger.consecutive_errors >= 5:
        trigger.health_status = "failing"
    elif trigger.consecutive_errors >= 2:
        trigger.health_status = "degraded"
    return previous != "failing" and trigger.health_status == "failing"


def _source_realert_due(trigger: TriggerDefinition, now: datetime) -> bool:
    """Is a repeat alert due for a source that is still failing? (#306)

    #264 gave the ACTION plane a two-stage alert-then-auto-pause policy, and
    deliberately left the SOURCE plane never-pausing: a source outage usually
    self-heals, so pausing would stop triggers a night's outage would have
    returned by morning. The cost was that a source which will NEVER recover
    (revoked token, deleted mailbox, retired endpoint) alerted exactly once on
    the way into "failing" and then went quiet forever, which is #264's own
    "still enabled after 850 errors" shape on the other plane.

    So the source plane re-alerts on a TIME cooldown instead of pausing.
    Deliberately time-based, not count-based: the backoff branch in
    ``check_triggers`` increments ``consecutive_errors`` for SKIPPED cycles
    too, so that counter reads "errors plus backed-off polls" and "every Nth
    poll" and "every Nth real check" differ by a factor of ten. A clock does
    not care, and this is only ever evaluated on a real, failed poll.

    Keyed on ``last_source_alert_at``, its OWN field. Sharing the action
    plane's ``last_policy_alert_at`` was tried and is wrong: the shared
    quantity is a three-hour window rather than an instant, so a source blip
    would suppress #264's action alert, and since that gate is
    ``count == alert_after`` exactly, suppressed means DROPPED, not delayed.
    """
    if trigger.health_status != "failing":
        return False
    from ..config import get_settings

    cooldown_minutes = getattr(
        get_settings(), "trigger_failure_alert_cooldown_minutes", 180
    )
    # The cooldown doubles as the REPEAT INTERVAL here, so zero disables the
    # reminder rather than enabling an infinitely fast one: "re-alert every 0
    # minutes" has no sane reading, and on the action plane zero already means
    # "disable this stage" (#264's threshold-of-zero rule). Setting it to zero
    # therefore restores the pre-#306 behavior of one alert per episode.
    if cooldown_minutes <= 0:
        return False
    if trigger.last_source_alert_at is None:
        # A trigger already sitting in "failing" from before this shipped has
        # no stamp, so it gets one catch-up alert and then falls into the
        # normal cadence.
        return True
    elapsed = (now - ensure_aware_utc(trigger.last_source_alert_at)).total_seconds()
    return elapsed >= cooldown_minutes * 60


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

    @staticmethod
    def _snapshot(store: TriggerStore) -> str:
        """Stable serialization of *store* for dirty detection.

        ``check_triggers`` mutates nested fields and trigger sources mutate
        ``trigger.state`` in place, so a top-level identity check is not
        enough; compare serialized content instead.
        """
        return json.dumps(store.model_dump(mode="json"), sort_keys=True, default=str)

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """Context manager for atomic trigger store updates.

        Only persists when the block actually changed the store, so a no-op
        poll (the common case in ``check_triggers``) does no disk write.

        Raises ``RuntimeError`` if a needed save fails, so a disk error is
        surfaced rather than silently dropping the mutation (matching the
        ``delete_triggers_for_thread`` contract). The save is still attempted
        in ``finally``, but its result is only raised on the success path, so
        an exception from inside the block propagates first and is never masked.
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
                raise RuntimeError(f"Failed to persist triggers for user {user_id}")

    # -- persistence ------------------------------------------------------

    def _path_for(self, user_id: str) -> Path:
        safe = safe_path_segment(user_id)
        return self.triggers_dir / f"{safe}.json"

    def _load(self, user_id: str) -> TriggerStore:
        path = self._path_for(user_id)
        # Read + parse + quarantine run under the per-user lock (an RLock, so
        # atomic_update re-enters freely): an unlocked reader could otherwise
        # parse a stale corrupt file, lose the race to a repairing _save, and
        # quarantine-rename the freshly written valid store aside (the
        # hook_manager._load invariant).
        with self._get_lock(user_id):
            if not path.exists():
                return TriggerStore(user_id=user_id)
            try:
                # A file fingerprint differing from the recorded manager
                # write is a raw on-disk edit: audit it once (a written
                # trigger is scheduled autonomous action). The .sig sidecar
                # is shared across manager instances and processes (trigger
                # authoring runs in the API while fire-state saves happen in
                # the ticker, a separate process in the Docker shape), so
                # neither side mis-audits the other's saves; re-recording it
                # after the audit acknowledges the edit so it is logged once,
                # not once per poll or per instance. An absent sidecar (no
                # manager write on record) means no audit: fail-safe.
                # This store is never served from a cache (``_load`` re-reads
                # the file every time), so the fingerprint guards only the
                # audit line. It still compares against the sidecar as the
                # baseline rather than a bare tuple: file-timestamp clocks are
                # coarse, so an edit reusing the manager write's mtime and byte
                # count would otherwise go unaudited.
                expected = read_store_fingerprint(path)
                if expected is not None:
                    current, edited_on_disk = compare_fingerprint(path, expected)
                    # `current is None` means the file vanished between the
                    # exists() check and this stat. compare_fingerprint calls
                    # that "changed" (it is, from the sidecar's point of view),
                    # but auditing it would log a raw EDIT for a deletion the
                    # read below is about to fail on anyway.
                    if current is not None and edited_on_disk:
                        record_store_fingerprint(path)
                        try:
                            from .activity_log import log_external_edit

                            log_external_edit(
                                "triggers",
                                "trigger store file edited on disk",
                                user_id=user_id,
                            )
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "Failed to record triggers external-edit audit",
                                exc_info=True,
                            )
                    else:
                        # A pre-hash sidecar compares on (mtime, size) alone,
                        # so it must be upgraded even when nothing changed, or
                        # this store stays on the degraded comparison forever.
                        upgrade_legacy_store_fingerprint(path, expected)
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
            except OSError as e:
                # Unreadable, not corrupt: see the same split in
                # hook_manager._load. Note this store's quarantine rationale
                # below cuts the other way, since a caller that saves on exit
                # would overwrite a file left in place with an empty store.
                # Reading nothing is still the safer answer: an unreadable
                # file usually becomes readable again, and quarantining it
                # guarantees the loss the overwrite only risks.
                logger.error("Could not read triggers for %s: %s", user_id, e)
                return TriggerStore(user_id=user_id)
            except Exception as e:  # noqa: BLE001 - never let a bad file break a turn
                # Quarantine, never leave in place: atomic_update saves on
                # exit, so a corrupt file left here would be overwritten with
                # an empty store by the next mutating poll.
                quarantine = quarantine_corrupt_file(path)
                logger.error(
                    "Failed to load triggers for %s: %s (%s)",
                    user_id,
                    e,
                    f"corrupt file preserved at {quarantine}"
                    if quarantine
                    else "quarantine rename failed; file left in place",
                )
                try:
                    from .activity_log import log_external_edit

                    log_external_edit(
                        "triggers",
                        "corrupt trigger store "
                        + (f"quarantined as quarantine/{quarantine.name}" if quarantine
                           else "could not be quarantined"),
                        user_id=user_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Failed to record triggers quarantine audit", exc_info=True
                    )
                return TriggerStore(user_id=user_id)

    def _save(self, store: TriggerStore) -> bool:
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
            # Refuse a fingerprint posing as a secret (#307). Update repairs
            # this silently by comparing against the stored value; create has
            # no stored value to compare against, so refusing is the only
            # honest answer. Raised rather than returned so the caller can say
            # WHY: both create surfaces answer a bare None with the same
            # generic "check source_type and config".
            from ..triggers.sources import (
                MaskedSecretRejected,
                masked_secret_collision,
            )

            collision = masked_secret_collision(
                source_type,
                source_config,
                [t.source_config for t in store.triggers],
            )
            if collision is not None:
                raise MaskedSecretRejected(collision)

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
        """Update fields on an existing trigger.

        ``source_config`` is a WHOLE-DICT replace, so every caller that wants
        to change one key has to resend the rest. Since #307 masks secrets on
        every read, the only value a caller holds for a secret key is its
        fingerprint, and resending that would write the mask over the live
        credential. The restore therefore lives HERE, at the one seam every
        writer passes through, rather than at each call site: the REST route
        and the agent's ``trigger_config(action="update")`` both reach this,
        and so will the next writer.

        The REST route restores again on its own, earlier, because it has to
        hand the REAL value to ``validate_config`` (a mask is a non-empty
        string and would validate happily). Restoring twice is harmless: a
        real secret never equals the mask of itself.
        """
        with self.atomic_update(user_id) as store:
            trigger = store.get_trigger(trigger_id)
            if trigger is None:
                return False

            if isinstance(kwargs.get("source_config"), dict):
                from ..triggers.sources import restore_unchanged_secrets

                kwargs["source_config"] = restore_unchanged_secrets(
                    trigger.source_type,
                    kwargs["source_config"],
                    trigger.source_config,
                )

            for key, value in kwargs.items():
                if hasattr(trigger, key) and key not in ("id", "created_at"):
                    setattr(trigger, key, value)

        return True

    def resume_trigger(self, user_id: str, trigger_id: str) -> Optional[dict]:
        """Clear a trigger's auto-pause AND its whole failure history (#264).

        The repair verb. Before this existed there was no surface anywhere
        that could reset trigger health, so a trigger whose cause you had
        already fixed stayed "failing" and kept skipping 9 of 10 polls (the
        backoff in ``check_triggers``) until an action happened to succeed,
        and hand-editing the store file was the only way out.

        Resume is EXPLICIT, never inferred from another field being written:
        #154 infers its TODO resume from "schedule set while paused" and
        backlog #249 already filed that inference as a defect.

        Deliberately does NOT touch ``enabled``: pause and the user's own
        toggle are orthogonal, so resuming a trigger the user had also
        switched off must not switch it back on.

        Returns a summary of what was cleared (for honest caller copy), or
        None when no such trigger exists.
        """
        with self.atomic_update(user_id) as store:
            trigger = store.get_trigger(trigger_id)
            if trigger is None:
                return None
            summary = {
                "was_paused": trigger.auto_paused_at is not None,
                "action_failures": trigger.action_failures,
                "consecutive_errors": trigger.consecutive_errors,
                "previous_health": trigger.health_status,
                "enabled": trigger.enabled,
            }
            trigger.auto_paused_at = None
            trigger.action_failures = 0
            trigger.last_source_alert_at = None
            trigger.last_policy_alert_at = None
            trigger.consecutive_errors = 0
            trigger.health_status = "healthy"
            trigger.last_error = None
            trigger.last_error_at = None
            trigger.last_error_kind = None
        logger.info(
            "Trigger %s resumed (was_paused=%s, cleared %d action failures)",
            trigger_id,
            summary["was_paused"],
            summary["action_failures"],
        )
        return summary

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
        """Return True if ALL conditions pass (AND logic).

        Delegates to the shared ``core/conditions.evaluate_conditions`` so the
        trigger and lifecycle-hooks stacks match conditions identically.
        """
        return evaluate_conditions(event, conditions)

    # -- execution log ----------------------------------------------------

    def _executions_path(self, user_id: str) -> Path:
        safe = safe_path_segment(user_id)
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
            write_text_atomic(path, json.dumps(entries, default=str))
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
                write_text_atomic(path, json.dumps(kept, default=str))
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

        logger.debug(
            f"[TRIGGER CHECK] user={user_id}, registered_sources={list(AVAILABLE_SOURCES.keys())}"
        )

        results: List[Tuple[TriggerDefinition, List[dict]]] = []
        # (trigger, is_repeat). A repeat is the #306 re-alert for a source that
        # is still failing a cooldown later, and it reads differently.
        newly_failing: List[Tuple[TriggerDefinition, bool]] = []

        with self.atomic_update(user_id) as store:
            now = utc_now()

            def _record_source_failure(
                trigger: TriggerDefinition, error: str
            ) -> None:
                """Apply one failed source poll and decide whether to alert."""
                became_failing = _apply_health_outcome(
                    trigger, error, now, kind="source"
                )
                if became_failing or _source_realert_due(trigger, now):
                    # "Repeat" means we have alerted about THIS source before.
                    # Judged on the stamp, not on `became_failing` alone: the
                    # action plane can drive health to "failing" on its own,
                    # and the first thing the owner hears about a source must
                    # not be the word "STILL".
                    is_repeat = (
                        not became_failing
                        and trigger.last_source_alert_at is not None
                    )
                    # Stamp on the FIRST alert too, not only on repeats:
                    # without it the transition alert would be followed by a
                    # re-alert on the very next real poll.
                    trigger.last_source_alert_at = now
                    newly_failing.append((trigger, is_repeat))

            for trigger in store.triggers:
                if not trigger.enabled:
                    continue

                # Auto-paused by the #264 action-failure policy: the whole
                # point is that it stops ACTING and stops CONSUMING, so this
                # short-circuits the source check too (an Outlook check tags
                # mail read before its action ever runs, backlog #265).
                # Distinct from `enabled` so the user's own toggle keeps its
                # meaning; cleared only by resume_trigger().
                if trigger.auto_paused_at is not None:
                    continue

                # Cooldown check
                if trigger.cooldown_seconds and trigger.last_fired:
                    elapsed = (
                        now - ensure_aware_utc(trigger.last_fired)
                    ).total_seconds()
                    if elapsed < trigger.cooldown_seconds:
                        continue

                # Backoff for failing triggers: skip until the counter hits
                # a multiple of 10. Counting the SKIPPED cycle is what makes
                # the backoff lapse (pre-existing bug, caught in the #154
                # review: a check failure lands the counter on 5-9 and a
                # frozen counter then skips every future cycle forever, so
                # "failing" was permanent). While failing, the counter
                # therefore reads "errors plus backed-off polls", which is
                # fine: thresholds are crossings from below, and any scoped
                # success still resets it.
                if trigger.health_status == "failing" and trigger.consecutive_errors % 10 != 0:
                    trigger.consecutive_errors += 1
                    continue

                source = get_source(trigger.source_type)
                if source is None:
                    # Feed it through the SAME health path as a failed poll
                    # (#306). This used to log and `continue`, so a trigger
                    # whose source plugin had been removed from the build sat
                    # reading "healthy", fired nothing, and told nobody: a
                    # third shape of quietly-dead source, and the one where
                    # the trigger cannot possibly recover on its own.
                    _record_source_failure(
                        trigger,
                        f"Source type '{trigger.source_type}' is not registered",
                    )
                    logger.warning(
                        f"Source '{trigger.source_type}' not registered, "
                        f"skipping trigger {trigger.id}"
                    )
                    continue

                try:
                    if _source_accepts_thread_id(source):
                        events = source.check(
                            trigger.source_config,
                            trigger.state,
                            user_id,
                            thread_id=trigger.thread_id,
                        )
                    else:
                        # Out-of-tree source plugin written before ``check``
                        # grew ``thread_id`` (sources are hot-reloadable
                        # files): poll it the old way. It cannot honour
                        # per-thread credential bindings, which is the
                        # documented cost of the legacy signature.
                        events = source.check(
                            trigger.source_config, trigger.state, user_id
                        )
                    _apply_health_outcome(trigger, None, now, kind="source")
                except Exception as e:
                    _record_source_failure(trigger, str(e))
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

        # Alerts go out AFTER the store lock releases (the sender does
        # network/store work that must not run inside atomic_update).
        for trigger, is_repeat in newly_failing:
            self._send_failing_alert(
                user_id,
                trigger.id,
                trigger.name,
                trigger.thread_id or f"trigger-{trigger.id}",
                trigger.last_error or "",
                repeat=is_repeat,
            )

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
            known_action = True
            if action.type == "agent_prompt":
                self._fire_agent_prompt(action.config, template_vars, agent, user_id, trigger)
            elif action.type == "notify":
                self._fire_notify(action.config, template_vars, user_id, trigger)
            elif action.type == "create_todo":
                self._fire_create_todo(action.config, template_vars, user_id)
            elif action.type == "run_workflow":
                self._fire_run_workflow(action.config, event, agent, user_id, trigger)
            else:
                logger.error(f"Unknown action type: {action.type}")
                known_action = False
            execution.status = "success"
            if known_action:
                # An unknown action type ran nothing: recording it as a
                # healthy action outcome would report a misconfigured
                # trigger as working.
                self._record_action_health(user_id, trigger.id, None)
        except Exception as e:
            execution.status = "error"
            execution.error_message = str(e)[:200]
            logger.error(
                f"[TRIGGER] Action failed for trigger '{trigger.name}' ({trigger.id}): {e}",
                exc_info=True,
            )
            self._record_action_health(user_id, trigger.id, str(e))
            self._publish_trigger_error(
                trigger,
                user_id,
                str(e),
                fanout=bool(getattr(e, "fanout_observed", False)),
            )
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
            for index, event in enumerate(events):
                self.fire_action(trigger, event, agent, user_id)
                # A per-event batch can cross the #264 pause threshold
                # part-way through, and the pause is a verdict about THIS
                # trigger's action: firing the rest of the batch anyway
                # would keep acting after the policy said stop, overshoot
                # `action_failures` past the count that caused the pause,
                # and let a late success in the same batch zero the streak
                # (reporting "auto-paused after 0 failed actions"). Re-read
                # rather than trust the in-memory copy: the pause is
                # written to the store by _record_action_health, and in
                # Docker another process may have written it.
                if index + 1 < len(events):
                    live = self.get_trigger(user_id, trigger.id)
                    if live is None or live.auto_paused_at is not None:
                        logger.info(
                            "[TRIGGER] %s auto-paused mid-batch; dropping %d "
                            "remaining event(s)",
                            trigger.id,
                            len(events) - index - 1,
                        )
                        return
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

            response_parts, _thinking_parts, iteration_limit_hit, fanout_observed = self._stream_live(
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
            # Partial (iteration-limit) still ran the turn: healthy.
            self._record_action_health(user_id, trigger.id, None)

            self._publish_trigger_completion(
                trigger=trigger,
                thread_id=thread_id,
                user_id=user_id,
                response=response,
                event_count=len(events),
                partial=iteration_limit_hit,
                fanout=fanout_observed,
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
            self._record_action_health(user_id, trigger.id, str(e))
            self._publish_trigger_error(
                trigger,
                user_id,
                str(e),
                fanout=bool(getattr(e, "fanout_observed", False)),
            )
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

        response_parts, _thinking_parts, iteration_limit_hit, fanout_observed = self._stream_live(
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
            fanout=fanout_observed,
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
    ) -> Tuple[List[str], List[str], bool, bool]:
        """Stream through the agent, publishing each event live.

        If *task_started_data* is provided, the ``task_started`` event is
        published when the first chunk arrives (i.e. after the thread lock
        is acquired), not before.  This prevents the frontend from entering
        streaming mode while the user's chat is still active.

        Returns (response_parts, thinking_parts, iteration_limit_hit,
        fanout_observed). ``fanout_observed`` means the prompt was absorbed
        into a busy holder turn and everything streamed here is that turn's
        mirrored output (see the stream_bridge fanout marker).
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
                    # Marked first chunk = this fire became a queuer
                    # mirroring the holder's turn; stamp the lifecycle so
                    # consumers can skip the mirror task.
                    data={
                        **task_started_data,
                        **({"fanout": True} if chunk.get("fanout") else {}),
                    },
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

        return (
            response_parts,
            result.thinking_parts,
            result.iteration_limit_hit,
            result.fanout_observed,
        )

    def _publish_trigger_completion(
        self,
        trigger: TriggerDefinition,
        thread_id: str,
        user_id: str,
        response: str,
        event_count: int = 1,
        partial: bool = False,
        fanout: bool = False,
    ) -> None:
        """Publish task_completed and log to activity feed."""
        from .activity_log import ActivityType, log_activity
        from .event_bus import publish_autonomous_event

        task_id = f"trigger-{trigger.id}"

        completed_data: dict[str, Any] = {
            "content": response,
            "trigger_id": trigger.id,
            "trigger_name": trigger.name,
        }
        if partial:
            completed_data["partial"] = True
        if fanout:
            # Mirror-task marker: the content was fanned in from another
            # task's holder turn (stream_bridge fanout marker); consumers
            # must not deliver it a second time.
            completed_data["fanout"] = True

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

    def _record_action_health(
        self, user_id: str, trigger_id: str, error: Optional[str]
    ) -> None:
        """Feed ACTION outcomes into the shared trigger health fields.

        Pre-#154 only ``check_triggers``'s source-check except branch touched
        ``consecutive_errors``/``health_status``, so a trigger whose ACTION
        (agent turn, notify, workflow) errored on every fire stayed
        "healthy" forever. The rules live in ``_apply_health_outcome`` (one
        copy shared with the source-check site); the kind-scoped reset there
        keeps every poll cycle's successful source check from zeroing an
        action-failure streak. The existing failing-trigger backoff in
        ``check_triggers`` governs action-failure episodes too.

        This is also where the #264 action-failure POLICY runs: alert once at
        ``trigger_failure_alert_after``, auto-pause at
        ``trigger_failure_pause_after``. The policy drives off
        ``action_failures``, never ``consecutive_errors`` (see that field's
        comment: the shared counter also counts backed-off polls, so a
        threshold on it means nothing).

        The action plane deliberately does NOT send the
        ``_send_failing_alert`` transition alert any more: with the policy
        live it would be the second of three notifications for one broken
        trigger (policy alert at 2, transition at 5, pause at 5). The policy
        alert fires earlier and says more. The SOURCE plane keeps it, since
        no pause policy governs that plane.

        Unpersisted state must not drive policy: ``atomic_update`` raises
        when the save fails, so a failed write leaves via the except below
        with no alert sent and no pause claimed. Fault-isolated: health
        bookkeeping must never break a fire.
        """
        try:
            verdict: Optional[str] = None
            count = 0
            trigger_name = trigger_id
            thread_id = f"trigger-{trigger_id}"
            last_error = ""
            with self.atomic_update(user_id) as store:
                trigger = next(
                    (t for t in store.triggers if t.id == trigger_id), None
                )
                if trigger is None:
                    return
                now = utc_now()
                _apply_health_outcome(trigger, error, now, kind="action")

                if error is None:
                    # A successful fire ends the episode. The pause marker is
                    # not cleared here: a paused trigger cannot fire, so the
                    # only way to reach this branch while paused is a fire
                    # that was already in flight when the pause landed, and
                    # that must not silently un-pause it.
                    trigger.action_failures = 0
                else:
                    trigger.action_failures += 1
                    count = trigger.action_failures
                    verdict = self._action_failure_verdict(trigger, count)
                    if verdict == "pause":
                        trigger.auto_paused_at = now
                    if verdict is not None:
                        trigger.last_policy_alert_at = now

                trigger_name = trigger.name
                thread_id = trigger.thread_id or f"trigger-{trigger_id}"
                last_error = trigger.last_error or ""

            # Alerts go out AFTER the store lock releases: the sender does
            # network and store work of its own (same discipline as
            # check_triggers' newly_failing loop).
            if verdict == "pause":
                logger.warning(
                    "Trigger %s auto-paused after %d consecutive action "
                    "failures",
                    trigger_id,
                    count,
                )
                self._send_action_policy_alert(
                    user_id,
                    trigger_id,
                    trigger_name,
                    thread_id,
                    last_error,
                    count,
                    paused=True,
                )
            elif verdict == "alert":
                self._send_action_policy_alert(
                    user_id,
                    trigger_id,
                    trigger_name,
                    thread_id,
                    last_error,
                    count,
                    paused=False,
                )
        except Exception:
            logger.warning(
                "Failed to record action health for trigger %s",
                trigger_id,
                exc_info=True,
            )

    @staticmethod
    def _action_failure_verdict(
        trigger: TriggerDefinition, count: int
    ) -> Optional[str]:
        """Decide what this action failure earns: "pause", "alert" or nothing.

        Pause wins over alert when both thresholds are crossed at once (the
        #154 ordering). Either threshold at 0 disables that stage.
        """
        if trigger.auto_paused_at is not None:
            # Already paused: a fire that was in flight when the pause landed
            # must not stack a second pause or a second alert.
            return None
        from ..config import get_settings

        settings = get_settings()
        alert_after = max(0, int(getattr(settings, "trigger_failure_alert_after", 2)))
        pause_after = max(0, int(getattr(settings, "trigger_failure_pause_after", 5)))
        if pause_after and count >= pause_after:
            # Never suppressed: a pause is terminal and the owner has to
            # know the trigger stopped.
            return "pause"
        if alert_after and count == alert_after:
            cooldown = max(
                0,
                int(getattr(settings, "trigger_failure_alert_cooldown_minutes", 180)),
            )
            last = trigger.last_policy_alert_at
            if cooldown and last is not None:
                age = (utc_now() - ensure_aware_utc(last)).total_seconds()
                if age < cooldown * 60:
                    return None
            return "alert"
        return None

    def _send_action_policy_alert(
        self,
        user_id: str,
        trigger_id: str,
        trigger_name: str,
        thread_id: str,
        last_error: str,
        count: int,
        *,
        paused: bool,
    ) -> None:
        """Owner alert for the #264 action-failure policy. Never raises."""
        try:
            from ..config import get_settings
            from .notification_dispatch import send_owner_alert

            if paused:
                message = (
                    f"[TRIGGER PAUSED] Trigger \"{trigger_name}\" "
                    f"({trigger_id}) was auto-paused after {count} "
                    f"consecutive failed actions, so it has stopped running "
                    f"and stopped consuming events. Last error: "
                    f"{last_error}. Fix the cause, then resume it with "
                    f"/triggers resume {trigger_id}."
                )
            else:
                settings = get_settings()
                pause_after = max(
                    0, int(getattr(settings, "trigger_failure_pause_after", 5))
                )
                pause_note = (
                    f" It auto-pauses after {pause_after} consecutive "
                    f"failures." if pause_after else ""
                )
                message = (
                    f"[TRIGGER ALERT] Trigger \"{trigger_name}\" "
                    f"({trigger_id}) has failed its action {count} times in "
                    f"a row. Last error: {last_error}.{pause_note} Manage it "
                    f"with /triggers."
                )

            send_owner_alert(
                message,
                get_settings(),
                user_id=user_id,
                thread_id=thread_id,
                task_id=f"trigger-{trigger_id}",
            )
        except Exception:
            logger.warning(
                "Action-policy alert failed for trigger %s",
                trigger_id,
                exc_info=True,
            )

    def _send_failing_alert(
        self,
        user_id: str,
        trigger_id: str,
        trigger_name: str,
        thread_id: str,
        last_error: str,
        repeat: bool = False,
    ) -> None:
        """One owner alert for a failing SOURCE, first time or repeat (#306).

        ``repeat`` distinguishes the crossing into "failing" from the
        re-alerts that follow one cooldown apart while it stays there. They
        need different copy: the first is news, the rest are a standing
        reminder about something the owner has already been told about once,
        and reusing the first wording would read as a fresh failure each time.

        The repeat wording deliberately does NOT suggest ``resume``: resume
        clears health, and on a source that is genuinely dead the trigger
        simply fails its way back to "failing". Fix or disable are the two
        answers that end it.

        Never raises (a broken alert plane must not break the poll loop or
        a fire).
        """
        try:
            from ..config import get_settings
            from .notification_dispatch import send_owner_alert

            if repeat:
                message = (
                    f"[TRIGGER STILL FAILING] Trigger \"{trigger_name}\" "
                    f"({trigger_id}) is still failing its source checks and "
                    f"is polling at a reduced rate. Last error: {last_error}. "
                    f"This reminder repeats until you fix the cause or "
                    f"disable it with /triggers."
                )
            else:
                message = (
                    f"[TRIGGER FAILING] Trigger \"{trigger_name}\" "
                    f"({trigger_id}) keeps erroring and is now marked "
                    f"failing (checks back off). Last error: {last_error}. "
                    f"Manage it with /triggers."
                )

            send_owner_alert(
                message,
                get_settings(),
                user_id=user_id,
                thread_id=thread_id,
                task_id=f"trigger-{trigger_id}",
            )
        except Exception:
            logger.warning(
                "Failing-transition alert failed for trigger %s",
                trigger_id,
                exc_info=True,
            )

    def _publish_trigger_error(
        self,
        trigger: TriggerDefinition,
        user_id: str,
        error_msg: str,
        *,
        fanout: bool = False,
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
                # A fanned-in holder error is still a mirror; consumers
                # drop marked bookends.
                **({"fanout": True} if fanout else {}),
            },
        )

    def _fire_notify(
        self,
        config: dict,
        template_vars: dict,
        user_id: str,
        trigger: TriggerDefinition,
    ) -> None:
        """Send a notification (no LLM call).

        Accepts either ``profile`` (current notification model) or the legacy
        ``platform`` key for backward compatibility with trigger configs
        created before the destinations/profiles refactor.
        """
        from ..tools.notify import notify

        template = config.get("message_template", "Trigger {trigger_name} fired.")
        message = _safe_format(template, template_vars)
        profile = config.get("profile")
        platform = config.get("platform")
        thread_id = trigger.thread_id or f"trigger-{trigger.id}"

        invoke_args: dict = {"message": message}
        if profile:
            invoke_args["profile"] = profile
        elif platform:
            invoke_args["platform"] = platform

        logger.info(f"[TRIGGER] Sending notification: {message[:100]}...")
        result = notify.invoke(
            invoke_args,
            config={"configurable": {"user_id": user_id, "thread_id": thread_id}},
        )
        logger.info(f"[TRIGGER] Notify result: {result}")

    def _fire_create_todo(self, config: dict, template_vars: dict, user_id: str) -> None:
        """Create a TODO item (no LLM call)."""
        # Reuse the canonical lazy TodoManager singleton (over
        # ``get_settings().data_dir``) instead of constructing a fresh manager
        # (with its mkdir + log) on every trigger fire.
        from ..tools.todo import _get_todo_manager

        template = config.get("task_template", "Triggered: {trigger_name}")
        task = _safe_format(template, template_vars)

        todo_manager = _get_todo_manager()

        # Parse an optional ``scheduled_for`` up front so it can be written in
        # the same ``add_item`` call. Keep the lenient parser: it returns None
        # on bad input (an invalid value logs a warning and creates the TODO
        # unscheduled) rather than raising, which in this fire path would abort
        # the whole trigger.
        scheduled_for = None
        scheduled_for_str = config.get("scheduled_for")
        if scheduled_for_str:
            from ..core.time_utils import parse_scheduled_time

            scheduled_for = parse_scheduled_time(scheduled_for_str)
            if scheduled_for is None:
                logger.warning(
                    "[TRIGGER] Ignoring invalid scheduled_for %r on create_todo",
                    scheduled_for_str,
                )

        created_item_id = None
        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.add_item(
                task=task,
                scheduled_for=scheduled_for,
                created_by="trigger",
                thread_id=f"trigger_{template_vars.get('trigger_id', 'auto')}",
            )
            if item:
                created_item_id = item.id
                logger.info(f"[TRIGGER] Created TODO {item.id}: {task[:80]}")
            else:
                logger.warning("[TRIGGER] Failed to create TODO (at limit?)")

        # Register the schedule in the ticker's index AFTER the JSON is
        # persisted (``sync_schedule_to_db`` reads the item back from disk, and
        # ``atomic_update`` saves on block exit). Without this, the ticker,
        # which polls only the index, would never fire the scheduled TODO until
        # a restart rebuilt the index from disk. Mirrors the REST/slash create
        # paths (api/routers/todos.py, command_service.py).
        if created_item_id and scheduled_for is not None:
            from ..api.routers.todos import _get_todo_schedule_db
            from ..config import get_settings

            schedule_db = _get_todo_schedule_db(get_settings())
            todo_manager.sync_schedule_to_db(user_id, created_item_id, schedule_db)

    def _fire_run_workflow(
        self,
        config: dict,
        event: dict,
        agent: "NymeriaAgent | TurnExecutor",
        user_id: str,
        trigger: TriggerDefinition,
    ) -> None:
        """Run a published workflow tool (no LLM call).

        The RAW event dict is passed as the workflow's ``event`` parameter
        when its signature declares one (no per-field mapping config: the
        authored body extracts what it needs). Executes via the turn
        executor's ``run_workflow`` seam so the run always happens in the API
        process (slim: directly; Docker worker: relayed). Never delivers
        output anywhere itself; delivery is the workflow's own explicit job
        (nym.thread / nym.notify), so a headless fire cannot leak output to a
        guessed destination.
        """
        import asyncio

        from .turn_executor import TurnExecutor, wrap_for_stream

        workflow_id = str(config.get("workflow_id") or "").strip()
        if not workflow_id:
            raise ValueError("run_workflow action config needs workflow_id")

        from .workflows.tool_runtime import workflow_declares_event

        params = dict(config.get("params") or {})
        if workflow_declares_event(workflow_id):
            params.setdefault("event", dict(event))
        thread_id = trigger.thread_id or f"trigger-{trigger.id}"
        executor: TurnExecutor = wrap_for_stream(agent)

        logger.info(
            f"[TRIGGER] Running workflow {workflow_id} for trigger "
            f"'{trigger.name}' ({trigger.id})"
        )
        envelope = asyncio.run(
            executor.run_workflow(
                workflow_id, params, user_id=user_id, thread_id=thread_id
            )
        )
        status = str(envelope.get("status") or "")
        if status == "needs_approval":
            # The run suspended awaiting the owner's decision: a successful
            # fire (the approve verb already announced it).
            logger.info(f"[TRIGGER] Workflow {workflow_id} suspended for approval")
            return
        if not envelope.get("ok"):
            error = envelope.get("error") or {}
            raise RuntimeError(
                f"workflow {workflow_id} {status or 'error'} "
                f"({error.get('kind', 'unknown')}): {error.get('message', '')}"
            )
        logger.info(f"[TRIGGER] Workflow {workflow_id} finished ok")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Template substitution now lives in ``core/text_format.py`` so the trigger and
# lifecycle-hooks stacks share one implementation. Re-exported here (and as the
# historical private name ``_safe_format``) so existing importers keep working.
from .text_format import _DefaultDict, safe_format as _safe_format  # noqa: E402,F401
