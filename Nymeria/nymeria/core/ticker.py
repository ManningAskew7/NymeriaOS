"""Global polling ticker for scheduled task execution.

The Ticker now polls scheduled TODOs instead of a separate task database.
When a TODO has a scheduled_for time that has passed, the Ticker wakes up
and executes the TODO's task via the agent.
"""

import atexit
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, NamedTuple, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel

from .activity_log import ActivityType, log_activity
from .event_bus import publish_agent_stream_chunk, publish_autonomous_event
from .memory_index import MemoryIndex
from .notification_dispatch import (
    create_autonomous_notification,
    send_owner_alert,
    should_notify_autonomous,
)
from .notifications import NOTIFICATION_SUMMARY_MAX_CHARS
from .pending_prompt_queue import PENDING_QUEUE_META_EVENT_TYPES
from .scheduler_lock import LockOutcome, ScheduleLock
from .scheduler_state import (
    SchedulerStateManager,
    parse_state_time,
    read_release_request,
)
from .storage_paths import safe_path_segment
from .stream_bridge import StreamCollection, stream_and_collect
from .time_utils import ensure_aware_utc
from .todo_schedule_db import ScheduledTodoEntry, TodoScheduleDB
from .todo_manager import (
    BACKOFF_NOTE_PREFIX,
    GIVEUP_NOTE_PREFIX,
    PAUSE_NOTE_PREFIX,
    TodoManager,
    TodoStatus,
    format_note_banner,
    prepend_note_banner,
)
from .trigger_manager import TriggerManager
from .turn_executor import TurnExecutor
from .watchdog_sweep import WatchdogSweep

if TYPE_CHECKING:
    from ..config.settings import Settings
    from .agent import NymeriaAgent
    from .thread_config import ThreadConfigManager
    from .user_profile import UserProfileManager

logger = logging.getLogger(__name__)

# A due TODO whose list file cannot be read or repaired right now is retried
# this much later instead of being dropped from the schedule index (#394).
UNREADABLE_LIST_RETRY_SECONDS = 300

# Console for printing ticker output
# safe_box=True uses ASCII box-drawing characters, avoiding UnicodeEncodeError
# on Windows consoles that use cp1252/charmap encoding
_console = Console(force_terminal=True, safe_box=True)


def _format_slot(moment: "float | datetime") -> str:
    """Render an epoch or datetime for alert and row copy, in the user's zone.

    Minute precision with the zone key (``2026-09-24 13:00 Australia/Sydney``),
    since these lines reach a person over Telegram or the app; metadata keeps
    ISO timestamps. Falls back to UTC if the configured zone cannot load.
    """
    if isinstance(moment, datetime):
        value = (
            moment.replace(tzinfo=timezone.utc)
            if moment.tzinfo is None
            else moment
        )
    else:
        value = datetime.fromtimestamp(moment, timezone.utc)
    try:
        from .time_utils import get_user_tz

        zone = get_user_tz()
        return f"{value.astimezone(zone).strftime('%Y-%m-%d %H:%M')} {zone.key}"
    except Exception:
        return f"{value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"


class _RunStamp(NamedTuple):
    """When a scheduled run actually STARTED in the pool (#395).

    Stamped by the pool thread, so a run still queued for a free slot has
    none and is never reported as running. The monotonic time measures how
    long it has run (immune to wall-clock jumps and host sleep); the wall
    time is only for display.
    """

    entry: "ScheduledTodoEntry"
    started_mono: float
    started_wall: float


def _sanitize_unicode(text: str) -> str:
    """
    Sanitize text for Windows console output.

    Replaces common Unicode characters with ASCII equivalents,
    then strips any remaining non-ASCII characters.
    """
    # Replace common Unicode symbols with ASCII equivalents
    replacements = {
        '\u2713': '[x]',  # ✓ checkmark
        '\u2714': '[x]',  # ✔ heavy checkmark
        '\u2715': '[ ]',  # ✕ multiplication x
        '\u2716': '[ ]',  # ✖ heavy multiplication x
        '\u2717': '[ ]',  # ✗ ballot x
        '\u2718': '[ ]',  # ✘ heavy ballot x
        '\u2022': '*',    # • bullet
        '\u2023': '>',    # ‣ triangular bullet
        '\u2192': '->',   # → rightwards arrow
        '\u2190': '<-',   # ← leftwards arrow
        '\u2194': '<->',  # ↔ left right arrow
        '\u2026': '...',  # … ellipsis
        '\u2013': '-',    # – en dash
        '\u2014': '--',   # — em dash
        '\u201c': '"',    # " left double quotation
        '\u201d': '"',    # " right double quotation
        '\u2018': "'",    # ' left single quotation
        '\u2019': "'",    # ' right single quotation
    }

    for char, replacement in replacements.items():
        text = text.replace(char, replacement)

    # Strip any remaining non-ASCII characters
    return re.sub(r'[^\x00-\x7F]+', '', text)


def _render_tool_line(
    pending_calls: dict, chunk: dict
) -> None:
    """Print a compact tool one-liner to the console on tool_result."""
    call_id = chunk.get("id", "")
    result = chunk.get("result", "")

    # Match result to its call
    call = pending_calls.pop(call_id, None)
    if call is None:
        result_name = chunk.get("name", "")
        for cid, c in list(pending_calls.items()):
            if c["name"] == result_name:
                call = pending_calls.pop(cid)
                break

    name = call["name"] if call else chunk.get("name", "tool")
    args = call.get("args", {}) if call else {}

    # Args preview
    args_preview = ""
    if args:
        if len(args) == 1:
            val = str(next(iter(args.values())))
            args_preview = (val[:80] + "...") if len(val) > 80 else val
        else:
            pairs = []
            for k, v in args.items():
                vs = str(v)
                if len(vs) > 40:
                    vs = vs[:37] + "..."
                pairs.append(f"{k}={vs}")
            joined = ", ".join(pairs)
            args_preview = (joined[:100] + "...") if len(joined) > 100 else joined

    # Result preview
    result_str = str(result).replace("\n", " ").strip()
    result_preview = (result_str[:120] + "...") if len(result_str) > 120 else result_str

    is_error = "Error:" in result_str
    result_style = "red" if is_error else "dim"

    # Sanitize dynamic content for Windows console
    name = _sanitize_unicode(name)
    args_preview = _sanitize_unicode(args_preview)
    result_preview = _sanitize_unicode(result_preview)

    parts = [f"  [yellow]>[/yellow] [yellow]{name}[/yellow]"]
    if args_preview:
        parts.append(f"[dim]{args_preview}[/dim]")
    if result_preview:
        parts.append(f"[dim]->[/dim] [{result_style}]{result_preview}[/{result_style}]")

    try:
        _console.print(" ".join(parts))
    except Exception:
        logger.debug("Console render failed for tool line")


class _TodoConsoleRenderer:
    """Console rendering state for a single TODO execution stream."""

    def __init__(self) -> None:
        self.pending_calls: dict = {}
        self.response_buffer: str = ""
        self.printed_header: bool = False
        self.had_tool_calls: bool = False

    def flush_response_buffer(self) -> None:
        if self.response_buffer.strip():
            try:
                if not self.printed_header:
                    _console.print()
                    _console.print("[bold green]Nymeria:[/bold green]")
                    self.printed_header = True
                elif self.had_tool_calls:
                    _console.print()
                _console.print(Markdown(_sanitize_unicode(self.response_buffer.strip())))
            except Exception:
                logger.debug("Console render failed for preamble flush")
            self.response_buffer = ""

    def render_chunk(self, chunk: dict) -> None:
        chunk_type = chunk.get("type")
        if chunk_type == "tool_call":
            self.flush_response_buffer()
            self.pending_calls[chunk.get("id", "")] = {
                "name": chunk.get("name", "unknown"),
                "args": chunk.get("args", {}),
            }
        elif chunk_type == "tool_result":
            _render_tool_line(self.pending_calls, chunk)
            self.had_tool_calls = True
        elif chunk_type == "response":
            content = chunk.get("content", "")
            if content:
                self.response_buffer += content

    def flush_remaining(self) -> None:
        remaining = self.response_buffer.strip()
        if remaining:
            try:
                if not self.printed_header:
                    _console.print()
                    _console.print("[bold green]Nymeria:[/bold green]")
                elif self.had_tool_calls:
                    _console.print()
                _console.print(Markdown(_sanitize_unicode(remaining)))
            except Exception as console_err:
                logger.warning(f"Console print failed (non-fatal): {console_err}")
            self.response_buffer = ""


def _stream_error_message(chunk: dict) -> str:
    error_content = chunk.get("content", "")
    error_code = chunk.get("code", "unknown")
    return error_content or f"Agent stream error (code={error_code})"


def _should_continue_after_limit(chunk: dict) -> bool:
    return (
        chunk.get("scope", "unknown") == "main_agent"
        and chunk.get("reason", "max_iterations") != "repeated_tool_result"
    )


# Global ticker instance
_ticker: Optional["Ticker"] = None


def get_ticker() -> Optional["Ticker"]:
    """Get the global ticker instance."""
    return _ticker


def set_ticker(ticker: Optional["Ticker"]) -> None:
    """Set the global ticker instance."""
    global _ticker
    _ticker = ticker


class Ticker:
    """
    Global polling ticker for scheduled TODO execution.

    Runs as a daemon thread, polling the TodoScheduleDB at a configurable
    interval for due TODOs. Executes them through the agent with
    _is_self_invoke=True.

    Features:
    - Polls scheduled TODOs (not a separate task database)
    - Configurable poll interval
    - Prevents duplicate execution with processing lock
    - Retries failed TODOs up to MAX_RETRIES
    - Recovery of missed schedules on startup
    """

    MAX_RETRIES = 3  # Max retries before marking failed
    DEFAULT_POLL_INTERVAL = 5  # Default seconds between polls

    def __init__(
        self,
        executor: TurnExecutor,
        settings: "Settings",
        schedule_db: TodoScheduleDB,
        todo_manager: TodoManager,
        thread_config_manager: "ThreadConfigManager",
        profile_manager: "UserProfileManager",
        *,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        busy_agent: Optional["NymeriaAgent"] = None,
        spawn_sweeper: Optional[Callable[[], int]] = None,
        dream_sweeper: Optional[Callable[[], int]] = None,
        take_over: bool = True,
    ):
        """
        Initialize the ticker.

        Args:
            executor: ``TurnExecutor`` used to run scheduled TODOs and
                trigger actions. In slim, this wraps the local agent; in
                Docker, it routes calls to the API container.
            settings: Resolved ``Settings`` for poll caps, archive
                window, context management, etc.
            schedule_db: ``TodoScheduleDB`` for polling scheduled TODOs.
            todo_manager: ``TodoManager`` for accessing TODO data.
            thread_config_manager: Per-thread config store (used for
                autonomous notification routing and trigger context).
            profile_manager: User profile store (used for RAG opt-in
                checks when indexing completion summaries).
            poll_interval: Seconds between polls (from settings).
            busy_agent: Optional ``NymeriaAgent`` for in-process busy
                checks and post-turn sliding-window trimming. Slim
                passes the local agent. The Docker worker passes None
                because the API runtime handles contention via its own
                ``ThreadLockManager`` + pending-prompt queue.
            spawn_sweeper: Optional callable that performs spawned-
                thread idle cleanup (returns deleted count). Slim
                passes a closure over its local agent; Docker passes
                None because the cleanup runs API-side via a
                housekeeping task.
            dream_sweeper: Optional callable that fires due dreams for
                dream-enabled threads (returns started count). Like
                ``spawn_sweeper``, slim injects a closure over its
                local agent and Docker passes None (the API heartbeat
                drives it, since the worker holds no agent to dream
                against).
            take_over: Whether a ticker that finds another process running
                this data dir's schedule takes it over when that process
                exits (#397). The services (slim, the API, the worker) do;
                the fat CLI passes False, so a CLI opened beside a service
                never grabs the schedule in the gap of a service restart.
        """
        self._turn_executor = executor
        self.settings = settings
        self.schedule_db = schedule_db
        self.todo_manager = todo_manager
        self.thread_config_manager = thread_config_manager
        self.profile_manager = profile_manager
        self._busy_agent = busy_agent
        self._spawn_sweeper = spawn_sweeper
        self._dream_sweeper = dream_sweeper
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._active_futures: Dict[str, Future] = {}  # todo_id -> running Future
        # Long-run visibility (#395), guarded by _lock and popped with the
        # future: the start stamp of every run that has STARTED in the pool,
        # and the start of a run already reported or already logged as
        # holding its TODO (once per run, not per poll; a queued run logs
        # under -1.0). ``_monotonic`` is the clock seam tests drive.
        self._active_runs: Dict[str, _RunStamp] = {}
        self._reported_stuck_runs: Dict[str, float] = {}
        self._logged_held_runs: Dict[str, float] = {}
        self._monotonic: Callable[[], float] = time.monotonic
        self._pool_size = 0
        # Execution-marker bookkeeping (#262), both guarded by _lock.
        # todo_id -> user_id whose marker release failed at run end; the
        # poll loop retries it so a failed DELETE cannot block the TODO for
        # the whole stale window.
        self._unreleased_markers: Dict[str, str] = {}
        # todo_id -> started_at of a leftover marker already reported as
        # blocking; one report per marker, reset by the next clean claim.
        self._reported_blocking_markers: Dict[str, float] = {}
        # todo_id -> epoch before which a due TODO is not resubmitted because
        # its list file could not be read or repaired (#394). In memory on
        # purpose: the schedule row keeps the item's own slot, since the
        # finalize path reads a row/item slot mismatch as a mid-run
        # reschedule and would fire the TODO a second time.
        self._list_unavailable_until: Dict[str, float] = {}
        # user_ids already alerted for the current unreadable-list episode;
        # cleared by that user's next authoritative load.
        self._list_unavailable_alerted: set[str] = set()
        self._lock = threading.Lock()
        self._retry_counts: dict = {}  # Track retries per TODO
        self._executor: Optional[ThreadPoolExecutor] = None
        self._housekeeping_executor: Optional[ThreadPoolExecutor] = None

        # Trigger system: poll-based sources checked at a slower interval.
        # Runs off the main loop via a housekeeping executor so network I/O
        # in source checks cannot delay scheduled TODO execution or occupy
        # autonomous workers.
        self.trigger_poll_interval = max(poll_interval * 6, 30)  # Default 30s
        self._last_trigger_check: float = 0.0
        self._trigger_manager: Optional[TriggerManager] = None
        self._trigger_poll_running = False
        self._trigger_poll_lock = threading.Lock()

        # Auto-purge: archive completed TODOs periodically (~1 hour)
        self._archive_interval = 3600  # 1 hour
        self._last_archive_check: float = 0.0

        # Idle-sweep: delete temporary-lifetime spawned threads whose
        # idle_timeout_hours has elapsed since their last activity.
        self._spawn_sweep_interval = 1800  # 30 minutes
        self._last_spawn_sweep_check: float = 0.0

        # Dream sweep: fire self-reflection dreams for dream-enabled threads
        # that have gone quiet. The per-thread gates do the real rate limiting;
        # this interval is just the polling granularity.
        from .dreaming import DREAM_SWEEP_INTERVAL_SECONDS

        self._dream_sweep_interval = DREAM_SWEEP_INTERVAL_SECONDS
        self._last_dream_sweep_check: float = 0.0

        # Watchdog sweep: nudge threads about stale TODOs (the fold of the
        # former standalone watchdog process). Supervisory role stays legible
        # via its own enable flag, interval, and kill switches; detection
        # runs on the housekeeping executor and nudge turns on the
        # autonomous pool, like trigger fires.
        self._watchdog_sweep: Optional[WatchdogSweep] = None
        if getattr(settings, "watchdog_enabled", False):
            self._watchdog_sweep = WatchdogSweep(
                executor=executor,
                settings=settings,
                todo_manager=todo_manager,
                thread_config_manager=thread_config_manager,
            )
        self._watchdog_sweep_interval = (
            max(1, getattr(settings, "watchdog_interval_minutes", 5)) * 60
        )
        self._last_watchdog_sweep_check: float = 0.0
        self._watchdog_sweep_running = False
        self._watchdog_sweep_lock = threading.Lock()

        self._recovery_lock = threading.Lock()
        self._startup_recovery_prepared = False
        # One scheduler per data dir, across processes (#397). The ticker
        # claims this lock before it touches the schedule and holds it until
        # the process exits (the OS drops it then, a crash or an exec
        # included); without it the ticker stands by and keeps the holder's
        # description here for the status and the one notice. Guarded by
        # _recovery_lock.
        self._schedule_lock = ScheduleLock(settings.data_dir)
        self._owns_schedule = False
        self._standby_holder: Optional[str] = None
        # Stood by and not yet recovered as the owner: the next successful
        # claim is a TAKEOVER, even when the first takeover recovery raised.
        self._stood_by = False
        self._can_take_over = take_over
        self._lock_file_alarm: Optional[LockOutcome] = None
        self._missed_work_policy = getattr(
            settings,
            "scheduler_missed_work_policy",
            "run",
        )
        self._active_execution_stale_seconds = int(
            getattr(settings, "scheduler_active_execution_stale_minutes", 1440)
        ) * 60
        # Recurring-failure policy thresholds (#154); 0 disables a stage.
        self._failure_alert_after = max(
            0, int(getattr(settings, "scheduler_failure_alert_after", 2))
        )
        self._failure_pause_after = max(
            0, int(getattr(settings, "scheduler_failure_pause_after", 5))
        )
        # Skipped-occurrence alert policy (#262); 0 disables the alert (the
        # activity rows are always written), cooldown 0 alerts every time.
        self._skip_alert_after = max(
            0, int(getattr(settings, "scheduler_skip_alert_after", 1))
        )
        self._skip_alert_cooldown_seconds = max(
            0,
            int(getattr(settings, "scheduler_skip_alert_cooldown_minutes", 1440)),
        ) * 60
        # A run still going this long is reported once (#395); 0 disables.
        self._stuck_run_alert_seconds = max(
            0, int(getattr(settings, "scheduler_run_stuck_alert_minutes", 60))
        ) * 60
        self._scheduler_state = SchedulerStateManager(settings.data_dir)
        persisted_state = self._scheduler_state.load()
        self._pending_startup_missed_ids: set[str] = set(
            persisted_state.get("pending_missed_todo_ids") or []
        )
        self._trigger_catchup_paused = bool(
            persisted_state.get("trigger_catchup_paused")
        )
        # When the hold this owner keeps was detected: a release request
        # (#398, written by another process) counts only if it is newer, so a
        # request left from before a restart never releases the new hold.
        # Set by the recovery that establishes or adopts a hold.
        self._hold_since: Optional[datetime] = None

    def prepare_startup_recovery(self) -> dict[str, Any]:
        """Claim this data dir's schedule, then synchronize its state.

        A ticker that cannot claim it (another process runs the scheduler for
        this data dir, #397) does none of the recovery, since every step would
        act on the owner's live state: clearing its in-flight execution
        markers, re-deciding its missed work. It returns a standby status; a
        ticker that may take over retries the claim from its poll loop.
        """
        with self._recovery_lock:
            if self._startup_recovery_prepared:
                return self.get_scheduler_status()
            status = self._claim_and_recover_locked()
            if status is None:
                status = self.get_scheduler_status()
                status.update(
                    {
                        "indexed_schedule_count": 0,
                        "startup_missed_count": 0,
                        "startup_execution_markers_cleared": 0,
                    }
                )
            return status

    def _claim_and_recover_locked(self) -> Optional[dict[str, Any]]:
        """Claim the schedule and recover; None means stand by. Caller holds
        _recovery_lock.

        The ONE claim rule for both callers (startup, which ``start()``
        repeats on a standby, and the poll): a ticker that has stood by
        claims only if it may take over, and then recovers as a takeover.
        """
        if self._stood_by and not self._can_take_over:
            return None
        if not self._claim_schedule():
            return None
        return self._recover_locked(takeover=self._stood_by)

    def _recover_locked(self, *, takeover: bool) -> dict[str, Any]:
        """The startup recovery. Caller holds _recovery_lock and the schedule.

        A TAKEOVER (the previous owner process exited while this one stood
        by) clears the markers and rebuilds the index like a start, since the
        owner's runs died with it, but decides no missed work: a scheduler ran
        until moments ago, so nothing was missed, and under ``ask`` the hold
        the owner persisted is adopted rather than replaced.
        """
        self._scheduler_state.record_start()
        # A single ticker owns the schedule DB (the lock enforces it across
        # processes, #397), so any execution marker present now is orphaned:
        # by a crash or a non-graceful shutdown that left a mid-run daemon
        # thread's marker behind, or by the owner a takeover replaces. Clear
        # them all here (before this ticker polls) so a TODO interrupted
        # moments before a restart re-fires immediately instead of being
        # held by the 24h stale sweep. The stale sweep still guards the
        # live in-flight path (mark_execution_started / is_execution_active)
        # against a wedged thread within a running process.
        startup_markers_cleared = self.schedule_db.clear_all_executions()
        indexed = self.rebuild_schedule_index()

        missed_ids: list[str] = []
        self._hold_since = None
        if takeover:
            persisted = self._scheduler_state.load()
            held = [
                str(todo_id)
                for todo_id in persisted.get("pending_missed_todo_ids") or []
                if todo_id
            ]
            if self._missed_work_policy == "ask" and held:
                self._pending_startup_missed_ids = set(held)
                self._trigger_catchup_paused = True
                # A request made against the adopted hold still counts.
                self._hold_since = parse_state_time(
                    persisted.get("last_missed_detection_at")
                ) or datetime.now(timezone.utc)
            else:
                self._pending_startup_missed_ids.clear()
                self._trigger_catchup_paused = False
                self._scheduler_state.clear_pending_missed()
        else:
            missed_ids = [
                entry.todo_id
                for entry in self.schedule_db.get_due(before=time.time())
            ]
            if self._missed_work_policy == "ask" and missed_ids:
                self._pending_startup_missed_ids = set(missed_ids)
                self._trigger_catchup_paused = True
                persisted = self._scheduler_state.set_pending_missed(
                    missed_ids,
                    trigger_catchup_paused=True,
                )
                self._hold_since = parse_state_time(
                    persisted.get("last_missed_detection_at")
                ) or datetime.now(timezone.utc)
                self._print_recovery_notice(
                    len(missed_ids),
                    "held until an admin releases them (/scheduler release)",
                )
            else:
                self._pending_startup_missed_ids.clear()
                self._trigger_catchup_paused = False
                self._scheduler_state.clear_pending_missed()
                if missed_ids:
                    self._print_recovery_notice(len(missed_ids), "executing now")

        self._startup_recovery_prepared = True
        self._stood_by = False
        status = self.get_scheduler_status()
        status.update(
            {
                "indexed_schedule_count": indexed,
                "startup_missed_count": len(missed_ids),
                "startup_execution_markers_cleared": startup_markers_cleared,
            }
        )
        return status

    def _claim_schedule(self) -> bool:
        """Take the schedule lock; False means stand by. Holds _recovery_lock."""
        if self._owns_schedule:
            return True
        outcome, detail = self._schedule_lock.acquire()
        if outcome is LockOutcome.HELD_ELSEWHERE:
            if self._standby_holder is None:
                logger.warning(
                    "Scheduler on standby: %s runs the scheduler for %s. This "
                    "process runs no scheduled TODOs, trigger polls or sweeps "
                    "%s.",
                    detail,
                    self._schedule_lock.path.parent,
                    "until that process exits, then takes over"
                    if self._can_take_over
                    else "for as long as it runs",
                )
                self._print_standby_notice(detail, self._can_take_over)
            self._standby_holder = detail
            self._stood_by = True
            return False
        if outcome is LockOutcome.UNAVAILABLE:
            # Fail OPEN: running unguarded is the pre-#397 behavior, while
            # failing closed would leave this service with no scheduler. The
            # poll retries the lock (_check_lock_file), so a transient error
            # does not leave the guard off for the process's life.
            logger.warning(
                "Scheduler lock unavailable (%s); running the scheduler "
                "without the one-scheduler-per-data-dir guard",
                detail,
            )
            self._print_lock_unavailable_notice(detail)
        elif self._standby_holder is not None:
            logger.info(
                "Scheduler takeover: %s released the scheduler for %s; this "
                "process runs it now",
                self._standby_holder,
                self._schedule_lock.path.parent,
            )
        self._standby_holder = None
        self._owns_schedule = True
        return True

    def _release_schedule(self) -> None:
        """Drop the schedule lock: what this process's exit does (test seam).

        Production never calls this. The lock is held until the process exits,
        because stop() leaves in-flight runs going (the pool shuts down
        without waiting) and a standby that took over then would clear their
        markers and fire a still-due TODO a second time.
        """
        with self._recovery_lock:
            self._schedule_lock.release()
            self._owns_schedule = False
            self._standby_holder = None
            self._stood_by = False
            self._startup_recovery_prepared = False

    def _check_lock_file(self) -> None:
        """An owner re-checks, each poll, that ``scheduler.lock`` is still the
        file it locked: deleted or replaced, another process could lock the
        new one and run a second scheduler. It re-locks the new file when it
        can and says so once when it cannot. An owner running unguarded (the
        lock failed open) retries the lock instead."""
        if not self._schedule_lock.held:
            self._retry_unguarded_lock()
            return
        outcome = self._schedule_lock.relock_if_replaced()
        if outcome is None or outcome is self._lock_file_alarm:
            return
        self._lock_file_alarm = outcome
        if outcome is LockOutcome.ACQUIRED:
            logger.warning(
                "%s was deleted or replaced while this process ran the "
                "scheduler; it is locked again",
                self._schedule_lock.path,
            )
            self._lock_file_alarm = None
        elif outcome is LockOutcome.HELD_ELSEWHERE:
            logger.error(
                "%s was replaced and %s now holds the new file: two processes "
                "may be running this data dir's scheduler. Restart one of them.",
                self._schedule_lock.path,
                self._schedule_lock.holder(),
            )
        else:
            logger.warning(
                "%s was deleted or replaced and cannot be locked again; this "
                "process keeps running the scheduler unguarded",
                self._schedule_lock.path,
            )

    def _retry_unguarded_lock(self) -> None:
        """Retry the lock of an owner that failed open. Getting it turns the
        guard on; finding it held means a second process started a scheduler
        while this one ran unguarded, which is said once, as an ERROR. A lock
        that stays unavailable stays quiet: the claim already warned."""
        outcome, detail = self._schedule_lock.acquire()
        if outcome is LockOutcome.ACQUIRED:
            logger.info(
                "Scheduler lock %s acquired: the one-scheduler-per-data-dir "
                "guard is on again",
                self._schedule_lock.path,
            )
            self._lock_file_alarm = None
            return
        if outcome is self._lock_file_alarm:
            return
        self._lock_file_alarm = outcome
        if outcome is LockOutcome.HELD_ELSEWHERE:
            logger.error(
                "%s holds the scheduler lock for %s while this process runs "
                "the scheduler unguarded: two processes may be running this "
                "data dir's scheduler. Restart one of them.",
                detail,
                self._schedule_lock.path.parent,
            )

    @property
    def owns_schedule(self) -> bool:
        """Whether this ticker runs the schedule (False while on standby)."""
        return self._owns_schedule

    @staticmethod
    def _print_standby_notice(holder: str, takes_over: bool) -> None:
        after = (
            "while it does, and takes over when it exits"
            if takes_over
            else "in this session; restart the session after that process "
            "exits to run them here"
        )
        try:
            _console.print()
            _console.print(
                Panel(
                    "[yellow]Another Nymeria process runs the scheduler for "
                    f"this data directory: {escape(holder)}. This process "
                    f"runs no scheduled TODOs, trigger polls or sweeps {after}."
                    "[/yellow]",
                    title="[bold]Scheduler On Standby[/bold]",
                    border_style="yellow",
                )
            )
            _console.print()
        except Exception:
            logger.debug("Console render failed for scheduler standby notice")

    @staticmethod
    def _print_lock_unavailable_notice(detail: str) -> None:
        # The fat CLI silences the nymeria logger, so the WARNING alone would
        # never reach the one user this case is most likely to hit.
        try:
            _console.print()
            _console.print(
                Panel(
                    "[yellow]The scheduler lock for this data directory is "
                    f"unavailable ({escape(detail)}). This process runs the "
                    "scheduler anyway; make sure no other Nymeria process uses "
                    "this data directory.[/yellow]",
                    title="[bold]Scheduler Lock Unavailable[/bold]",
                    border_style="yellow",
                )
            )
            _console.print()
        except Exception:
            logger.debug("Console render failed for scheduler lock notice")

    @staticmethod
    def _print_recovery_notice(count: int, detail: str) -> None:
        try:
            _console.print()
            _console.print(
                Panel(
                    f"[yellow]Found {count} scheduled TODO(s) from before shutdown; "
                    f"{detail}.[/yellow]",
                    title="[bold]Scheduler Recovery[/bold]",
                    border_style="yellow",
                )
            )
            _console.print()
        except Exception:
            logger.debug("Console render failed for scheduler recovery notice")

    def start(self) -> None:
        """Start the ticker thread."""
        if self._running:
            logger.warning("Ticker already running")
            return

        if not self._startup_recovery_prepared:
            self.prepare_startup_recovery()

        self._running = True

        # Initialize thread pool for parallel autonomous execution
        max_workers = self.settings.max_concurrent_autonomous or None  # 0 = None = unlimited
        self._pool_size = max_workers or 0
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="NymeriaTicker"
        )
        self._housekeeping_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="NymeriaTickerHousekeeping"
        )

        self._thread = threading.Thread(
            target=self._poll_loop,
            name="NymeriaTicker",
            daemon=True,
        )
        self._thread.start()
        current_time = time.time()
        logger.info(
            f"Ticker started with {self.poll_interval}s poll interval, "
            f"max_workers={max_workers}. Current timestamp: {current_time} "
            f"({datetime.fromtimestamp(current_time)})"
        )

        # Register cleanup on exit
        atexit.register(self.stop)

    def stop(self) -> None:
        """Stop the ticker thread gracefully.

        The schedule lock stays held until the process exits (#397): runs in
        flight outlive this call, and releasing now would let a standby clear
        their markers and fire a still-due TODO again.
        """
        if not self._running:
            return

        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._housekeeping_executor:
            self._housekeeping_executor.shutdown(wait=False, cancel_futures=True)
            self._housekeeping_executor = None
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        # A standby ticker never ran the schedule: the shutdown record in the
        # shared state file is the owner's to write.
        with self._recovery_lock:
            owns_schedule = self._owns_schedule
        if owns_schedule:
            self._scheduler_state.record_clean_shutdown()
        logger.info("Ticker stopped")

    def _get_trigger_manager(self) -> TriggerManager:
        """Lazy-init the trigger manager."""
        if self._trigger_manager is None:
            self._trigger_manager = TriggerManager(self.settings.data_dir)
        return self._trigger_manager

    def _poll_loop(self) -> None:
        """Main polling loop.

        Scheduled TODO checks run inline every tick. Trigger polling
        and TODO archival are offloaded to a housekeeping executor so
        slow network I/O or file operations cannot delay the next TODO
        check cycle or consume autonomous worker capacity.
        """
        while self._running:
            self._poll_once()

            # Sleep in small increments to allow fast shutdown
            sleep_increments = int(self.poll_interval * 10)
            for _ in range(sleep_increments):
                if not self._running:
                    break
                time.sleep(0.1)

    def _poll_once(self) -> None:
        """One poll. A standby ticker (#397) only retries the schedule lock."""
        if not self._ensure_schedule_owner():
            return
        self._check_lock_file()
        # Before the due check, so released work runs on this same poll.
        self._honor_release_request()
        try:
            self._check_and_execute()
        except Exception as e:
            logger.error(f"Ticker poll error: {e}", exc_info=True)

        now = time.time()

        self._maybe_submit_archive(now)
        self._maybe_submit_trigger_poll(now)
        self._maybe_submit_spawn_sweep(now)
        self._maybe_submit_dream_sweep(now)
        self._maybe_submit_watchdog_sweep(now)

    def _ensure_schedule_owner(self) -> bool:
        """True once this ticker owns the schedule. A standby that may take
        over retries the claim each poll and, once the owner process has
        exited, runs the recovery it skipped and carries on as the owner."""
        if self._startup_recovery_prepared:
            return True
        if not self._can_take_over:
            return False
        with self._recovery_lock:
            if self._startup_recovery_prepared:
                return True
            # A poll thread that stop() already told to exit claims nothing.
            if not self._running:
                return False
            try:
                self._claim_and_recover_locked()
            except Exception:
                logger.error("Scheduler startup recovery failed", exc_info=True)
            return self._startup_recovery_prepared

    def _honor_release_request(self) -> None:
        """Release held missed work when another process asked (#398).

        Under ``ask`` the hold lives here, in the process that runs the
        scheduler, but an admin asks through the API, which in Docker is a
        different process. It records the request in the data dir; the owner
        honors one newer than its hold. No file I/O unless a hold is active.
        """
        if not (self._pending_startup_missed_ids or self._trigger_catchup_paused):
            return
        try:
            request = read_release_request(self.settings.data_dir)
            since = self._hold_since
            # An unknown hold age refuses rather than releases.
            if request is None or since is None or request.requested_at <= since:
                return
            status = self.release_missed_work()
        except Exception:
            logger.error("Honoring the missed-work release request failed", exc_info=True)
            return
        logger.info(
            "Released %s held missed TODO(s) at the request of %s (requested %s)",
            len(status.get("released_todo_ids") or []),
            request.requested_by,
            request.requested_at.isoformat(timespec="seconds"),
        )

    def _maybe_submit_archive(self, now: float) -> bool:
        """Submit hourly TODO archival without occupying autonomous workers."""
        if now - self._last_archive_check < self._archive_interval:
            return False

        self._last_archive_check = now
        if self._housekeeping_executor:
            try:
                self._housekeeping_executor.submit(self._run_archive)
            except Exception as e:
                logger.debug(f"Archive submit skipped: {e}")
                return False
            return True

        self._run_archive()
        return True

    def _maybe_submit_trigger_poll(self, now: float) -> bool:
        """Submit trigger source polling if due and no previous poll is active."""
        if self._trigger_catchup_paused:
            return False
        if now - self._last_trigger_check < self.trigger_poll_interval:
            return False

        with self._trigger_poll_lock:
            if self._trigger_poll_running:
                return False
            self._trigger_poll_running = True

        try:
            if self._housekeeping_executor:
                self._housekeeping_executor.submit(self._run_trigger_poll)
            else:
                self._run_trigger_poll()
        except Exception as e:
            with self._trigger_poll_lock:
                self._trigger_poll_running = False
            logger.debug(f"Trigger poll submit skipped: {e}")
            return False

        self._last_trigger_check = now
        return True

    def _run_trigger_poll(self) -> None:
        """Execute trigger polling with exception isolation."""
        try:
            self._check_triggers()
        except Exception as e:
            logger.error(f"Trigger poll error: {e}", exc_info=True)
        finally:
            with self._trigger_poll_lock:
                self._trigger_poll_running = False

    def _run_archive(self) -> None:
        """Execute TODO archival with exception isolation."""
        try:
            self._archive_completed_todos()
        except Exception as e:
            logger.error(f"Archive completed TODOs error: {e}", exc_info=True)

    def _maybe_submit_spawn_sweep(self, now: float) -> bool:
        """Submit idle-thread sweep without occupying autonomous workers."""
        if now - self._last_spawn_sweep_check < self._spawn_sweep_interval:
            return False

        self._last_spawn_sweep_check = now
        if self._housekeeping_executor:
            try:
                self._housekeeping_executor.submit(self._run_spawn_sweep)
            except Exception as e:
                logger.debug(f"Spawn sweep submit skipped: {e}")
                return False
            return True

        self._run_spawn_sweep()
        return True

    def _run_spawn_sweep(self) -> None:
        """Execute the idle-thread sweep with exception isolation.

        The Docker worker passes ``spawn_sweeper=None`` because cleanup runs
        API-side via a housekeeping task (the worker has no local agent to
        sweep against). Slim injects a closure over its local agent.
        """
        if self._spawn_sweeper is None:
            return
        try:
            deleted = self._spawn_sweeper()
            if deleted:
                logger.info(
                    f"Spawn idle-sweep deleted {deleted} temporary thread(s)"
                )
        except Exception as e:
            logger.error(f"Spawn idle-sweep error: {e}", exc_info=True)

    def _maybe_submit_dream_sweep(self, now: float) -> bool:
        """Submit the dream sweep off the main loop, like the spawn sweep."""
        if now - self._last_dream_sweep_check < self._dream_sweep_interval:
            return False

        self._last_dream_sweep_check = now
        if self._housekeeping_executor:
            try:
                self._housekeeping_executor.submit(self._run_dream_sweep)
            except Exception as e:
                logger.debug(f"Dream sweep submit skipped: {e}")
                return False
            return True

        self._run_dream_sweep()
        return True

    def _maybe_submit_watchdog_sweep(self, now: float) -> bool:
        """Submit the stale-TODO watchdog sweep if due and not already running.

        Detection (TODO listing + staleness filter) runs on the housekeeping
        executor; the sweep submits each nudge turn to the autonomous worker
        pool itself, so a slow LLM turn never occupies a housekeeping slot.
        """
        if self._watchdog_sweep is None:
            return False
        if now - self._last_watchdog_sweep_check < self._watchdog_sweep_interval:
            return False

        with self._watchdog_sweep_lock:
            if self._watchdog_sweep_running:
                return False
            self._watchdog_sweep_running = True

        try:
            if self._housekeeping_executor:
                self._housekeeping_executor.submit(self._run_watchdog_sweep)
            else:
                self._run_watchdog_sweep()
        except Exception as e:
            with self._watchdog_sweep_lock:
                self._watchdog_sweep_running = False
            logger.debug(f"Watchdog sweep submit skipped: {e}")
            return False

        self._last_watchdog_sweep_check = now
        return True

    def _run_watchdog_sweep(self) -> None:
        """Execute the watchdog sweep with exception isolation."""
        try:
            self._watchdog_sweep.run_cycle(self._executor)  # type: ignore[union-attr]
        except Exception as e:
            logger.error(f"Watchdog sweep error: {e}", exc_info=True)
        finally:
            with self._watchdog_sweep_lock:
                self._watchdog_sweep_running = False

    def watchdog_stats(self) -> dict[str, Any]:
        """Watchdog sweep counters for the worker heartbeat (or disabled)."""
        if self._watchdog_sweep is None:
            return {"enabled": False}
        return self._watchdog_sweep.stats()

    def _run_dream_sweep(self) -> None:
        """Fire due dreams with exception isolation.

        The Docker worker passes ``dream_sweeper=None`` because dreams are
        scheduled API-side (the worker has no local agent to dream against).
        Slim injects a closure over its local agent.
        """
        if self._dream_sweeper is None:
            return
        try:
            started = self._dream_sweeper()
            if started:
                logger.info(f"Dream sweep started {started} dream(s)")
        except Exception as e:
            logger.error(f"Dream sweep error: {e}", exc_info=True)

    def _archive_completed_todos(self) -> None:
        """Archive completed TODOs older than the configured retention for all users."""
        days_old = getattr(self.settings, "todo_auto_archive_days", 7)
        users = self.todo_manager.get_all_users_with_todos()
        for user_id in users:
            # Isolate per-user failures (e.g. a disk error now surfaced by
            # ``atomic_update``) so one user cannot abort the rest of the sweep.
            try:
                with self.todo_manager.atomic_update(user_id) as todo_list:
                    archived = todo_list.archive_completed(days_old=days_old)
                    if archived > 0:
                        logger.info(
                            f"Archived {archived} completed TODO(s) older than "
                            f"{days_old} day(s) for user {user_id}"
                        )
            except Exception as e:
                logger.error(
                    f"TODO archival failed for user {user_id}: {e}", exc_info=True
                )
                continue

    def _check_triggers(self) -> None:
        """Check all poll-based trigger sources for events and fire actions.

        When a single poll returns multiple events (e.g. 5 new emails),
        they are batched into ONE action call instead of firing N separate
        LLM calls.  The batch is passed as a list to ``fire_action_batch``.
        """
        manager = self._get_trigger_manager()
        users = manager.get_all_users_with_triggers()
        logger.info(f"[TRIGGER POLL] Checking triggers for {len(users)} user(s): {users}")

        for user_id in users:
            # In slim, ``busy_agent`` is the local agent so check_triggers can
            # short-circuit on a busy thread before allocating a worker slot.
            # In Docker, the worker passes None — the API handles contention
            # in agent.astream() via its pending-prompt queue.
            #
            # Isolate per-user failures (e.g. a disk error now surfaced by
            # ``atomic_update``) so one user cannot block the rest of the cycle.
            try:
                fired = manager.check_triggers(user_id, agent=self._busy_agent)
                logger.info(f"[TRIGGER POLL] user={user_id}: {len(fired)} trigger(s) fired")
                for trigger, events in fired:
                    logger.info(
                        f"[TRIGGER POLL] Firing trigger '{trigger.name}' ({trigger.id}) "
                        f"with {len(events)} event(s)"
                    )
                    if self._executor:
                        self._executor.submit(
                            manager.fire_action_batch,
                            trigger,
                            events,
                            self._turn_executor,
                            user_id,
                        )
                    else:
                        manager.fire_action_batch(
                            trigger, events, self._turn_executor, user_id
                        )
            except Exception as e:
                logger.error(
                    f"[TRIGGER POLL] Trigger check failed for user {user_id}: {e}",
                    exc_info=True,
                )
                continue

    def _check_and_execute(self) -> None:
        """Check for due scheduled TODOs and execute them."""
        now = time.time()

        # Log ticker poll - less frequently when no due items
        # Get entries from DB (this will also log what's in the DB)
        due_entries = self._filter_list_unavailable(
            self._filter_held_missed(self.schedule_db.get_due(before=now)), now
        )

        # A TODO whose run is still going stays due (its row is re-armed or
        # removed only at the run's end), so it is left out of these per-poll
        # lines; the skip below logs it once per run instead (#395).
        with self._lock:
            startable = [e for e in due_entries if e.todo_id not in self._active_futures]
        if startable:
            logger.info(f"[TICKER POLL] NOW={now} ({datetime.fromtimestamp(now)}) - Found {len(startable)} due TODO(s)!")
            for entry in startable:
                logger.info(f"[TICKER POLL] Due: todo_id={entry.todo_id}, user={entry.user_id}, scheduled_for={entry.scheduled_for} ({datetime.fromtimestamp(entry.scheduled_for)})")
        elif not due_entries:
            # Steady-state heartbeat; debug-only so it does not flood the log.
            if int(now) % 30 < self.poll_interval:
                logger.debug(f"[TICKER POLL] NOW={now} ({datetime.fromtimestamp(now)}) - No due TODOs")

        self._reap_finished_futures()
        self._retry_unreleased_markers()
        self._report_stuck_runs()

        for entry in due_entries:
            # Skip if already running in the pool. The row stays due while
            # its run goes, so this branch repeats every poll: say so once
            # per run (#395), and _report_stuck_runs escalates a run that
            # never ends.
            with self._lock:
                if entry.todo_id in self._active_futures:
                    self._log_held_once(entry.todo_id)
                    continue

            # Submit to thread pool for parallel execution
            if self._executor:
                future = self._executor.submit(self._run_scheduled, entry)
                with self._lock:
                    self._active_futures[entry.todo_id] = future

    def _run_scheduled(self, entry: ScheduledTodoEntry) -> None:
        """Pool entry point: stamp the run's real start (#395), then run it."""
        with self._lock:
            self._active_runs[entry.todo_id] = _RunStamp(
                entry, self._monotonic(), time.time()
            )
        self._execute_scheduled_todo(entry)

    def _log_held_once(self, todo_id: str) -> None:
        """One INFO line per run for a due TODO its own run holds. Call under _lock."""
        run = self._active_runs.get(todo_id)
        key = run.started_mono if run is not None else -1.0
        if self._logged_held_runs.get(todo_id) == key:
            return
        self._logged_held_runs[todo_id] = key
        if run is None:
            logger.info(
                "[TICKER POLL] TODO %s is due but its run is still waiting "
                "for a free pool slot",
                todo_id,
            )
        else:
            logger.info(
                "[TICKER POLL] TODO %s is due but its run started %ds ago is "
                "still going; it waits for that run",
                todo_id,
                int(self._monotonic() - run.started_mono),
            )

    def _filter_held_missed(self, due_entries: list) -> list:
        """Drop startup-missed TODOs held pending an explicit release.

        Returns ``due_entries`` unchanged when nothing is being held.
        """
        if not (self._pending_startup_missed_ids and due_entries):
            return due_entries
        held_entries = [
            entry
            for entry in due_entries
            if entry.todo_id in self._pending_startup_missed_ids
        ]
        if not held_entries:
            return due_entries
        logger.info(
            "[TICKER POLL] Holding %s startup-missed TODO(s) "
            "pending explicit release",
            len(held_entries),
        )
        return [
            entry
            for entry in due_entries
            if entry.todo_id not in self._pending_startup_missed_ids
        ]

    def _filter_list_unavailable(self, due_entries: list, now: float) -> list:
        """Drop due TODOs deferred because their list could not be read."""
        with self._lock:
            if not self._list_unavailable_until:
                return due_entries
            for todo_id, until in list(self._list_unavailable_until.items()):
                if until <= now:
                    del self._list_unavailable_until[todo_id]
            deferred = set(self._list_unavailable_until)
        if not deferred:
            return due_entries
        return [entry for entry in due_entries if entry.todo_id not in deferred]

    def _defer_unavailable_list(self, entry: ScheduledTodoEntry) -> None:
        """A due TODO whose list is not authoritative: defer it, keep its row.

        The list file exists but could not be read or repaired (#394): the
        TODO may be absent from the stand-in list, or present but impossible
        to mark in progress, and neither proves anything about the file.
        Unscheduling it would lose the reminder until a restart; running it
        would need a status write that ``atomic_update`` refuses. So the
        poll loop skips it for ``UNREADABLE_LIST_RETRY_SECONDS`` and the row
        keeps the item's own slot (a late fire is then reported by #262's
        skip accounting as usual). The owner is alerted once per episode.
        """
        retry_at = time.time() + UNREADABLE_LIST_RETRY_SECONDS
        with self._lock:
            self._list_unavailable_until[entry.todo_id] = retry_at
            first = entry.user_id not in self._list_unavailable_alerted
            self._list_unavailable_alerted.add(entry.user_id)
        logger.error(
            "TODO list for %s could not be read or repaired; deferring due "
            "TODO %s until %s",
            entry.user_id,
            entry.todo_id,
            _format_slot(retry_at),
        )
        if not first:
            return
        task = (entry.task_preview or "").strip()[:80]
        try:
            send_owner_alert(
                (
                    f"[TODO LIST UNREADABLE] Your TODO list file exists but "
                    f"could not be read or repaired (check its permissions and "
                    f"the server log), so scheduled TODO [{entry.todo_id}] "
                    f"\"{task}\" was postponed. It is retried every "
                    f"{UNREADABLE_LIST_RETRY_SECONDS // 60} minutes until the "
                    f"file can be read; nothing in it was changed."
                ),
                self.settings,
                user_id=entry.user_id,
                thread_id=entry.thread_id or "",
                task_id=entry.todo_id,
            )
        except Exception:  # noqa: BLE001 - the deferral itself must stand
            logger.warning("Unreadable-list alert failed", exc_info=True)

    def _reap_finished_futures(self) -> None:
        """Pop completed execution futures and log any that raised."""
        with self._lock:
            done_ids = [tid for tid, f in self._active_futures.items() if f.done()]
            for tid in done_ids:
                f = self._active_futures.pop(tid)
                run = self._active_runs.pop(tid, None)
                self._logged_held_runs.pop(tid, None)
                reported = self._reported_stuck_runs.pop(tid, None)
                if run is not None and reported == run.started_mono:
                    logger.info(
                        "Scheduled TODO %s: the run reported as still running "
                        "finished after %d min",
                        tid,
                        int((self._monotonic() - run.started_mono) // 60),
                    )
                exc = f.exception()
                if exc:
                    logger.error(f"Task {tid} failed in thread pool: {exc}")

    # ------------------------------------------------------------------
    # Long runs (#395): nothing times a scheduled run out (the only
    # cancellation seam is cooperative, and the Docker relay gets a keepalive
    # every 25 s, so no read timeout ever trips), and a run whose future is
    # still registered holds its TODO: the poll never resubmits it. A wedged
    # run therefore used to hold a one-shot reminder for the life of the
    # process with no signal at all. It is now reported once per run.
    # ------------------------------------------------------------------

    def _report_stuck_runs(self) -> None:
        """Report each STARTED run still going past the threshold, once.

        Only the bookkeeping runs here, under the lock; each report (a
        network send per notification destination) goes to the housekeeping
        pool so a slow destination cannot delay the next TODO check. Never
        raises.
        """
        if not self._stuck_run_alert_seconds:
            return
        try:
            now_mono = self._monotonic()
            with self._lock:
                stuck: list[_RunStamp] = []
                running = 0
                for tid, run in self._active_runs.items():
                    future = self._active_futures.get(tid)
                    if future is None or future.done():
                        continue
                    running += 1
                    if now_mono - run.started_mono < self._stuck_run_alert_seconds:
                        continue
                    if self._reported_stuck_runs.get(tid) == run.started_mono:
                        continue
                    self._reported_stuck_runs[tid] = run.started_mono
                    stuck.append(run)
        except Exception:  # noqa: BLE001 - visibility must never break a poll
            logger.error("Long-run check failed", exc_info=True)
            return
        for run in stuck:
            try:
                if self._housekeeping_executor is not None:
                    self._housekeeping_executor.submit(
                        self._report_stuck_run, run, now_mono, running
                    )
                else:
                    self._report_stuck_run(run, now_mono, running)
            except Exception:  # noqa: BLE001 - one report must not drop the rest
                logger.error(
                    "Long-run report failed for TODO %s",
                    run.entry.todo_id,
                    exc_info=True,
                )

    def _report_stuck_run(self, run: _RunStamp, now_mono: float, running: int) -> None:
        """One WARNING, one ``task_skipped`` row and one owner alert."""
        entry = run.entry
        minutes = int((now_mono - run.started_mono) // 60)
        task = (entry.task_preview or "")[:80]
        since = _format_slot(run.started_wall)
        where = f"thread {entry.thread_id}" if entry.thread_id else "its thread"
        pool = (
            f"{running} scheduled run(s) running in a pool of {self._pool_size} "
            f"shared with trigger fires and watchdog nudges"
            if self._pool_size
            else f"{running} scheduled run(s) running"
        )
        logger.warning(
            "Scheduled TODO %s has been running for %d min (started %s) and "
            "holds its TODO until it ends; %s",
            entry.todo_id,
            minutes,
            since,
            pool,
        )
        # Alert and row are isolated from each other, as in #262: a failing
        # row write must not swallow the alert, which is the durable signal.
        try:
            send_owner_alert(
                # Verdict and remedy first: the in-app row keeps only
                # NOTIFICATION_SUMMARY_MAX_CHARS (#406), so the task text
                # goes last. "May": a long run is not necessarily a stuck
                # one, and a stop is cooperative (a single stalled read
                # never sees it).
                (
                    f"[SCHEDULED TASK STILL RUNNING] TODO [{entry.todo_id}] has "
                    f"been running {minutes} min and will not run again until "
                    f"that run ends. If it is stuck, /stop in {where} may free "
                    f"it; if not, restart Nymeria (a due one-shot then runs "
                    f"again). Task: \"{task}\", started {since}."
                ),
                self.settings,
                user_id=entry.user_id,
                thread_id=entry.thread_id or "",
                task_id=entry.todo_id,
            )
        except Exception:
            logger.error(
                "Long-run alert dispatch failed for TODO %s",
                entry.todo_id,
                exc_info=True,
            )
        try:
            log_activity(
                ActivityType.TASK_SKIPPED,
                (
                    f"Still running after {minutes} min: {task} (started "
                    f"{since}); this TODO waits for that run"
                ),
                user_id=entry.user_id,
                thread_id=entry.thread_id,
                metadata={
                    "todo_id": entry.todo_id,
                    "reason": "run_still_running",
                    "run_started_at": datetime.fromtimestamp(
                        run.started_wall, timezone.utc
                    ).isoformat(),
                    "running_minutes": minutes,
                    "scheduled_runs_running": running,
                    "pool_size": self._pool_size or None,
                },
            )
        except Exception:
            logger.error(
                "Failed to record long run for TODO %s",
                entry.todo_id,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Execution markers (#262): claim, release, and make a blocked run
    # visible. The marker row in todo_schedule.db is the cross-process
    # "this TODO is running" lock; a refused claim used to be one INFO line
    # repeated every poll until the stale sweep reclaimed the marker.
    # ------------------------------------------------------------------

    def _claim_execution_marker(self, entry: ScheduledTodoEntry) -> bool:
        """Claim the TODO's execution marker; report a refusal that blocks it."""
        if self.schedule_db.mark_execution_started(
            entry.todo_id,
            entry.user_id,
            entry.thread_id,
            stale_after_seconds=self._active_execution_stale_seconds,
        ):
            with self._lock:
                self._reported_blocking_markers.pop(entry.todo_id, None)
            return True
        try:
            self._report_refused_claim(entry)
        except Exception:
            logger.error(
                "Failed to report refused execution claim for TODO %s",
                entry.todo_id,
                exc_info=True,
            )
        return False

    def _report_refused_claim(self, entry: ScheduledTodoEntry) -> None:
        """Say why a due TODO did not start, loudly only when it is blocked.

        ``_check_and_execute`` never submits a TODO that still has a future in
        this pool, so a refusal means the marker has no live run HERE. Its
        start time separates the cases, exactly under the one-ticker
        invariant and approximately beyond it: a marker claimed at or after
        this occurrence's due slot belongs to this occurrence (another
        scheduler process running it, or a retry attempt's own leftover),
        so it stays a log line; one claimed BEFORE the slot was left by an
        earlier run and holds a new occurrence hostage until it is released.
        Only that case writes a row and alerts the owner, once per marker
        (the branch repeats on every poll). A second scheduler process on
        the same database is refused by the schedule lock (#397); only where
        that lock is unavailable (it fails open) could a live run whose turn
        re-armed its own TODO into a slot that comes due mid-run also read
        as blocked.
        """
        started_at = self.schedule_db.get_execution_started_at(entry.todo_id)
        if started_at is None:
            logger.warning(
                "Scheduled TODO %s could not claim its execution marker and "
                "none is present (database error?); retrying next poll",
                entry.todo_id,
            )
            return
        if started_at >= entry.scheduled_for:
            logger.info(
                "Scheduled TODO %s is already executing this occurrence "
                "elsewhere, skipping duplicate run",
                entry.todo_id,
            )
            return
        with self._lock:
            if self._reported_blocking_markers.get(entry.todo_id) == started_at:
                logger.debug(
                    "Scheduled TODO %s still blocked by its reported marker",
                    entry.todo_id,
                )
                return
            self._reported_blocking_markers[entry.todo_id] = started_at
            release_pending = entry.todo_id in self._unreleased_markers
        reclaim_epoch = started_at + self._active_execution_stale_seconds
        started = _format_slot(started_at)
        reclaim = _format_slot(reclaim_epoch)
        due = _format_slot(entry.scheduled_for)
        task = (entry.task_preview or "")[:80]
        if release_pending:
            # Our own failed release: the poll loop retries it every poll,
            # so the reclaim time is only the worst case.
            cause = (
                f"this scheduler could not release the marker from the run "
                f"that started {started} and retries every poll; if the "
                f"database keeps failing it is reclaimed at {reclaim}"
            )
            verdict = (
                f"its run marker is not released yet and the scheduler "
                f"retries every poll; if the database keeps failing it is "
                f"reclaimed at {reclaim}"
            )
        else:
            cause = (
                f"an execution marker left by the run that started {started} "
                f"was never released. The scheduler reclaims it "
                f"automatically at {reclaim}; restarting the scheduler "
                f"clears it now"
            )
            verdict = (
                f"a stale run marker holds it; restarting the scheduler "
                f"clears it now, or it is reclaimed at {reclaim}"
            )
        logger.warning(
            "Scheduled TODO %s (due %s) is blocked by an execution marker "
            "from a run started %s (release retry pending: %s); reclaimed "
            "at %s at the latest",
            entry.todo_id,
            due,
            started,
            release_pending,
            reclaim,
        )
        # Alert and row are isolated from each other: a failing row write
        # must not swallow the alert, which is the durable signal.
        try:
            send_owner_alert(
                # Verdict and remedy first, slots and task last (#406).
                (
                    f"[SCHEDULED TASK BLOCKED] TODO [{entry.todo_id}] cannot "
                    f"start: {verdict}. Task: \"{task}\", due {due}. The "
                    f"marker is from the run that started {started}."
                ),
                self.settings,
                user_id=entry.user_id,
                thread_id=entry.thread_id or "",
                task_id=entry.todo_id,
            )
        except Exception:
            logger.error(
                "Blocked-run alert dispatch failed for TODO %s",
                entry.todo_id,
                exc_info=True,
            )
        try:
            log_activity(
                ActivityType.TASK_SKIPPED,
                f"Blocked from starting: {task} (due {due}; {cause})",
                user_id=entry.user_id,
                thread_id=entry.thread_id,
                metadata={
                    "todo_id": entry.todo_id,
                    "reason": "execution_marker_held",
                    "due_slot": datetime.fromtimestamp(
                        entry.scheduled_for, timezone.utc
                    ).isoformat(),
                    "marker_started_at": datetime.fromtimestamp(
                        started_at, timezone.utc
                    ).isoformat(),
                    "reclaim_at": datetime.fromtimestamp(
                        reclaim_epoch, timezone.utc
                    ).isoformat(),
                    "release_retry_pending": release_pending,
                },
            )
        except Exception:
            logger.error(
                "Failed to record blocked run for TODO %s",
                entry.todo_id,
                exc_info=True,
            )

    def _release_execution_marker(self, todo_id: str, user_id: str) -> None:
        """Release a run's marker, queueing a retry if the DELETE fails.

        ``clear_execution`` swallows its sqlite error (locked past the
        timeout, disk I/O) and returns False; before #262 nothing checked, so
        one failed DELETE blocked the TODO until the stale sweep, up to a day.
        """
        if self.schedule_db.clear_execution(todo_id, user_id):
            return
        logger.warning(
            "Failed to release the execution marker for TODO %s; retrying "
            "on the next poll",
            todo_id,
        )
        with self._lock:
            self._unreleased_markers[todo_id] = user_id

    def _retry_unreleased_markers(self) -> None:
        """Retry marker releases that failed at run end.

        Runs on the poll thread after ``_reap_finished_futures`` and before
        any new submission, so a TODO with no registered future has no live
        run in this process and its marker is the leftover. While a future
        is still registered (the failing run's own last moments, or a new
        run) the retry waits: releasing then could delete a live run's
        marker.
        """
        with self._lock:
            pending = [
                (todo_id, user_id)
                for todo_id, user_id in self._unreleased_markers.items()
                if todo_id not in self._active_futures
            ]
        for todo_id, user_id in pending:
            if self.schedule_db.clear_execution(todo_id, user_id):
                with self._lock:
                    self._unreleased_markers.pop(todo_id, None)
                logger.info(
                    "Released the execution marker for TODO %s on retry", todo_id
                )

    def _should_create_autonomous_notification(self, thread_id: str) -> bool:
        return should_notify_autonomous(thread_id, self.thread_config_manager)

    def _execute_scheduled_todo(self, entry: ScheduledTodoEntry) -> None:
        """Execute a scheduled TODO via the agent stream.

        Orchestrates pre-flight validation, streaming (with optional
        continuation on iteration limit), and finalization.  Delegates
        streaming, success, and error paths to focused helpers.
        """
        if not self._claim_execution_marker(entry):
            return

        todo_list, authoritative = self.todo_manager.load_todos(entry.user_id)
        if not authoritative:
            self._defer_unavailable_list(entry)
            self._release_execution_marker(entry.todo_id, entry.user_id)
            return
        with self._lock:
            self._list_unavailable_alerted.discard(entry.user_id)
        todo = todo_list.get_item(entry.todo_id)
        if not todo:
            logger.warning(f"Scheduled TODO {entry.todo_id} not found, removing from schedule")
            self.schedule_db.remove_scheduled(entry.todo_id)
            self._release_execution_marker(entry.todo_id, entry.user_id)
            return

        if not todo.is_active():
            logger.info(f"Scheduled TODO {entry.todo_id} is no longer active, removing from schedule")
            self.schedule_db.remove_scheduled(entry.todo_id)
            self._release_execution_marker(entry.todo_id, entry.user_id)
            return

        thread_id = entry.thread_id or todo.thread_id or f"todo-{todo.id}"
        logger.info(f"Executing scheduled TODO {todo.id} for user {entry.user_id}: {todo.task[:50]}...")

        try:
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                todo_list.update_item(todo.id, status=TodoStatus.IN_PROGRESS)
        except Exception:
            self._release_execution_marker(todo.id, entry.user_id)
            raise

        log_activity(
            ActivityType.SELF_INVOKE,
            f"Scheduled TODO started: {todo.task[:100]}",
            user_id=entry.user_id,
            thread_id=thread_id,
            metadata={"todo_id": todo.id},
        )

        prompt = f"Work on TODO {todo.id}: {todo.task}"
        if todo.notes:
            prompt += f"\n\nNotes: {todo.notes}"

        try:
            self._print_wakeup_banner(todo.task)
            logger.info(f"[TICKER] === START === TODO {todo.id}, thread={thread_id}, user={entry.user_id}")

            if todo.workflow_id:
                # A workflow TODO runs the published workflow headlessly (no
                # agent turn); recurrence and retries below apply unchanged.
                self._execute_workflow_todo(entry, todo, thread_id)
                return

            logger.debug(f"[TICKER] Prompt: {prompt[:200]}...")

            stream_result, renderer, completed_early = self._stream_todo_execution(
                entry, todo, thread_id, prompt,
            )

            if not completed_early:
                self._finalize_successful_execution(
                    entry, todo, thread_id, stream_result, renderer,
                )

        except Exception as e:
            self._handle_execution_failure(entry, todo, thread_id, e)

        finally:
            # Single execution-marker cleanup for every path that reaches here
            # (success, finalize, the double-iteration-limit backoff, and a
            # failure handler that itself raises, e.g. a disk-save error now
            # surfaced by ``atomic_update``). The early-return guards above
            # (not-found, inactive, status-update failure) clear their own
            # marker before returning. A failed release is retried by the
            # poll loop (#262).
            self._release_execution_marker(todo.id, entry.user_id)

    # ------------------------------------------------------------------
    # Helpers for _execute_scheduled_todo
    # ------------------------------------------------------------------

    def _execute_workflow_todo(
        self, entry: ScheduledTodoEntry, todo, thread_id: str
    ) -> None:
        """Run a workflow TODO headlessly through the executor seam.

        The run always happens in the API process (slim: the local executor
        calls the engine directly; the Docker worker's executor relays to
        ``POST /workflows/{id}/execute``). Output is never auto-delivered:
        delivery is the workflow's own explicit job (nym.thread/nym.notify
        with explicit targets); the owner gets only the usual scheduled-task
        status notification. A ``needs_approval`` outcome is a success (the
        run suspended awaiting the owner; the approve verb announced it).
        Error envelopes raise so the standard retry/backoff machinery in
        ``_handle_execution_failure`` engages.
        """
        import asyncio

        params = dict(todo.workflow_params or {})
        logger.info(
            f"[TICKER] Running scheduled workflow {todo.workflow_id} "
            f"for TODO {todo.id}"
        )
        publish_autonomous_event(
            event_type="task_started",
            thread_id=thread_id,
            user_id=entry.user_id,
            task_id=todo.id,
            data={"todo_id": todo.id, "workflow_id": todo.workflow_id},
        )
        envelope = asyncio.run(
            self._turn_executor.run_workflow(
                todo.workflow_id,
                params,
                user_id=entry.user_id,
                thread_id=thread_id,
            )
        )
        status = str(envelope.get("status") or "")
        if not envelope.get("ok") and status != "needs_approval":
            error = envelope.get("error") or {}
            raise RuntimeError(
                f"workflow {todo.workflow_id} {status or 'error'} "
                f"({error.get('kind', 'unknown')}): {error.get('message', '')}"
            )

        schedule_rewritten = self._handle_recurrence(entry, todo)
        self._reset_failure_streak(entry.user_id, todo.id)
        current = self.todo_manager.get_todo_by_id(entry.user_id, todo.id)
        if current and not current.recurrence and not schedule_rewritten:
            # The agent path leaves closing the TODO to the agent turn; a
            # workflow TODO has no agent turn, so close it here (recurring
            # ones were just reset to PENDING by _handle_recurrence). A
            # mid-run schedule rewrite means the run armed its own next
            # wake; closing would park that live wake behind a done status,
            # so the honored rewrite skips the close too.
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                todo_list.update_item(todo.id, status=TodoStatus.DONE)
        if todo.id in self._retry_counts:
            del self._retry_counts[todo.id]

        summary = f"Scheduled workflow '{todo.workflow_id}' " + (
            "suspended awaiting your approval"
            if status == "needs_approval"
            else "finished ok"
        )
        should_notify = self._should_create_autonomous_notification(thread_id)
        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=entry.user_id,
            task_id=todo.id,
            data={
                "notify": should_notify,
                "content": summary,
                "todo_id": todo.id,
                "workflow_id": todo.workflow_id,
            },
        )
        log_activity(
            ActivityType.TASK_COMPLETED,
            summary,
            user_id=entry.user_id,
            thread_id=thread_id,
            metadata={"todo_id": todo.id, "workflow_id": todo.workflow_id},
        )
        if should_notify:
            create_autonomous_notification(
                user_id=entry.user_id,
                thread_id=thread_id,
                task_id=todo.id,
                summary=summary,
                settings=self.settings,
                thread_config_manager=self.thread_config_manager,
            )
        logger.info(
            f"TODO {todo.id} workflow execution completed ({status or 'ok'})"
        )
        self._report_fire_skips(entry, todo, thread_id)

    @staticmethod
    def _print_wakeup_banner(task_text: str) -> None:
        try:
            sanitized_task = _sanitize_unicode(task_text)
            _console.print()
            _console.print(
                Panel(
                    f"[italic]{sanitized_task}[/italic]",
                    title="[bold yellow]Wake up Nymeria, you have work to do[/bold yellow]",
                    border_style="yellow",
                )
            )
        except Exception as console_err:
            logger.warning(f"Console print failed (non-fatal): {console_err}")

    def _stream_todo_execution(
        self,
        entry: ScheduledTodoEntry,
        todo,
        thread_id: str,
        prompt: str,
    ) -> tuple[StreamCollection, _TodoConsoleRenderer, bool]:
        """Run the initial agent stream and optional continuation pass.

        Returns ``(stream_result, renderer, completed_early)`` where
        ``completed_early`` is ``True`` when a double iteration limit
        triggered backoff and the caller should skip normal finalization.
        """
        renderer = _TodoConsoleRenderer()
        started_published = False

        def on_chunk(chunk: dict, collection: StreamCollection) -> None:
            nonlocal started_published
            # Hold task_started until astream actually owns the thread
            # lock AND turn work has begun (the first non-queue-meta
            # chunk; since the llm_call_started status event this is LLM
            # dispatch, pre-first-token). Queue-meta events (queued,
            # prompt_queued, prompt_injected, prompt_absorbed,
            # turn_halted, fanout_dropped) signal queue transitions, not
            # the start of model work; firing task_started on them
            # would flip the frontend into autonomous-streaming mode
            # before the worker actually has a response.
            if (
                not started_published
                and chunk.get("type") not in PENDING_QUEUE_META_EVENT_TYPES
            ):
                publish_autonomous_event(
                    event_type="task_started",
                    thread_id=thread_id,
                    user_id=entry.user_id,
                    task_id=todo.id,
                    # A marked first chunk means this relay became a queuer
                    # and is mirroring the holder's turn (stream_bridge
                    # fanout marker); stamp the lifecycle so consumers can
                    # skip the mirror task entirely.
                    data={
                        "prompt": prompt,
                        "todo_id": todo.id,
                        **({"fanout": True} if chunk.get("fanout") else {}),
                    },
                )
                started_published = True

            self._log_stream_chunk(todo.id, chunk, collection)
            publish_agent_stream_chunk(
                chunk,
                thread_id=thread_id,
                user_id=entry.user_id,
                task_id=todo.id,
            )
            renderer.render_chunk(chunk)

        stream_result = stream_and_collect(
            self._turn_executor,
            astream_kwargs={
                "message": prompt,
                "thread_id": thread_id,
                "user_id": entry.user_id,
                "_is_self_invoke": True,
                "source": "ticker",
                "source_id": todo.id,
                "source_label": (todo.task or "scheduled task")[:80],
            },
            on_chunk=on_chunk,
            error_message_factory=_stream_error_message,
            should_mark_iteration_limit=_should_continue_after_limit,
        )

        if stream_result.iteration_limit_hit:
            double_limit = self._run_continuation_pass(
                entry, todo, thread_id, stream_result, renderer,
            )
            if double_limit:
                return stream_result, renderer, True

        return stream_result, renderer, False

    def _run_continuation_pass(
        self,
        entry: ScheduledTodoEntry,
        todo,
        thread_id: str,
        stream_result: StreamCollection,
        renderer: _TodoConsoleRenderer,
    ) -> bool:
        """Run one continuation pass after an iteration limit.

        Merges the continuation result into ``stream_result``.  Returns
        ``True`` if the continuation also hit the iteration limit (the
        caller should return early after double-limit backoff).
        """
        continuation_prompt = (
            f"Continue working on the scheduled task: {todo.task}. "
            f"If you have already completed everything, please confirm "
            f"the results."
        )
        logger.info(
            f"[TICKER] Sending continuation prompt for TODO {todo.id} "
            f"after iteration_limit"
        )

        def on_continuation_chunk(chunk: dict, _collection: StreamCollection) -> None:
            publish_agent_stream_chunk(
                chunk,
                thread_id=thread_id,
                user_id=entry.user_id,
                task_id=todo.id,
            )
            renderer.render_chunk(chunk)
            self._log_continuation_chunk(todo.id, chunk)

        continuation_result = stream_and_collect(
            self._turn_executor,
            astream_kwargs={
                "message": continuation_prompt,
                "thread_id": thread_id,
                "user_id": entry.user_id,
                "_is_self_invoke": True,
                "source": "ticker",
                "source_id": todo.id,
                "source_label": (todo.task or "scheduled task")[:80],
            },
            on_chunk=on_continuation_chunk,
            error_message_factory=_stream_error_message,
        )
        stream_result.response_parts.extend(continuation_result.response_parts)
        stream_result.thinking_parts.extend(continuation_result.thinking_parts)
        stream_result.chunk_count += continuation_result.chunk_count
        stream_result.fanout_observed = (
            stream_result.fanout_observed or continuation_result.fanout_observed
        )

        if not continuation_result.iteration_limit_hit:
            return False

        self._backoff_after_double_limit(entry, todo, thread_id)
        return True

    def _backoff_after_double_limit(
        self,
        entry: ScheduledTodoEntry,
        todo,
        thread_id: str,
    ) -> None:
        ticker_chose_wake = False
        current_todo = self.todo_manager.get_todo_by_id(entry.user_id, todo.id)
        if current_todo is not None and self._schedule_rewritten_mid_run(
            current_todo, entry
        ):
            # Same contract as _handle_recurrence's honored branch: a
            # schedule write made during the run (the agent's own re-arm, a
            # REST/GUI edit) wins over the automatic +10min backoff, and the
            # notes it may have set stay untouched. No last_execution stamp
            # here: the occurrence did not complete, matching the normal
            # backoff branch. The row re-sync is the same JSON-authority
            # belt as the other honored branch.
            self.todo_manager.sync_schedule_to_db(
                entry.user_id, todo.id, self.schedule_db
            )
            logger.info(
                f"[TICKER] TODO {todo.id} schedule was rewritten during its "
                f"own run; skipping double-iteration-limit backoff"
            )
            content = (
                "Task hit the iteration limit twice; keeping the "
                "wake the run itself scheduled."
            )
        else:
            ticker_chose_wake = True
            backoff_time = datetime.now(timezone.utc) + timedelta(minutes=10)
            banner = format_note_banner(
                BACKOFF_NOTE_PREFIX,
                f"hit iteration limit twice, rescheduled "
                f"for {backoff_time.isoformat()}",
            )
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                item = todo_list.get_item(todo.id)
                existing_notes = item.notes if item else None
                todo_list.update_item(
                    todo.id,
                    scheduled_for=backoff_time,
                    notes=prepend_note_banner(existing_notes, banner),
                )
            self.todo_manager.sync_schedule_to_db(
                entry.user_id, todo.id, self.schedule_db
            )
            logger.info(
                f"[TICKER] TODO {todo.id} rescheduled to "
                f"{backoff_time.isoformat()} after double iteration_limit"
            )
            content = (
                "Task requires more steps than the current "
                "iteration limit allows. Rescheduled for "
                "10 minutes from now."
            )

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=entry.user_id,
            task_id=todo.id,
            data={
                "notify": False,
                "content": content,
                "todo_id": todo.id,
            },
        )
        # A late fire that double-limits still let slots pass, and the
        # ticker's own +10min wake can pass over more on a short interval;
        # the next fire anchors on the wake, so this is the only place to
        # see them. An agent's own re-arm (honored above) stays intent.
        self._report_fire_skips(
            entry, todo, thread_id, through_armed=ticker_chose_wake
        )
        # Execution-marker cleanup happens once at the end of
        # _execute_scheduled_todo, which this path returns through.

    def _finalize_successful_execution(
        self,
        entry: ScheduledTodoEntry,
        todo,
        thread_id: str,
        stream_result: StreamCollection,
        renderer: _TodoConsoleRenderer,
    ) -> None:
        response_text = stream_result.response_text()
        if not stream_result.response_parts and stream_result.thinking_parts:
            logger.info(
                f"[TICKER] No response chunks, using thinking content as "
                f"response ({len(stream_result.thinking_parts)} parts)"
            )
        logger.info(
            f"[TICKER] === STREAM DONE === chunks={stream_result.chunk_count}, "
            f"response_parts={len(stream_result.response_parts)}, "
            f"thinking_parts={len(stream_result.thinking_parts)}, "
            f"response_len={len(response_text)}"
        )
        logger.info(
            f"Raw autonomous response (first 500 chars): "
            f"{response_text[:500] if response_text else 'empty'}"
        )

        self._handle_recurrence(entry, todo)
        self._reset_failure_streak(entry.user_id, todo.id)

        if todo.id in self._retry_counts:
            del self._retry_counts[todo.id]

        should_notify = self._should_create_autonomous_notification(thread_id)
        notification_summary = (
            response_text[:NOTIFICATION_SUMMARY_MAX_CHARS]
            if response_text
            else "Scheduled TODO executed"
        )

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=entry.user_id,
            task_id=todo.id,
            data={
                # Mirror-task marker (see stream_bridge fanout marker): the
                # content below was fanned in from another task's holder
                # turn, so consumers must not deliver it a second time.
                **({"fanout": True} if stream_result.fanout_observed else {}),
                "notify": should_notify,
                "content": response_text,
                "todo_id": todo.id,
            },
        )

        self._index_todo_completion(
            user_id=entry.user_id,
            thread_id=thread_id,
            todo_id=todo.id,
            todo_task=todo.task,
            response_summary=response_text[:200] if response_text else "",
        )

        log_activity(
            ActivityType.TASK_COMPLETED,
            response_text[:200] if response_text else "Scheduled TODO executed",
            user_id=entry.user_id,
            thread_id=thread_id,
            metadata={"todo_id": todo.id, "notify": should_notify},
        )

        renderer.flush_remaining()

        if should_notify and notification_summary:
            create_autonomous_notification(
                user_id=entry.user_id,
                thread_id=thread_id,
                task_id=todo.id,
                summary=notification_summary,
                settings=self.settings,
                thread_config_manager=self.thread_config_manager,
            )
            try:
                _console.print(f"[yellow]Notification sent: {notification_summary}[/yellow]")
            except Exception:
                logger.debug("Console render failed for notification message")

        try:
            _console.print()
        except Exception:
            logger.debug("Console render failed for output spacing")
        logger.info(f"TODO {todo.id} scheduled execution completed, notify={should_notify}")

        self._report_fire_skips(entry, todo, thread_id)

        self._trim_context_if_needed(entry, thread_id)

    @staticmethod
    def _schedule_rewritten_mid_run(
        current_todo, entry: ScheduledTodoEntry
    ) -> bool:
        """True when the TODO's schedule no longer matches the fired slot.

        The ticker never touches ``scheduled_for`` while a run is in flight,
        so at finalize time any difference from the slot that fired,
        including None (a mid-run ``clear_schedule``), proves a deliberate
        write made during the run: the agent's own re-arm, a REST/GUI edit,
        or the done-path advance. The 1s tolerance is defensive only: both
        sides derive from the same stored datetime (the row is written from
        the item and the isoformat/epoch round-trips are exact), so equality
        holds today and the tolerance guards a future lossy path.
        """
        current = current_todo.scheduled_for
        if current is None:
            return True
        return abs(current.timestamp() - entry.scheduled_for) > 1.0

    def _handle_recurrence(self, entry: ScheduledTodoEntry, todo) -> bool:
        """Re-arm or clear a fired TODO's schedule at end of run.

        Returns True when a mid-run schedule write was detected and honored
        instead. A schedule write made DURING the TODO's own fire turn wins
        over the automatic re-arm/clear: before 2026-08-29 this method
        rewrote the schedule unconditionally from its pre-run snapshot,
        silently destroying mid-run re-arms ("remind me again in 35 min") on
        recurring TODOs and clearing the documented self-rescheduling-watcher
        pattern for non-recurring ones. When honoring, ``last_execution`` is
        stamped with the slot that actually ran (the done-anchor invariant,
        ``resolve_done_recurrence_anchor``), the ticker's own fire-start
        IN_PROGRESS returns to PENDING (a lifecycle write, not agent intent;
        any other status the run set is preserved), and the schedule DB row
        is re-synced from the JSON, so a mid-run re-arm to a time already
        past fires on the next poll rather than being lost.
        """
        from .todo_constants import compute_recurrence_reschedule

        current_todo = self.todo_manager.get_todo_by_id(entry.user_id, todo.id)
        if current_todo is not None and self._schedule_rewritten_mid_run(
            current_todo, entry
        ):
            fired_slot = datetime.fromtimestamp(
                entry.scheduled_for, timezone.utc
            )
            logger.info(
                f"TODO {todo.id} schedule was rewritten during its own run "
                f"(fired slot {fired_slot}, now "
                f"{current_todo.scheduled_for}); honoring the mid-run write"
            )
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                item = todo_list.get_item(todo.id)
                if item:
                    item.last_execution = fired_slot
                    if item.status == TodoStatus.IN_PROGRESS:
                        item.status = TodoStatus.PENDING
            self.todo_manager.sync_schedule_to_db(
                entry.user_id, todo.id, self.schedule_db,
            )
            return True
        if current_todo and current_todo.recurrence:
            recurrence_anchor = datetime.fromtimestamp(
                entry.scheduled_for,
                timezone.utc,
            )
            next_execution, origin_to_persist = compute_recurrence_reschedule(
                current_todo.recurrence,
                recurrence_anchor,
                current_todo.recurrence_anchor,
            )
            if next_execution:
                logger.info(
                    f"Rescheduling recurring TODO {todo.id} "
                    f"({current_todo.recurrence}) from anchor "
                    f"{recurrence_anchor} for {next_execution}"
                )
                with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                    todo_list.update_item(
                        todo.id,
                        scheduled_for=next_execution,
                        status=TodoStatus.PENDING,
                    )
                    item = todo_list.get_item(todo.id)
                    if item:
                        item.last_execution = recurrence_anchor
                        if origin_to_persist is not None:
                            item.recurrence_anchor = origin_to_persist
                self.todo_manager.sync_schedule_to_db(
                    entry.user_id, todo.id, self.schedule_db,
                )
            else:
                self.todo_manager.clear_todo_schedule(
                    entry.user_id, todo.id, self.schedule_db,
                )
        else:
            self.todo_manager.clear_todo_schedule(
                entry.user_id, todo.id, self.schedule_db,
            )
        return False

    def _count_fire_skips(
        self,
        entry: ScheduledTodoEntry,
        snapshot,
        armed: Optional[datetime] = None,
        *,
        through_armed: bool = False,
    ) -> int:
        """Cadence slots this fire let pass unrun, from the FIRE-TIME cadence.

        ``snapshot`` is the TODO as read when the run started. Counting from
        it rather than from whatever the run wrote makes the count
        independent of which end-of-run branch set the next slot: the
        ticker's automatic re-arm, a done the fire turn marked (the done
        paths share the same skip-forward), the turn's own re-arm, or the
        double-iteration-limit backoff. It is the slots strictly between the
        fired slot and the first slot of that cadence after now, so an
        on-time run with a deliberate re-arm counts 0 and a recurrence edited
        mid-run cannot fake a skip.

        The count stops at ``armed`` (the slot the run end actually wrote)
        when it falls before that first slot: the report runs at the very
        end of the run, and a slot the re-arm (or a mid-turn done) armed can
        come due while the run is still wrapping up; it is due, not skipped,
        and fires on the next poll. ``through_armed`` extends the count to
        the armed slot even beyond that first slot, for a wake the ticker
        itself chose (the double-iteration-limit backoff's now+10m), whose
        passed-over slots no later fire can see; an agent's own later
        re-arm stays intent and is not counted.
        """
        recurrence = getattr(snapshot, "recurrence", None)
        if not recurrence:
            return 0
        from .todo_constants import (
            compute_recurrence_reschedule,
            count_skipped_occurrences,
        )

        fired = datetime.fromtimestamp(entry.scheduled_for, timezone.utc)
        origin = getattr(snapshot, "recurrence_anchor", None)
        upcoming, _ = compute_recurrence_reschedule(recurrence, fired, origin)
        if upcoming is None:
            return 0
        if armed is not None:
            armed = ensure_aware_utc(armed)
            if fired < armed and (through_armed or armed < upcoming):
                upcoming = armed
        return count_skipped_occurrences(recurrence, fired, upcoming, origin=origin)

    def _report_fire_skips(
        self,
        entry: ScheduledTodoEntry,
        snapshot,
        thread_id: str,
        *,
        through_armed: bool = False,
    ) -> None:
        """Make the occurrences a late fire let pass visible (#262).

        Called by every end-of-run path AFTER it published the run's own
        ``task_completed``, so an alert to a slow destination never delays
        the reminder it is about. The skip-forward itself is intended (a
        recurring TODO resumes its cadence instead of replaying every missed
        slot), but each slot jumped is an occurrence that never ran, and
        before #262 nothing said so: five missed medication reminders
        produced zero signals. Every such fire writes an activity row; the
        owner alert is gated by ``scheduler_skip_alert_after`` and a
        per-TODO cooldown persisted as ``skip_alerted_at``. Each step is
        fault-isolated on its own: reporting can never break the run's end.
        """
        try:
            current = self.todo_manager.get_todo_by_id(entry.user_id, snapshot.id)
            upcoming = current.scheduled_for if current else None
            skipped = self._count_fire_skips(
                entry, snapshot, upcoming, through_armed=through_armed
            )
        except Exception:
            logger.error(
                "Failed to count skipped occurrences for TODO %s",
                entry.todo_id,
                exc_info=True,
            )
            return
        if not skipped:
            return
        todo_id = snapshot.id
        recurrence = snapshot.recurrence
        task = (snapshot.task or "")[:80]
        noun = "occurrence" if skipped == 1 else "occurrences"
        fired_at = datetime.fromtimestamp(entry.scheduled_for, timezone.utc)
        now = datetime.now(timezone.utc)
        next_copy = f"next run {_format_slot(upcoming)}" if upcoming else "no next run is scheduled"
        logger.warning(
            "Recurring TODO %s (%s) let %d occurrence(s) pass: the run due "
            "%s ended at %s",
            todo_id,
            recurrence,
            skipped,
            fired_at.isoformat(),
            now.isoformat(),
        )
        try:
            log_activity(
                ActivityType.TASK_SKIPPED,
                (
                    f"Skipped {skipped} scheduled {noun}: {task} (the run due "
                    f"{_format_slot(fired_at)} ended at {_format_slot(now)}, "
                    f"after later slots had passed; {next_copy})"
                ),
                user_id=entry.user_id,
                thread_id=thread_id or None,
                metadata={
                    "todo_id": todo_id,
                    "reason": "occurrences_collapsed",
                    "skipped_occurrences": skipped,
                    "fired_slot": fired_at.isoformat(),
                    "next_slot": upcoming.isoformat() if upcoming else None,
                    "recurrence": recurrence,
                },
            )
        except Exception:
            logger.error(
                "Failed to record skipped occurrences for TODO %s",
                todo_id,
                exc_info=True,
            )
        try:
            if not self._claim_skip_alert(entry.user_id, todo_id, skipped, now):
                return
        except Exception:
            # The stamp did not persist, so no alert: an alert must never
            # outrun the cooldown state that suppresses its repeats.
            logger.error(
                "Failed to record the skip-alert stamp for TODO %s",
                todo_id,
                exc_info=True,
            )
            return
        try:
            send_owner_alert(
                # Verdict and next slot first, task and timing last (#406).
                (
                    f"[SCHEDULED TASK SKIPPED] Recurring TODO [{todo_id}] "
                    f"skipped {skipped} scheduled {noun}; {next_copy}. Task: "
                    f"\"{task}\". The run due {_format_slot(fired_at)} ended "
                    f"at {_format_slot(now)}, after later slots had already "
                    f"passed. Usual causes: the scheduler was down or busy, "
                    f"or a run took longer than its {recurrence} interval. "
                    f"At most one such alert per TODO per "
                    f"{self._skip_alert_cooldown_label()}."
                ),
                self.settings,
                user_id=entry.user_id,
                thread_id=thread_id,
                task_id=todo_id,
            )
        except Exception:
            logger.error(
                "Skip alert dispatch failed for TODO %s", todo_id, exc_info=True
            )

    def _claim_skip_alert(
        self, user_id: str, todo_id: str, skipped: int, now: datetime
    ) -> bool:
        """Decide and persist whether this skip alerts the owner.

        True only when the policy allows it (``scheduler_skip_alert_after``
        skipped occurrences in this fire, 0 disables) and the TODO's
        ``skip_alerted_at`` is outside the cooldown; the new stamp is saved
        before True is returned, so an unsaved stamp (the save raises) never
        yields an alert. Persisted rather than ticker memory so a restart,
        which deploy-sync does on every push, does not re-alert.
        """
        if not self._skip_alert_after or skipped < self._skip_alert_after:
            return False
        if not self._skip_alert_window_open(
            self.todo_manager.get_todo_by_id(user_id, todo_id), now
        ):
            # Checked before the write lock: atomic_update saves on exit, so
            # deciding inside it would rewrite the file for every suppressed
            # alert. Re-checked inside against a concurrent claim.
            return False
        claimed = False
        with self.todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if self._skip_alert_window_open(item, now):
                assert item is not None
                item.skip_alerted_at = now
                claimed = True
        return claimed

    def _skip_alert_window_open(self, item, now: datetime) -> bool:
        """True when ``item`` exists and its skip-alert cooldown has passed."""
        if item is None:
            return False
        last = item.skip_alerted_at
        return (
            last is None
            or not self._skip_alert_cooldown_seconds
            or (now - last).total_seconds() >= self._skip_alert_cooldown_seconds
        )

    def _skip_alert_cooldown_label(self) -> str:
        minutes = self._skip_alert_cooldown_seconds // 60
        if not minutes:
            return "late run"
        if minutes % 1440 == 0:
            days = minutes // 1440
            return "day" if days == 1 else f"{days} days"
        if minutes % 60 == 0:
            hours = minutes // 60
            return "hour" if hours == 1 else f"{hours} hours"
        return f"{minutes} minutes"

    def _trim_context_if_needed(
        self, entry: ScheduledTodoEntry, thread_id: str,
    ) -> None:
        # Post-turn sliding-window trim. Only available when we have an
        # in-process agent (slim). Docker workers pass busy_agent=None;
        # the API's astream() runs its own start-of-turn trim before
        # each turn, so skipping here just defers cleanup by one cycle.
        if self._busy_agent is None:
            return
        if self.settings.context_management != "sliding_window":
            return
        cycle_count = self._busy_agent.get_context_cycle_count(thread_id)
        max_cycles = self.settings.sliding_window_cycles
        if cycle_count <= max_cycles:
            return
        messages_removed = self._busy_agent.trim_context_window(
            thread_id, max_cycles, user_id=entry.user_id,
        )
        if messages_removed > 0:
            logger.info(
                f"Thread {thread_id}: Sliding window trimmed "
                f"{messages_removed} messages "
                f"(was {cycle_count} cycles, now {max_cycles})"
            )
            try:
                _console.print(
                    f"[dim]Context window trimmed: kept last {max_cycles} cycles[/dim]"
                )
            except Exception:
                logger.debug("Console render failed for context trim message")

    def _handle_execution_failure(
        self,
        entry: ScheduledTodoEntry,
        todo,
        thread_id: str,
        error: Exception,
    ) -> None:
        import traceback

        import httpx

        if (
            isinstance(error, httpx.HTTPStatusError)
            and error.response.status_code == 401
        ):
            logger.error(
                "[TICKER] Service token rejected by API (401) for TODO %s; "
                "the client's refresh-and-retry did not recover. Check "
                "NYMERIA_SERVICE_TOKEN or the shared data/SLIM_SERVICE_TOKEN.txt.",
                todo.id,
            )
        logger.error(f"[TICKER] === ERROR === TODO {todo.id} failed: {error}")
        logger.error(f"[TICKER] Traceback:\n{traceback.format_exc()}")
        try:
            sanitized_error = _sanitize_unicode(str(error))
            _console.print(f"[red]Scheduled TODO failed: {sanitized_error}[/red]")
        except Exception:
            logger.debug("Console render failed for error message")

        # A fanned-in holder error is still a mirror (the latch rides the
        # raised exception because the StreamCollection dies with it);
        # stamp so consumers drop this bookend instead of delivering a
        # second error line.
        error_fanout = bool(getattr(error, "fanout_observed", False))
        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=entry.user_id,
            task_id=todo.id,
            data={
                "error": True,
                "error_message": str(error)[:200],
                "content": f"Task failed: {str(error)[:200]}",
                "todo_id": todo.id,
                **({"fanout": True} if error_fanout else {}),
            },
        )

        if not error_fanout:
            create_autonomous_notification(
                user_id=entry.user_id,
                thread_id=thread_id,
                task_id=todo.id,
                summary=f"Task failed: {str(error)[:180]}",
                settings=self.settings,
                thread_config_manager=self.thread_config_manager,
            )

        retry_count = self._retry_counts.get(todo.id, 0) + 1
        self._retry_counts[todo.id] = retry_count

        if retry_count >= self.MAX_RETRIES:
            del self._retry_counts[todo.id]
            current_todo = self.todo_manager.get_todo_by_id(entry.user_id, todo.id)
            if current_todo and current_todo.recurrence:
                # Recurring TODOs must survive a transient outage: skip this
                # failed occurrence and re-arm at the next recurrence slot (or,
                # only for a recurrence with no further slot, clear the
                # schedule) via _handle_recurrence, instead of clearing the
                # schedule outright, which would leave ``recurrence`` set but
                # ``scheduled_for`` null and silently kill the schedule forever.
                # The next occurrence gets a fresh retry budget since
                # ``_retry_counts`` was just cleared.
                logger.warning(
                    "Recurring TODO %s failed after %s retries; skipping this "
                    "occurrence and re-arming at the next scheduled slot",
                    todo.id,
                    retry_count,
                )
                # Failure policy (#154): ONE streak increment per exhausted
                # occurrence (this branch), never per intra-occurrence retry.
                failure_count = self._record_recurring_failure(
                    entry, current_todo, error
                )
                if (
                    self._failure_pause_after
                    and failure_count >= self._failure_pause_after
                ):
                    # Pause INSTEAD of re-arming; recurrence is kept and the
                    # marker makes the schedule-less shape deliberate.
                    self._pause_recurring_schedule(
                        entry, current_todo, thread_id, error, failure_count
                    )
                else:
                    self._handle_recurrence(entry, current_todo)
                    # ``todo`` is this attempt's pre-run snapshot: the fire
                    # cadence the skip count must use (#262).
                    self._report_fire_skips(entry, todo, thread_id)
                    if (
                        self._failure_alert_after
                        and failure_count == self._failure_alert_after
                    ):
                        # Fires once per episode: only success resets the
                        # streak, so the count crosses the threshold once.
                        self._send_failure_alert(
                            entry, current_todo, thread_id, error, failure_count
                        )
                log_activity(
                    ActivityType.TASK_FAILED,
                    f"Recurring TODO occurrence failed: {todo.task[:80]} - {str(error)[:50]}",
                    user_id=entry.user_id,
                    thread_id=thread_id,
                    metadata={
                        "todo_id": todo.id,
                        "error": str(error),
                        "retries": retry_count,
                        "recurring": True,
                        "consecutive_failures": failure_count,
                    },
                )
                return

            # Non-recurring give-up is a deliberate DEAD STOP and stays
            # OUTSIDE the mid-run-edit-wins guard: honoring a mid-run re-arm
            # here would let a permanently failing self-rescheduling one-shot
            # retry forever with no #154-style pause backstop (the failure
            # streak only exists for recurring TODOs). The clear is loud
            # (per-retry failure notifications plus the banner below), not
            # the silent-death shape the guard exists to prevent.
            self.schedule_db.remove_scheduled(todo.id)
            giveup_banner = format_note_banner(
                GIVEUP_NOTE_PREFIX,
                f"{retry_count} retries: {str(error)[:100]}",
            )
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                item = todo_list.get_item(todo.id)
                existing_notes = item.notes if item else None
                todo_list.update_item(
                    todo.id,
                    status=TodoStatus.PENDING,
                    notes=prepend_note_banner(existing_notes, giveup_banner),
                    clear_schedule=True,
                )
            logger.error(f"TODO {todo.id} failed permanently after {retry_count} retries")

            log_activity(
                ActivityType.TASK_FAILED,
                f"Scheduled TODO failed: {todo.task[:80]} - {str(error)[:50]}",
                user_id=entry.user_id,
                thread_id=thread_id,
                metadata={"todo_id": todo.id, "error": str(error), "retries": retry_count},
            )
        else:
            logger.info(f"TODO {todo.id} will retry (attempt {retry_count + 1}/{self.MAX_RETRIES})")

    # ── Recurring-failure policy (#154) ──────────────────────────────────
    # One streak increment per exhausted occurrence; alert at
    # scheduler_failure_alert_after, auto-pause at
    # scheduler_failure_pause_after, any success resets. Sits ON TOP of the
    # re-arm-on-failure precedent above; every helper is fault-isolated so
    # policy bookkeeping can never break the cycle.

    def _record_recurring_failure(
        self, entry: ScheduledTodoEntry, todo, error: Exception
    ) -> int:
        """Persist one failed occurrence; returns the new streak count.

        Returns 0 when nothing persisted (todo vanished, or the save
        failed): unpersisted state must not drive the alert/pause policy.
        """
        count = 0
        try:
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                item = todo_list.get_item(todo.id)
                if item:
                    count = item.consecutive_failures + 1
                    item.consecutive_failures = count
                    item.last_failure = str(error)[:300]
                    item.last_failure_at = datetime.now(timezone.utc)
        except Exception:
            logger.warning(
                "Failed to record failure streak for TODO %s",
                todo.id,
                exc_info=True,
            )
            # Unpersisted state must not drive policy: with 0 the alert and
            # pause branches both no-op, so an alert can never fire on a
            # streak that is not on disk (and then re-fire next occurrence).
            count = 0
        return count

    def _reset_failure_streak(self, user_id: str, todo_id: str) -> None:
        """A successful occurrence ends the failure episode."""
        try:
            current = self.todo_manager.get_todo_by_id(user_id, todo_id)
            if not current or not (
                current.consecutive_failures
                or current.last_failure
                or current.last_failure_at
                or current.schedule_paused_at
            ):
                return
            if (
                current.schedule_paused_at is not None
                and current.scheduled_for is None
            ):
                # A LIVE pause (marker set, schedule cleared) at
                # success-finalize time can only be a #247 delivery-pause
                # that landed during this very run: a #154 pause of this
                # TODO rides its own failure path, and any earlier pause
                # would have kept it from firing at all. The pause is a
                # deliberate failure-policy write about delivery of prior
                # occurrences, so this run's execution success must not end
                # it; clearing only the marker here would leave the
                # marker-less dead-schedule shape #154 exists to prevent,
                # plus a pause banner the resume-strip could never remove.
                # The episode ends via explicit resume (update_item) or a
                # delivered report, never here.
                return
            with self.todo_manager.atomic_update(user_id) as todo_list:
                item = todo_list.get_item(todo_id)
                if item:
                    item.consecutive_failures = 0
                    item.last_failure = None
                    item.last_failure_at = None
                    item.schedule_paused_at = None
        except Exception:
            logger.warning(
                "Failed to reset failure streak for TODO %s",
                todo_id,
                exc_info=True,
            )

    def _pause_recurring_schedule(
        self,
        entry: ScheduledTodoEntry,
        todo,
        thread_id: str,
        error: Exception,
        failure_count: int,
    ) -> None:
        """Auto-pause: clear the schedule, KEEP recurrence, set the marker.

        Startup recovery cannot resurrect a paused schedule because
        ``rebuild_from_todos`` reads ``scheduled_for``, now None. Resume is
        an explicit reschedule (update_item clears the failure state when
        the pause marker is set).
        """
        try:
            # PREPEND the pause reason: notes are prompt input on every run
            # (and the user's own instructions), so they must survive the
            # pause; update_item's resume-clear strips this prefix back off.
            pause_note = format_note_banner(
                PAUSE_NOTE_PREFIX,
                f"{failure_count} consecutive failed runs: {str(error)[:100]}",
            )
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                item = todo_list.get_item(todo.id)
                existing = (item.notes or "").strip() if item else ""
                todo_list.update_item(
                    todo.id,
                    status=TodoStatus.PENDING,
                    notes=f"{pause_note} {existing}".strip()[:1000],
                    clear_schedule=True,
                )
                item = todo_list.get_item(todo.id)
                if item:
                    item.schedule_paused_at = datetime.now(timezone.utc)
            # Row removal AFTER the marker persists: if the JSON save fails,
            # the TODO is still scheduled (loud, keeps retrying) instead of
            # orphaned with neither row nor marker.
            self.schedule_db.remove_scheduled(todo.id)
            logger.warning(
                "Recurring TODO %s auto-paused after %d consecutive failures",
                todo.id,
                failure_count,
            )
        except Exception:
            logger.error(
                "Failed to auto-pause recurring TODO %s", todo.id, exc_info=True
            )
            return
        try:
            # send_owner_alert's own contract is never-raise; this belt
            # keeps a broken alert plane from escaping the failure handler
            # (which runs inside _execute_scheduled_todo's except block,
            # where a raise would skip the execution-marker note path).
            send_owner_alert(
                # Verdict and remedy first, task and error last (#406).
                (
                    f"[SCHEDULED TASK PAUSED] Recurring TODO [{todo.id}] was "
                    f"auto-paused after {failure_count} consecutive failed "
                    f"runs. To resume, reschedule it: /todos schedule "
                    f"{todo.id} ... (its recurrence is kept). Task: "
                    f"\"{(todo.task or '')[:80]}\". Last error: "
                    f"{str(error)[:200]}."
                ),
                self.settings,
                user_id=entry.user_id,
                thread_id=thread_id,
                task_id=todo.id,
            )
        except Exception:
            logger.error(
                "Pause alert dispatch failed for TODO %s",
                todo.id,
                exc_info=True,
            )

    def _send_failure_alert(
        self,
        entry: ScheduledTodoEntry,
        todo,
        thread_id: str,
        error: Exception,
        failure_count: int,
    ) -> None:
        pause_note = (
            f" It auto-pauses after {self._failure_pause_after} consecutive "
            f"failures."
            if self._failure_pause_after
            else ""
        )
        try:
            # Same belt as the pause alert: never let the alert plane break
            # the failure handler. Verdict and remedy first (#406).
            send_owner_alert(
                (
                    f"[SCHEDULED TASK ALERT] Recurring TODO [{todo.id}] has "
                    f"failed {failure_count} consecutive runs and keeps "
                    f"retrying on schedule. Manage it with /todos.{pause_note} "
                    f"Task: \"{(todo.task or '')[:80]}\". Last error: "
                    f"{str(error)[:200]}."
                ),
                self.settings,
                user_id=entry.user_id,
                thread_id=thread_id,
                task_id=todo.id,
            )
        except Exception:
            logger.error(
                "Failure alert dispatch failed for TODO %s",
                todo.id,
                exc_info=True,
            )

    @staticmethod
    def _log_stream_chunk(todo_id: str, chunk: dict, collection: StreamCollection) -> None:
        chunk_type = chunk.get("type", "unknown")
        chunk_content_preview = (
            str(chunk.get("content", ""))[:100] if chunk.get("content") else ""
        )
        logger.info(
            f"[TICKER] Chunk #{collection.chunk_count}: "
            f"type={chunk_type}, content_preview={chunk_content_preview}"
        )
        if chunk_type == "error":
            logger.error(
                f"[TICKER] Stream error for TODO {todo_id}: "
                f"code={chunk.get('code', 'unknown')}, "
                f"content={chunk.get('content', '')}"
            )
        elif chunk_type == "iteration_limit":
            logger.warning(
                f"[TICKER] Iteration limit for TODO {todo_id}: "
                f"scope={chunk.get('scope', 'unknown')}, "
                f"reason={chunk.get('reason', 'max_iterations')}, "
                f"max_iterations={chunk.get('max_iterations')}, "
                f"tool_call_count={chunk.get('tool_call_count')}"
            )

    @staticmethod
    def _log_continuation_chunk(todo_id: str, chunk: dict) -> None:
        chunk_type = chunk.get("type")
        if chunk_type == "error":
            logger.error(
                f"[TICKER] Continuation stream error for TODO "
                f"{todo_id}: code={chunk.get('code', 'unknown')}, "
                f"content={chunk.get('content', '')}"
            )
        elif chunk_type == "iteration_limit":
            logger.warning(
                f"[TICKER] Continuation also hit iteration_limit "
                f"for TODO {todo_id}. Task too complex — leaving "
                f"schedule for next tick cycle."
            )

    def _index_todo_completion(
        self,
        user_id: str,
        thread_id: str,
        todo_id: str,
        todo_task: str,
        response_summary: str,
    ) -> None:
        """
        Index TODO completion results in RAG for future reference.

        Args:
            user_id: User identifier
            thread_id: Thread identifier
            todo_id: TODO identifier
            todo_task: The TODO task description
            response_summary: Summary of the completion response
        """
        try:
            # Check if RAG is enabled for user
            profile = self.profile_manager.get_profile(user_id)
            if not profile.opt_in.rag_enabled:
                return

            rag_prefs = profile.get_rag_preferences()
            if not rag_prefs.get("include_todos", True):
                return

            # Get or create memory index
            safe_user_id = safe_path_segment(user_id)
            db_path = self.settings.data_dir / "users" / safe_user_id / "memory.db"
            # Throwaway instance: close its cached connection after use rather
            # than relying on GC finalization.
            memory_index = MemoryIndex(db_path)
            try:
                # Index the TODO completion
                content = f"TODO '{todo_task}' completed: {response_summary}"
                memory_index.add_chunk(
                    content=content,
                    metadata={"todo_id": todo_id},
                    chunk_type="todo",
                    user_id=user_id,
                    thread_id=thread_id,
                )
                logger.debug(f"Indexed TODO completion for user {user_id}, todo {todo_id}")
            finally:
                memory_index.close()

        except Exception as e:
            logger.warning(f"Failed to index TODO completion: {e}")

    def release_missed_work(self) -> dict[str, Any]:
        """Release startup-missed TODOs and paused trigger catch-up."""
        with self._recovery_lock:
            if not self._owns_schedule:
                # The hold lives in the owner's memory; clearing the shared
                # state file from here would only make it lie (#397).
                # `scheduler_control.release_missed_work` relays a non-owner's
                # release to the owner instead (#398); this guards other callers.
                status = self.get_scheduler_status()
                status["released_todo_ids"] = []
                status["release"] = "not_owner"
                return status
            released_ids = sorted(self._pending_startup_missed_ids)
            was_held = bool(released_ids) or self._trigger_catchup_paused
            self._pending_startup_missed_ids.clear()
            self._trigger_catchup_paused = False
            self._hold_since = None
            self._scheduler_state.clear_pending_missed()
            logger.info(
                "Released %s startup-missed TODO(s) for scheduler execution",
                len(released_ids),
            )
            status = self.get_scheduler_status()
            status["released_todo_ids"] = released_ids
            status["release"] = "released" if was_held else "nothing_held"
            return status

    def get_scheduler_status(self) -> dict[str, Any]:
        """Return scheduler state for API/UI lifecycle controls."""
        state = self._scheduler_state.load()
        standby_holder = self._standby_holder
        if standby_holder is not None:
            # A standby ticker's copy of the hold is whatever the owner had
            # persisted when this one was built; the shared file is current.
            pending_ids = sorted(
                str(todo_id)
                for todo_id in state.get("pending_missed_todo_ids") or []
                if todo_id
            )
            catchup_paused = bool(state.get("trigger_catchup_paused"))
        else:
            pending_ids = sorted(self._pending_startup_missed_ids)
            catchup_paused = self._trigger_catchup_paused
        pending_entries = []
        for todo_id in pending_ids:
            entry = self.schedule_db.get_entry(todo_id)
            if entry is None:
                continue
            scheduled_at = datetime.fromtimestamp(
                entry.scheduled_for,
                timezone.utc,
            ).isoformat()
            pending_entries.append(
                {
                    "todo_id": entry.todo_id,
                    "user_id": entry.user_id,
                    "thread_id": entry.thread_id,
                    "scheduled_for": scheduled_at,
                    "task_preview": entry.task_preview,
                }
            )

        return {
            "status": "ok",
            "ticker_running": self.is_running(),
            "owns_schedule": self._owns_schedule,
            "schedule_held_by": standby_holder,
            "missed_work_policy": self._missed_work_policy,
            "pending_missed_todo_count": len(pending_ids),
            "pending_missed_todo_ids": pending_ids,
            "pending_missed_todos": pending_entries,
            "trigger_catchup_paused": catchup_paused,
            "last_started_at": state.get("last_started_at"),
            "last_clean_shutdown_at": state.get("last_clean_shutdown_at"),
            "last_missed_detection_at": state.get("last_missed_detection_at"),
            "active_execution_count": self.schedule_db.count_active_executions(),
            "active_execution_stale_minutes": int(
                self._active_execution_stale_seconds / 60
            ),
        }

    def rebuild_schedule_index(self) -> int:
        """
        Rebuild the schedule index from TODO files.

        Called on startup to ensure the schedule index is in sync
        with the TODO JSON files.

        Returns:
            Number of scheduled TODOs indexed
        """
        return self.schedule_db.rebuild_from_todos(self.todo_manager)

    def is_running(self) -> bool:
        """Check if the ticker is running."""
        return self._running
