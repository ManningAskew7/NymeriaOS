"""TODO management for Nymeria autonomous operation."""

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .keyed_locks import KeyedRLockMap
from .storage_paths import (
    StoreUnavailableError,
    quarantine_copy,
    safe_path_segment,
    validation_summary,
    write_text_atomic,
)
from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .todo_schedule_db import TodoScheduleDB

# Thread-safe locks for todo operations (keyed by user_id)
_todo_locks = KeyedRLockMap()

# Notes-banner contracts. Notes are prompt input on every run and carry the
# user's own instructions, so no automation may overwrite them wholesale;
# banners are PREPENDED and later stripped by prefix.
#
# The auto-pause banner prefix is a THREE-SITE contract: the ticker's #154
# pause and delivery_accounting's #247 pause both PREPEND a note starting
# with this to the TODO's notes, and update_item's resume-clear strips a
# note by matching it. Rewording any writer without this constant would
# silently stop the strip, leaving a stale pause banner in notes.
PAUSE_NOTE_PREFIX = "[auto-paused after"

# TRANSIENT banners (the ticker's double-iteration-limit backoff and the
# non-recurring retry-give-up) have no explicit resume event, so they dedup
# on write instead: prepend_note_banner strips any LEADING transient
# banner(s) first, keeping at most one transient banner in front of the
# notes. The pause banner is deliberately not transient (its strip contract
# is the explicit resume above) and outranks them: a pause prepends in front
# of a transient banner, and the resume-strip exposes the transient again.
BACKOFF_NOTE_PREFIX = "[iteration-limit backoff:"
GIVEUP_NOTE_PREFIX = "[run failed after"
TRANSIENT_NOTE_PREFIXES: tuple = (BACKOFF_NOTE_PREFIX, GIVEUP_NOTE_PREFIX)


def format_note_banner(prefix: str, body: str) -> str:
    """Build a ``"[prefix body]"`` notes banner.

    ``]`` in the body (e.g. a ``KeyError['x']`` repr in an error message) is
    sanitized to ``)`` so the FIRST ``]`` always terminates the banner:
    both the resume-clear strip in ``update_item`` and
    ``prepend_note_banner``'s replace-scan find a banner's end that way, and
    an embedded bracket used to leave un-strippable residue in the notes.
    """
    return f"{prefix} {body.replace(']', ')')}]"


def strip_leading_note_banner(text: str, prefixes) -> str:
    """Remove one leading banner matching any of ``prefixes``, if present."""
    for prefix in prefixes:
        if text.startswith(prefix):
            close = text.find("]")
            if close != -1:
                return text[close + 1 :].strip()
    return text


def prepend_note_banner(existing: Optional[str], banner: str) -> str:
    """Prepend a TRANSIENT banner, replacing any leading transient banners.

    ``banner`` must come from ``format_note_banner`` with a
    ``TRANSIENT_NOTE_PREFIXES`` prefix. Strips consecutive leading transient
    banners of ANY kind first (a give-up replaces a stale backoff and vice
    versa), so repeated automation events never stack banners in front of
    the user's notes.
    """
    text = (existing or "").strip()
    while True:
        stripped = strip_leading_note_banner(text, TRANSIENT_NOTE_PREFIXES)
        if stripped == text:
            break
        text = stripped
    return f"{banner} {text}".strip()[:1000]


class TodoStatus(str, Enum):
    """Status of a TODO item."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class TodoItem(BaseModel):
    """A single TODO item."""

    id: str = Field(..., description="8-character unique identifier")
    task: str = Field(..., max_length=500, description="Task description")
    status: TodoStatus = Field(default=TodoStatus.PENDING)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    notes: Optional[str] = Field(default=None, max_length=1000)

    # Scheduling fields - when set, Nymeria wakes up to work on this TODO
    scheduled_for: Optional[datetime] = Field(default=None, description="When to wake up and work on this TODO")
    thread_id: str = Field(..., description="Thread this TODO belongs to")
    last_execution: Optional[datetime] = Field(default=None, description="Last scheduled execution time")

    @model_validator(mode='before')
    @classmethod
    def _migrate_legacy_fields(cls, data: dict) -> dict:
        """Migrate old TODO data: backfill thread_id, strip removed fields, convert blocked→pending."""
        if isinstance(data, dict):
            # Backfill thread_id='legacy' for old TODOs that lack one
            if not data.get('thread_id'):
                data['thread_id'] = 'legacy'
            # Migrate blocked → pending
            if data.get('status') == 'blocked':
                data['status'] = 'pending'
                if data.get('blocked_reason'):
                    existing_notes = data.get('notes') or ''
                    migrated = f"[was blocked: {data['blocked_reason']}] {existing_notes}".strip()
                    data['notes'] = migrated[:1000]  # Respect max_length
            # Strip removed fields. Pydantic drops unknown keys at validation
            # (extra='ignore'), so for purely-retired keys these pops are
            # documentation of deliberate retirement, not load-bearing; the
            # load-bearing half is the blocked_reason READ above, which must
            # happen before its key is stripped. `goal_id` joined the list
            # when the /goal subsystem was deleted (it was the supervisor
            # lock's marker).
            for field in ('priority', 'deadline', 'blocked_reason', 'permanent', 'goal_id'):
                data.pop(field, None)
        return data

    # User management & recurrence fields
    created_by: str = Field(default="agent", description="Who created this TODO: 'agent' or 'user'")
    recurrence: Optional[str] = Field(default=None, description="Recurrence interval as a duration string (e.g. '5m', '2h', '1d', '1w', '1mo'). Calendar months (Nmo) use calendar arithmetic; everything else is a fixed duration. Legacy preset names (hourly/daily/weekly/monthly, 5min/10min/15min/30min) are still accepted on input and resolved by todo_constants.")
    recurrence_anchor: Optional[datetime] = Field(default=None, description="Stable origin fire time for calendar-month (Nmo) recurrence. Next slots are derived as origin + N months so a month-end day (29-31) clamps to short months without drifting downward. Adopted lazily from the first fired slot and reset when the recurrence changes; unused for fixed-duration intervals.")

    # Scheduled-workflow integration: when workflow_id is set, the ticker
    # runs that published workflow tool headlessly (no agent turn) instead
    # of prompting the agent with the task text. Recurrence, retries, and
    # the missed-work policy apply unchanged.
    workflow_id: Optional[str] = Field(
        default=None,
        description="Published workflow tool to run instead of an agent turn",
    )
    workflow_params: Optional[dict] = Field(
        default=None, description="Parameters bound to the scheduled workflow run"
    )

    # Recurring-failure policy (#154). One increment per failed OCCURRENCE
    # (the ticker's retry-cap branch, never per intra-occurrence retry); any
    # successful occurrence resets all three. `schedule_paused_at` is the
    # auto-pause marker: recurrence stays set while `scheduled_for` is None,
    # which is deliberate and distinguishable from the pre-2026-07-09 bug
    # shape that silently killed schedules. An explicit reschedule of a
    # PAUSED todo clears the whole failure state (update_item); the ticker's
    # own re-arm also writes scheduled_for, which is why the clear is keyed
    # on the pause marker rather than on any reschedule.
    consecutive_failures: int = Field(
        default=0,
        description="Consecutive failed occurrences of this recurring TODO",
    )
    last_failure: Optional[str] = Field(
        default=None, max_length=300, description="Most recent failure message"
    )
    last_failure_at: Optional[datetime] = Field(
        default=None, description="When the most recent occurrence failed"
    )
    schedule_paused_at: Optional[datetime] = Field(
        default=None,
        description="Set when the failure policy auto-paused this schedule",
    )

    # Delivery-failure accounting (#247), parallel to the #154 trio above and
    # deliberately NOT sharing `consecutive_failures`: the ticker resets that
    # streak when the turn finishes, which is BEFORE a chat bot finishes (or
    # fails) delivering the output, so a shared field would oscillate
    # 0 -> 1 -> 0 across occurrences and never cross a threshold. These
    # fields are driven only by bot delivery reports
    # (POST /todos/{id}/delivery-report -> core/delivery_accounting.py):
    # a failed report increments, a delivered/partial report resets, and the
    # same scheduler_failure_* thresholds drive alert and pause. The
    # resume-clear in update_item covers both trios.
    delivery_failures: int = Field(
        default=0,
        description=(
            "Consecutive occurrences whose output a chat bot could not "
            "deliver (bot delivery reports)"
        ),
    )
    last_delivery_failure: Optional[str] = Field(
        default=None,
        max_length=300,
        description="Most recent delivery failure message",
    )
    last_delivery_failure_at: Optional[datetime] = Field(
        default=None, description="When delivery of the output last failed"
    )

    # Skip-alert cooldown stamp (#262). Set when a late fire let scheduled
    # occurrences pass unrun AND the owner was alerted; a further skip
    # alerts again only once scheduler_skip_alert_cooldown_minutes have
    # passed (every skip still writes an activity row). Persisted, not
    # ticker memory, so a restart does not re-alert; a pause resume clears
    # it (update_item).
    skip_alerted_at: Optional[datetime] = Field(
        default=None,
        description=(
            "When the owner was last alerted that this recurring TODO "
            "skipped scheduled occurrences (the skip-alert cooldown stamp)"
        ),
    )

    @field_validator(
        "created_at",
        "updated_at",
        "scheduled_for",
        "last_execution",
        "recurrence_anchor",
        "last_failure_at",
        "schedule_paused_at",
        "last_delivery_failure_at",
        "skip_alerted_at",
    )
    @classmethod
    def _datetimes_as_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        return ensure_aware_utc(value)

    def is_active(self) -> bool:
        """Check if this TODO is active (not done)."""
        return self.status != TodoStatus.DONE

    def is_scheduled(self) -> bool:
        """Check if this TODO has a pending scheduled execution."""
        return self.scheduled_for is not None and self.is_active()

    def is_stale(self, staleness_hours: int) -> bool:
        """Check if this TODO hasn't been updated in staleness_hours."""
        if not self.is_active():
            return False
        threshold = utc_now() - timedelta(hours=staleness_hours)
        return ensure_aware_utc(self.updated_at) < threshold

    def hours_since_update(self) -> float:
        """Get hours since last update."""
        delta = utc_now() - ensure_aware_utc(self.updated_at)
        return delta.total_seconds() / 3600


class TodoList(BaseModel):
    """User's TODO list containing all items."""

    user_id: str = Field(default="default")
    items: List[TodoItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _datetimes_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)

    # Limits
    MAX_TODOS: int = 50

    def get_item(self, todo_id: str) -> Optional[TodoItem]:
        """Get a TODO item by ID."""
        for item in self.items:
            if item.id == todo_id:
                return item
        return None

    def add_item(
        self,
        task: str,
        scheduled_for: Optional[datetime] = None,
        thread_id: str = "legacy",
        created_by: str = "agent",
        recurrence: Optional[str] = None,
        notes: Optional[str] = None,
        workflow_id: Optional[str] = None,
        workflow_params: Optional[dict] = None,
    ) -> Optional[TodoItem]:
        """
        Add a new TODO item.

        Args:
            task: Task description
            scheduled_for: When Nymeria should wake up to work on this
            thread_id: Thread context for scheduled execution
            created_by: Who created this TODO ('agent' or 'user')
            recurrence: Recurrence interval as a canonical duration string
                (e.g. '5m', '2h', '1d'). Callers should pass values already
                validated by todo_constants.validate_recurrence.
            notes: Additional notes
            workflow_id: Published workflow tool the ticker runs headlessly
                instead of an agent turn. Callers should pass values already
                validated by workflows.tool_runtime.workflow_binding_error.
            workflow_params: Parameters bound to the scheduled workflow run

        Returns:
            The created TodoItem, or None if at limit.
        """
        # Check limit
        active_count = len([i for i in self.items if i.is_active()])
        if active_count >= self.MAX_TODOS:
            return None

        # Truncate task if too long
        task = task[:500]

        # Generate short ID
        todo_id = str(uuid.uuid4())[:8]

        item = TodoItem(
            id=todo_id,
            task=task,
            scheduled_for=scheduled_for,
            thread_id=thread_id,
            created_by=created_by,
            recurrence=recurrence,
            notes=notes[:1000] if notes else None,
            workflow_id=workflow_id,
            workflow_params=workflow_params,
        )
        self.items.append(item)
        self.updated_at = utc_now()
        return item

    def update_item(
        self,
        todo_id: str,
        status: Optional[TodoStatus] = None,
        notes: Optional[str] = None,
        task: Optional[str] = None,
        scheduled_for: Optional[datetime] = None,
        clear_schedule: bool = False,
        thread_id: Optional[str] = None,
        recurrence: Optional[str] = None,
        clear_recurrence: bool = False,
    ) -> bool:
        """
        Update a TODO item.

        Args:
            todo_id: ID of the TODO to update
            status: New status
            notes: Add or update notes
            task: Update task description
            scheduled_for: Set/update scheduled execution time
            clear_schedule: If True, removes the schedule
            thread_id: Update thread context for scheduled execution
            recurrence: Recurrence interval as a canonical duration string
                (e.g. '5m', '2h', '1d'). Callers should pass values already
                validated by todo_constants.validate_recurrence.
            clear_recurrence: If True, removes the recurrence

        Returns:
            True if successful, False if not found.
        """
        item = self.get_item(todo_id)
        if not item:
            return False

        if status is not None:
            item.status = status

        if notes is not None:
            item.notes = notes[:1000] if notes else None

        if task is not None:
            item.task = task[:500]

        if clear_schedule:
            item.scheduled_for = None
        elif scheduled_for is not None:
            if item.schedule_paused_at is not None:
                # Resuming an auto-paused schedule (#154): the explicit
                # reschedule is the operator saying "try again", so the
                # whole failure episode ends here. Keyed on the pause
                # marker, NOT on any reschedule: the ticker's recurrence
                # re-arm also lands in this branch and must preserve the
                # consecutive-failure streak.
                item.schedule_paused_at = None
                item.consecutive_failures = 0
                item.last_failure = None
                item.last_failure_at = None
                item.delivery_failures = 0
                item.last_delivery_failure = None
                item.last_delivery_failure_at = None
                # A resumed series starts a fresh skip-alert window (#262):
                # its first late run after the resume alerts again.
                item.skip_alerted_at = None
                # The pause PREPENDED its reason to the notes (which are
                # prompt input and user instructions); strip that prefix so
                # a resumed TODO does not carry a stale pause banner.
                existing_notes = item.notes or ""
                stripped_notes = strip_leading_note_banner(
                    existing_notes, (PAUSE_NOTE_PREFIX,)
                )
                if stripped_notes != existing_notes:
                    item.notes = stripped_notes or None
            item.scheduled_for = scheduled_for
        # thread_id is independent of the schedule: an unspecified one is
        # preserved (scoping stays even when the schedule is cleared), and an
        # explicit rebind applies even in the same patch as clear_schedule
        # (pre-#143 the clear branch silently dropped it).
        if thread_id is not None:
            item.thread_id = thread_id

        if clear_recurrence:
            item.recurrence = None
            item.recurrence_anchor = None
        elif recurrence is not None:
            if recurrence != item.recurrence:
                # Recurrence changed: drop the stale month-end origin so the
                # next scheduled fire re-adopts a fresh anchor.
                item.recurrence_anchor = None
            item.recurrence = recurrence

        item.updated_at = utc_now()
        self.updated_at = utc_now()
        return True

    def complete_item(self, todo_id: str) -> bool:
        """
        Mark a TODO item as done.

        Returns:
            True if successful, False if not found.
        """
        item = self.get_item(todo_id)
        if not item:
            return False

        item.status = TodoStatus.DONE
        item.updated_at = utc_now()
        self.updated_at = utc_now()
        return True

    def delete_item(self, todo_id: str) -> Optional[TodoItem]:
        """
        Delete a TODO item.

        Returns:
            The deleted item, or None if not found.
        """
        for i, item in enumerate(self.items):
            if item.id == todo_id:
                deleted = self.items.pop(i)
                self.updated_at = utc_now()
                return deleted
        return None

    def delete_items_for_thread(self, thread_id: str) -> List[TodoItem]:
        """
        Delete all TODO items scoped to a thread.

        Returns:
            Deleted TodoItem objects.
        """
        deleted = [item for item in self.items if item.thread_id == thread_id]
        if deleted:
            self.items = [item for item in self.items if item.thread_id != thread_id]
            self.updated_at = utc_now()
        return deleted

    def get_active_todos(self) -> List[TodoItem]:
        """Get all active (non-done) TODO items."""
        return [item for item in self.items if item.is_active()]

    def get_active_todos_for_thread(self, thread_id: str) -> List[TodoItem]:
        """Get active TODO items scoped to a specific thread."""
        return [t for t in self.items if t.thread_id == thread_id and t.is_active()]

    def get_thread_task_counts(self) -> Dict[str, int]:
        """Get active task count per thread_id for badge display."""
        counts: Dict[str, int] = {}
        for item in self.items:
            if item.is_active():
                counts[item.thread_id] = counts.get(item.thread_id, 0) + 1
        return counts

    def get_stale_todos(self, staleness_hours: int) -> List[TodoItem]:
        """Get TODO items that haven't been updated in staleness_hours."""
        return [item for item in self.items if item.is_stale(staleness_hours)]

    def get_scheduled_todos(self) -> List[TodoItem]:
        """Get TODO items that have a scheduled execution time."""
        return [item for item in self.items if item.is_scheduled()]

    def archive_completed(self, days_old: int = 7) -> int:
        """
        Remove completed items older than days_old.

        Returns:
            Number of items archived.
        """
        threshold = utc_now() - timedelta(days=days_old)
        original_count = len(self.items)

        self.items = [
            item
            for item in self.items
            if item.status != TodoStatus.DONE or ensure_aware_utc(item.updated_at) > threshold
        ]

        archived = original_count - len(self.items)
        if archived > 0:
            self.updated_at = utc_now()
        return archived


# Bounded wait for the per-user lock on the repair path (see
# ``TodoManager._load`` for the lock-order reason it is not a plain acquire).
_REPAIR_LOCK_TIMEOUT_SECONDS = 10.0

# (requested user, embedded id) pairs already warned about, so a mismatched
# list that is only ever read does not log a warning on every turn.
_rebind_warned: set = set()

# user_ids whose list is unreadable and already logged at ERROR this episode.
_unavailable_logged: set = set()


class TodoListUnavailableError(StoreUnavailableError):
    """A write was refused because the user's list file exists but could not
    be read or repaired (#394). Saving would replace it with a stand-in."""


class _Unloadable:
    """A list file that exists and reads but does not load."""

    __slots__ = ("data", "reason", "raw")

    def __init__(self, data: object, reason: str, raw: bytes) -> None:
        self.data = data  # the parsed JSON, or None when the bytes did not parse
        self.reason = reason
        self.raw = raw  # the exact bytes read, preserved by the quarantine copy


def _read_list(todos_path: Path) -> "TodoList | OSError | _Unloadable | None":
    """One read of a list file: the list, ``None`` when absent, the read
    error, or an ``_Unloadable`` carrying the reason, the bytes, and the
    parsed data when the bytes were JSON."""
    try:
        raw = todos_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        return e
    try:
        # json.loads on BYTES detects UTF-8 (with or without a BOM), UTF-16
        # and UTF-32, so a Windows editor or PowerShell 5.1 redirect loads.
        # Anything else (Set-Content's cp1252 with a non-ASCII byte) is
        # corrupt: UnicodeDecodeError is a ValueError, so it lands here.
        data = json.loads(raw)
    except (ValueError, RecursionError) as e:
        return _Unloadable(None, f"not parseable as JSON ({type(e).__name__})", raw)
    try:
        return TodoList.model_validate(data)
    except Exception as e:  # noqa: BLE001 - one bad field must not cost the list
        return _Unloadable(data, validation_summary(e), raw)


def _salvage_todo_list(data: object, user_id: str) -> "tuple[TodoList, int, bool]":
    """Keep every item of a failed list that validates on its own.

    Returns ``(list, dropped_count, had_items)``; ``had_items`` is False when
    there was no ``items`` list to salvage from (undecodable bytes, a
    non-object document, ``items`` missing or not a list), which the report
    words as "could not be parsed" rather than "0 kept". The envelope
    (created_at, a stored MAX_TODOS) is kept when it validates on its own,
    else defaulted.
    """
    if not isinstance(data, dict):
        return TodoList(user_id=user_id), 0, False
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        return TodoList(user_id=user_id), 0, False
    kept: List[TodoItem] = []
    dropped = 0
    for raw in raw_items:
        try:
            kept.append(TodoItem.model_validate(raw))
        except Exception:  # noqa: BLE001 - drop only the bad item
            dropped += 1
    envelope = {key: value for key, value in data.items() if key != "items"}
    try:
        todo_list = TodoList.model_validate({**envelope, "items": []})
    except Exception:  # noqa: BLE001 - a bad envelope must not cost the items
        todo_list = TodoList(user_id=user_id)
    todo_list.items = kept
    return todo_list, dropped, True


def _items_phrase(count: int, adjective: str = "") -> str:
    """``1 item was`` / ``2 items were``, for the owner alert."""
    if count == 1:
        return f"1 {adjective}item was"
    return f"{count} {adjective}items were"


def _report_todo_repair(
    user_id: str,
    quarantine_name: str,
    kept: int,
    dropped: int,
    had_items: bool,
) -> None:
    """Audit row now, owner alert off-thread. Never raises."""
    where = f"todos/quarantine/{quarantine_name}"
    if not had_items:
        detail = "corrupt TODO list preserved; the list starts empty"
        alert = (
            f"[TODO LIST CORRUPT] Your TODO list file could not be parsed "
            f"(edited by hand or by a tool?), so a new, empty list started. "
            f"Nothing was deleted: the original is preserved at {where}, and "
            f"an admin can restore items from it."
        )
    else:
        detail = (
            f"invalid TODO list repaired: kept {kept} item(s), dropped "
            f"{dropped}"
        )
        alert = (
            f"[TODO LIST REPAIRED] Your TODO list file failed validation "
            f"(edited by hand or by a tool?). {_items_phrase(kept)} kept"
            + (
                f"; {_items_phrase(dropped, 'unreadable ')} dropped from the "
                f"live list"
                if dropped
                else ""
            )
            + f". The original file is preserved at {where}."
        )
    try:
        from .activity_log import log_external_edit

        log_external_edit(
            "todos", f"{detail} (original at quarantine/{quarantine_name})",
            user_id=user_id,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Failed to record TODO quarantine audit", exc_info=True)

    def _send() -> None:
        try:
            from ..config.settings import get_settings
            from .notification_dispatch import send_owner_alert

            send_owner_alert(alert, get_settings(), user_id=user_id)
        except Exception:  # noqa: BLE001
            logger.warning("TODO repair alert failed for %s", user_id, exc_info=True)

    threading.Thread(target=_send, name="todo-repair-alert", daemon=True).start()


def _list_json_object(raw: bytes) -> "dict | None":
    """Parse a list file for the raw scanners; None when it is not a JSON object."""
    data = json.loads(raw)
    return data if isinstance(data, dict) else None


class TodoManager:
    """Manages TODO lists on disk with thread-safe operations."""

    def __init__(self, data_dir: Path):
        """
        Initialize the TODO manager.

        Args:
            data_dir: Base data directory (TODOs stored in data_dir/todos/)
        """
        self.todos_dir = data_dir / "todos"
        self.todos_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"TodoManager initialized with directory: {self.todos_dir}")

    def _get_lock(self, user_id: str) -> threading.RLock:
        """Get or create a lock for a specific user."""
        return _todo_locks.get(user_id)

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """
        Context manager for atomic TODO list updates.

        Usage:
            with manager.atomic_update("default") as todo_list:
                todo_list.add_item("New task")
            # List is automatically saved when exiting the context

        This ensures that multiple concurrent modifications don't overwrite each other.

        Raises ``RuntimeError`` if the save fails, so a disk error is surfaced
        rather than silently dropping the mutation (matching the
        ``delete_todos_for_thread`` contract). The save still runs in
        ``finally``, but its result is only raised on the success path, so an
        exception from inside the block propagates first and is never masked.
        """
        lock = self._get_lock(user_id)
        with lock:
            todo_list, writable = self._load(user_id)
            if not writable:
                # The file exists but could not be read (permissions, I/O):
                # saving would replace bytes that may well read fine later
                # with whatever this block builds on an empty list (#394).
                raise TodoListUnavailableError(
                    f"TODO list for user {user_id} could not be read or "
                    f"repaired; refusing to overwrite it"
                )
            try:
                yield todo_list
            finally:
                saved_ok = self.save_todos(todo_list)
            if not saved_ok:
                raise RuntimeError(f"Failed to persist TODOs for user {user_id}")

    def _get_todos_path(self, user_id: str) -> Path:
        """Get the path to a user's TODO file."""
        safe_user_id = safe_path_segment(user_id)
        return self.todos_dir / f"{safe_user_id}.json"

    def get_todos(self, user_id: str = "default") -> TodoList:
        """
        Load a user's TODO list from disk, or create a new one if it doesn't exist.

        A file that does not load is never answered with an empty list that
        the next save would write over it (#394): see ``_load``.

        Args:
            user_id: User identifier (defaults to "default")

        Returns:
            TodoList instance
        """
        return self._load(user_id)[0]

    def load_todos(self, user_id: str) -> "tuple[TodoList, bool]":
        """``get_todos`` plus whether the list is AUTHORITATIVE.

        ``False`` means the file exists but could not be read or repaired
        right now (see ``_load``): the list is empty or a read-only salvaged
        view, so an item's absence proves nothing about the file. Callers
        that act on absence (the ticker dropping a schedule row) check it.
        """
        return self._load(user_id)

    def _load(self, user_id: str) -> "tuple[TodoList, bool]":
        """Load a user's list; returns ``(todo_list, writable)``.

        Every save path builds on this load and ``atomic_update`` saves on
        exit, so a load failure that answered "empty" used to destroy the
        whole list on the next write, including unattended ones (the hourly
        archive sweep, slim's startup migration). Now:

        - A UTF-8 BOM (a Windows editor save) is tolerated.
        - A file that does not load (not UTF-8, not JSON, or failing
          validation) goes to ``_repair_unloadable``: what validates is kept,
          the original bytes are preserved in quarantine.
        - An unreadable file (``OSError``) is left in place and reported NOT
          writable, so ``atomic_update`` refuses rather than overwrite it.

        The happy path takes no lock, as before: ``rebuild_from_todos`` reads
        lists while holding the schedule index lock, and ``atomic_update``
        bodies take that index lock while holding the TODO lock, so a
        blocking TODO-lock acquire here would be a lock-order inversion.
        """
        loaded = _read_list(self._get_todos_path(user_id))
        if isinstance(loaded, _Unloadable):
            return self._repair_unloadable(user_id)
        return self._settled(loaded, user_id)

    def _settled(self, loaded: "TodoList | OSError | None", user_id: str) -> "tuple[TodoList, bool]":
        """The ``(list, writable)`` answer for a read that needs no repair."""
        if isinstance(loaded, TodoList):
            _unavailable_logged.discard(user_id)
            return self._bind_to_requested_user(loaded, user_id), True
        if loaded is None:
            _unavailable_logged.discard(user_id)
            return TodoList(user_id=user_id), True
        if user_id in _unavailable_logged:
            logger.debug("TODOs for %s still unreadable: %s", user_id, loaded)
        else:
            # Once per episode: every prompt build reads the list. The owner
            # alert comes from the ticker when a due TODO is deferred.
            _unavailable_logged.add(user_id)
            logger.error(f"Could not read TODOs for {user_id}: {loaded}")
        return TodoList(user_id=user_id), False

    @staticmethod
    def _bind_to_requested_user(todo_list: TodoList, user_id: str) -> TodoList:
        """Keep a loaded list saving to the file it came from.

        ``save_todos`` routes by the EMBEDDED ``user_id``, so a list whose id
        names another user (a copied or hand-edited file) would write over
        that user's file on its next save. Compared as path segments: the
        sweeps pass a filename stem, which sanitizes the same as the real id.
        """
        if safe_path_segment(todo_list.user_id) != safe_path_segment(user_id):
            key = (user_id, todo_list.user_id)
            if key not in _rebind_warned:
                _rebind_warned.add(key)
                logger.warning(
                    "TODO list loaded for %s claims user_id %r; rebinding it "
                    "to the file it was loaded from",
                    user_id,
                    todo_list.user_id,
                )
            todo_list.user_id = user_id
        return todo_list

    def _repair_unloadable(self, user_id: str) -> "tuple[TodoList, bool]":
        """Salvage what validates, preserve the original, replace the file.

        Holds the per-user lock (re-entrant, so ``atomic_update``'s own hold
        is fine) and RE-READS under it: a concurrent save may already have
        replaced the bad file. The acquire is bounded because a plain reader
        may hold the schedule index lock (see ``_load``); on timeout a
        healthy re-read is served as usual (the holder usually repaired it),
        a still-corrupt file as a read-only salvaged view.

        The original bytes are COPIED to quarantine and the salvaged list then
        atomically replaces the file, so the list file never goes absent (a
        lock-free reader in a rename-then-write gap would take "no file" as an
        authoritative empty list). Just before replacing, the file is checked
        unchanged since the read: the lock is in-process only and a Docker api
        and worker share the file, so this narrows (does not close) the
        window in which the other process's write could be overwritten with
        this stale salvage.
        """
        todos_path = self._get_todos_path(user_id)
        lock = self._get_lock(user_id)
        if not lock.acquire(timeout=_REPAIR_LOCK_TIMEOUT_SECONDS):
            loaded = _read_list(todos_path)
            if not isinstance(loaded, _Unloadable):
                return self._settled(loaded, user_id)
            logger.warning(
                "TODO list for %s needs repair but its lock is busy; serving "
                "a read-only salvaged view",
                user_id,
            )
            view = _salvage_todo_list(loaded.data, user_id)[0]
            return self._bind_to_requested_user(view, user_id), False
        try:
            loaded = _read_list(todos_path)
            if not isinstance(loaded, _Unloadable):
                return self._settled(loaded, user_id)
            salvaged, dropped, had_items = _salvage_todo_list(loaded.data, user_id)
            salvaged = self._bind_to_requested_user(salvaged, user_id)
            quarantine = quarantine_copy(todos_path, loaded.raw)
            if quarantine is None:
                logger.error(
                    "Failed to load TODOs for %s: %s; could not preserve the "
                    "original, file left in place and writes refused",
                    user_id,
                    loaded.reason,
                )
                return salvaged, False
            try:
                unchanged = todos_path.read_bytes() == loaded.raw
            except OSError:
                unchanged = False
            if not unchanged:
                # Another process replaced the file after the read. The
                # quarantine copy still holds the bytes this load saw; the
                # new file is theirs to keep.
                logger.warning(
                    "TODO list for %s changed during repair; keeping the new "
                    "file (the bytes read are preserved at %s)",
                    user_id,
                    quarantine,
                )
                again = _read_list(todos_path)
                if isinstance(again, _Unloadable):
                    return salvaged, False
                return self._settled(again, user_id)
            if not self.save_todos(salvaged):
                # The original is still the live file: drop this copy so a
                # full disk does not grow a copy per load, and refuse writes.
                try:
                    quarantine.unlink()
                except OSError:
                    pass  # a leftover copy is harmless; the refusal below stands
                logger.error(
                    "Failed to write back salvaged TODOs for %s; original "
                    "left in place and writes refused",
                    user_id,
                )
                return salvaged, False
            kept = len(salvaged.items)
            logger.error(
                "Failed to load TODOs for %s: %s; kept %d item(s), dropped %d, "
                "original preserved at %s",
                user_id,
                loaded.reason,
                kept,
                dropped,
                quarantine,
            )
        finally:
            lock.release()
        _report_todo_repair(user_id, quarantine.name, kept, dropped, had_items)
        return salvaged, True

    def save_todos(self, todo_list: TodoList) -> bool:
        """
        Save a user's TODO list to disk.

        Args:
            todo_list: TodoList to save

        Returns:
            True if successful
        """
        todos_path = self._get_todos_path(todo_list.user_id)

        try:
            # Ensure directory exists
            todos_path.parent.mkdir(parents=True, exist_ok=True)

            # Update timestamp
            todo_list.updated_at = utc_now()

            write_text_atomic(
                todos_path,
                json.dumps(todo_list.model_dump(mode="json"), indent=2, default=str),
            )
            logger.debug(f"Saved TODO list for user: {todo_list.user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to save TODOs for {todo_list.user_id}: {e}")
            return False

    def get_all_users_with_todos(self) -> List[str]:
        """Get all user IDs that have at least one TODO item.

        A file that cannot be read or parsed is LISTED (#394), not skipped:
        the sweeps and the schedule rebuild then load it through ``_load``,
        which repairs a corrupt file and reports an unreadable one as
        non-authoritative, instead of the user silently dropping out of
        every sweep (and, at the next restart, out of the schedule index).
        """
        users = []
        if self.todos_dir.exists():
            for path in self.todos_dir.iterdir():
                if not (path.is_file() and path.suffix == ".json"):
                    continue
                try:
                    data = _list_json_object(path.read_bytes())
                except (OSError, ValueError, RecursionError) as e:
                    logger.warning("TODO list %s does not load: %s", path, e)
                    users.append(path.stem)
                    continue
                if data is None or not isinstance(data.get("items", []), list):
                    users.append(path.stem)  # wrong shape: let a load repair it
                elif data.get("items"):
                    users.append(path.stem)
        return sorted(users)

    def find_owner(self, todo_id: str) -> Optional[str]:
        """Return the user id owning ``todo_id``, or None.

        One pass over the per-user TODO files (raw JSON, no model
        validation): callers that know only a todo id (the #247
        delivery-report route) must resolve the owner server-side rather
        than trust a caller-supplied identity. Ids are 8-char uuid4
        prefixes; a cross-user collision resolves to the first sorted user
        (negligible probability, documented rather than defended).
        """
        if not self.todos_dir.exists():
            return None
        for path in sorted(self.todos_dir.iterdir()):
            if not (path.is_file() and path.suffix == ".json"):
                continue
            try:
                data = _list_json_object(path.read_bytes())
            except (OSError, ValueError, RecursionError) as e:
                logger.warning("Skipping unreadable TODO list %s: %s", path, e)
                continue
            items = data.get("items") if data is not None else None
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("id") == todo_id:
                    return path.stem
        return None

    def delete_todos(self, user_id: str) -> bool:
        """
        Delete a user's TODO list.

        Args:
            user_id: User identifier

        Returns:
            True if deleted, False if not found
        """
        todos_path = self._get_todos_path(user_id)

        if todos_path.exists():
            try:
                todos_path.unlink()
                logger.info(f"Deleted TODO list for user: {user_id}")
                return True
            except Exception as e:
                logger.error(f"Failed to delete TODOs for {user_id}: {e}")
                return False
        return False

    def delete_todos_for_thread(self, user_id: str, thread_id: str) -> List[TodoItem]:
        """
        Delete every TODO for ``user_id`` that belongs to ``thread_id``.

        Returns:
            Deleted TODO items.
        """
        lock = self._get_lock(user_id)
        with lock:
            todo_list, writable = self._load(user_id)
            if not writable:
                raise TodoListUnavailableError(
                    f"TODO list for user {user_id} could not be read or "
                    f"repaired; its items for thread {thread_id} were not removed"
                )
            deleted = todo_list.delete_items_for_thread(thread_id)
            if deleted:
                if not self.save_todos(todo_list):
                    raise RuntimeError(
                        f"Failed to save TODO cleanup for user {user_id}"
                    )
            return deleted

    # =========================================================================
    # Schedule synchronization methods
    # =========================================================================

    def sync_schedule_to_db(
        self,
        user_id: str,
        todo_id: str,
        schedule_db: "TodoScheduleDB",
    ) -> None:
        """
        Sync a TODO's schedule to the schedule database.

        Called after adding/updating a TODO with scheduled_for.

        Args:
            user_id: User ID
            todo_id: TODO ID to sync
            schedule_db: TodoScheduleDB instance
        """

        todo_list, authoritative = self._load(user_id)
        if not authoritative:
            # The item's absence from an unreadable list proves nothing, so
            # leave its index row alone (#394).
            return
        todo = todo_list.get_item(todo_id)

        if todo and todo.scheduled_for and todo.is_active():
            schedule_db.add_scheduled(
                todo_id=todo.id,
                user_id=user_id,
                scheduled_for=todo.scheduled_for,
                task_preview=todo.task[:100],
                thread_id=todo.thread_id,
            )
        else:
            # No schedule or not active - remove from index
            schedule_db.remove_scheduled(todo_id)

    def clear_todo_schedule(
        self,
        user_id: str,
        todo_id: str,
        schedule_db: "TodoScheduleDB",
    ) -> bool:
        """
        Clear a TODO's schedule after execution.

        Sets last_execution to now, clears scheduled_for.

        This is the ONE writer that puts a completion time (rather than an
        intended slot) in `last_execution`, which the done-paths now anchor
        recurrence on (`todo_constants.resolve_done_recurrence_anchor`). It
        cannot poison that anchor. Both call sites are in
        `Ticker._handle_recurrence`, and they are the two branches where no
        next slot exists: the TODO has no recurrence at all, or
        `compute_recurrence_reschedule` returned None. The latter is only
        reachable with a recurrence string that `parse_recurrence_interval`
        cannot parse (`validate_recurrence` rejects those on every input path,
        so it means hand-edited data), and the same unparseable string makes
        every done-path's `compute_recurrence_reschedule` return None too, so
        the anchor is computed and then discarded without moving a schedule.
        The #154 and #247 auto-pause paths do NOT come through here: they use
        `update_item(clear_schedule=True)`, which never touches
        `last_execution`.

        Args:
            user_id: User ID
            todo_id: TODO ID
            schedule_db: TodoScheduleDB instance

        Returns:
            True if successful
        """

        with self.atomic_update(user_id) as todo_list:
            todo = todo_list.get_item(todo_id)
            if todo:
                todo.last_execution = utc_now()
                todo.scheduled_for = None
                schedule_db.remove_scheduled(todo_id)
                return True
        return False

    def migrate_unscoped_todos(self, user_id: str, default_thread_id: str = "legacy") -> int:
        """
        Migrate TODOs that have no thread_id to the given default.

        This is idempotent: the model_validator already backfills 'legacy',
        but calling this ensures the file on disk is updated too.

        Returns:
            Number of items migrated.
        """
        migrated = 0
        with self.atomic_update(user_id) as todo_list:
            for item in todo_list.items:
                if not item.thread_id or item.thread_id == "legacy":
                    if item.thread_id != default_thread_id:
                        item.thread_id = default_thread_id
                        migrated += 1
        if migrated:
            logger.info(f"Migrated {migrated} unscoped TODO(s) for user {user_id} -> thread '{default_thread_id}'")
        return migrated

    def get_todo_by_id(self, user_id: str, todo_id: str) -> Optional[TodoItem]:
        """
        Get a specific TODO item by ID.

        Args:
            user_id: User ID
            todo_id: TODO ID

        Returns:
            TodoItem or None if not found
        """
        todo_list = self.get_todos(user_id)
        return todo_list.get_item(todo_id)
