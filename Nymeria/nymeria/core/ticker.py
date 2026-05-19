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
from typing import TYPE_CHECKING, Dict, Optional

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .activity_log import ActivityType, log_activity
from .event_bus import publish_agent_stream_chunk, publish_autonomous_event
from .memory_index import MemoryIndex
from .notification_dispatch import create_autonomous_notification, should_notify_autonomous
from .stream_bridge import StreamCollection, stream_and_collect
from .todo_schedule_db import ScheduledTodoEntry, TodoScheduleDB
from .todo_manager import TodoManager, TodoStatus
from .trigger_manager import TriggerManager

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)

# Console for printing ticker output
# safe_box=True uses ASCII box-drawing characters, avoiding UnicodeEncodeError
# on Windows consoles that use cp1252/charmap encoding
_console = Console(force_terminal=True, safe_box=True)


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
        agent: "NymeriaAgent",
        schedule_db: TodoScheduleDB,
        todo_manager: TodoManager,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ):
        """
        Initialize the ticker.

        Args:
            agent: NymeriaAgent instance for executing tasks
            schedule_db: TodoScheduleDB instance for polling scheduled TODOs
            todo_manager: TodoManager for accessing TODO data
            poll_interval: Seconds between polls (from settings)
        """
        self.agent = agent
        self.schedule_db = schedule_db
        self.todo_manager = todo_manager
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._active_futures: Dict[str, Future] = {}  # todo_id -> running Future
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

    def start(self) -> None:
        """Start the ticker thread."""
        if self._running:
            logger.warning("Ticker already running")
            return

        self._running = True

        # Initialize thread pool for parallel autonomous execution
        max_workers = self.agent.settings.max_concurrent_autonomous or None  # 0 = None = unlimited
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
        """Stop the ticker thread gracefully."""
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
        logger.info("Ticker stopped")

    def _get_trigger_manager(self) -> TriggerManager:
        """Lazy-init the trigger manager."""
        if self._trigger_manager is None:
            from ..config import get_settings
            settings = get_settings()
            self._trigger_manager = TriggerManager(settings.data_dir)
        return self._trigger_manager

    def _poll_loop(self) -> None:
        """Main polling loop.

        Scheduled TODO checks run inline every tick. Trigger polling
        and TODO archival are offloaded to a housekeeping executor so
        slow network I/O or file operations cannot delay the next TODO
        check cycle or consume autonomous worker capacity.
        """
        while self._running:
            try:
                self._check_and_execute()
            except Exception as e:
                logger.error(f"Ticker poll error: {e}", exc_info=True)

            now = time.time()

            self._maybe_submit_archive(now)
            self._maybe_submit_trigger_poll(now)
            self._maybe_submit_spawn_sweep(now)

            # Sleep in small increments to allow fast shutdown
            sleep_increments = int(self.poll_interval * 10)
            for _ in range(sleep_increments):
                if not self._running:
                    break
                time.sleep(0.1)

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
        """Execute the idle-thread sweep with exception isolation."""
        try:
            from ..tools.spawn_thread import sweep_idle_spawned_threads

            deleted = sweep_idle_spawned_threads(self.agent)
            if deleted:
                logger.info(
                    f"Spawn idle-sweep deleted {deleted} temporary thread(s)"
                )
        except Exception as e:
            logger.error(f"Spawn idle-sweep error: {e}", exc_info=True)

    def _archive_completed_todos(self) -> None:
        """Archive completed TODOs older than the configured retention for all users."""
        days_old = getattr(getattr(self.agent, "settings", None), "todo_auto_archive_days", 7)
        users = self.todo_manager.get_all_users_with_todos()
        for user_id in users:
            with self.todo_manager.atomic_update(user_id) as todo_list:
                archived = todo_list.archive_completed(days_old=days_old)
                if archived > 0:
                    logger.info(
                        f"Archived {archived} completed TODO(s) older than "
                        f"{days_old} day(s) for user {user_id}"
                    )

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
            fired = manager.check_triggers(user_id, agent=self.agent)
            logger.info(f"[TRIGGER POLL] user={user_id}: {len(fired)} trigger(s) fired")
            for trigger, events in fired:
                logger.info(
                    f"[TRIGGER POLL] Firing trigger '{trigger.name}' ({trigger.id}) "
                    f"with {len(events)} event(s)"
                )
                if self._executor:
                    self._executor.submit(
                        manager.fire_action_batch, trigger, events, self.agent, user_id
                    )
                else:
                    manager.fire_action_batch(trigger, events, self.agent, user_id)

    def _check_and_execute(self) -> None:
        """Check for due scheduled TODOs and execute them."""
        now = time.time()

        # Log ticker poll - less frequently when no due items
        # Get entries from DB (this will also log what's in the DB)
        due_entries = self.schedule_db.get_due(before=now)

        if due_entries:
            logger.info(f"[TICKER POLL] NOW={now} ({datetime.fromtimestamp(now)}) - Found {len(due_entries)} due TODO(s)!")
            for entry in due_entries:
                logger.info(f"[TICKER POLL] Due: todo_id={entry.todo_id}, user={entry.user_id}, scheduled_for={entry.scheduled_for} ({datetime.fromtimestamp(entry.scheduled_for)})")
        else:
            # Log every 30 seconds to confirm ticker is running
            if int(now) % 30 < self.poll_interval:
                logger.info(f"[TICKER POLL] NOW={now} ({datetime.fromtimestamp(now)}) - No due TODOs")

        # Clean up completed futures
        with self._lock:
            done_ids = [tid for tid, f in self._active_futures.items() if f.done()]
            for tid in done_ids:
                f = self._active_futures.pop(tid)
                exc = f.exception()
                if exc:
                    logger.error(f"Task {tid} failed in thread pool: {exc}")

        for entry in due_entries:
            # Skip if already running in the pool
            with self._lock:
                if entry.todo_id in self._active_futures:
                    continue

            # Submit to thread pool for parallel execution
            if self._executor:
                future = self._executor.submit(self._execute_scheduled_todo, entry)
                with self._lock:
                    self._active_futures[entry.todo_id] = future

    def _calculate_next_execution(self, recurrence: str, from_time: datetime) -> Optional[datetime]:
        """
        Calculate next execution time based on recurrence pattern.

        Args:
            recurrence: Recurrence pattern ('5min', '10min', '15min', '30min', 'hourly', 'daily', 'weekly', 'monthly')
            from_time: Time to calculate from

        Returns:
            Next execution datetime, or None if invalid recurrence
        """
        from .todo_constants import calculate_next_recurrence_time
        return calculate_next_recurrence_time(recurrence, from_time)

    def _should_create_autonomous_notification(self, thread_id: str) -> bool:
        return should_notify_autonomous(thread_id, self.agent.thread_config_manager)

    def _execute_scheduled_todo(self, entry: ScheduledTodoEntry) -> None:
        """Execute a scheduled TODO via the agent stream.

        Orchestrates pre-flight validation, streaming (with optional
        continuation on iteration limit), and finalization.  Delegates
        streaming, success, and error paths to focused helpers.
        """
        if not self.schedule_db.mark_execution_started(entry.todo_id, entry.user_id, entry.thread_id):
            logger.info(f"Scheduled TODO {entry.todo_id} is already executing, skipping duplicate run")
            return

        todo = self.todo_manager.get_todo_by_id(entry.user_id, entry.todo_id)
        if not todo:
            logger.warning(f"Scheduled TODO {entry.todo_id} not found, removing from schedule")
            self.schedule_db.remove_scheduled(entry.todo_id)
            self.schedule_db.clear_execution(entry.todo_id, entry.user_id)
            return

        if not todo.is_active():
            logger.info(f"Scheduled TODO {entry.todo_id} is no longer active, removing from schedule")
            self.schedule_db.remove_scheduled(entry.todo_id)
            self.schedule_db.clear_execution(entry.todo_id, entry.user_id)
            return

        thread_id = entry.thread_id or todo.thread_id or f"todo-{todo.id}"
        logger.info(f"Executing scheduled TODO {todo.id} for user {entry.user_id}: {todo.task[:50]}...")

        try:
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                todo_list.update_item(todo.id, status=TodoStatus.IN_PROGRESS)
        except Exception:
            self.schedule_db.clear_execution(todo.id, entry.user_id)
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
            logger.info(f"[TICKER] Prompt: {prompt[:200]}...")

            stream_result, renderer, completed_early = self._stream_todo_execution(
                entry, todo, thread_id, prompt,
            )

            if not completed_early:
                self._finalize_successful_execution(
                    entry, todo, thread_id, stream_result, renderer,
                )

        except Exception as e:
            self._handle_execution_failure(entry, todo, thread_id, e)

        self.schedule_db.clear_execution(todo.id, entry.user_id)

    # ------------------------------------------------------------------
    # Helpers for _execute_scheduled_todo
    # ------------------------------------------------------------------

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
            # lock — otherwise a `queued` chunk would flip the frontend
            # into autonomous-streaming mode mid-conversation.
            if not started_published and chunk.get("type") not in ("queued", "prompt_queued"):
                publish_autonomous_event(
                    event_type="task_started",
                    thread_id=thread_id,
                    user_id=entry.user_id,
                    task_id=todo.id,
                    data={"prompt": prompt, "todo_id": todo.id},
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
            self.agent,
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
            self.agent,
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
        backoff_time = datetime.now(timezone.utc) + timedelta(minutes=10)
        with self.todo_manager.atomic_update(entry.user_id) as todo_list:
            todo_list.update_item(
                todo.id,
                scheduled_for=backoff_time,
                notes=(
                    f"Hit iteration limit twice — rescheduled "
                    f"for {backoff_time.isoformat()}"
                ),
            )
        self.todo_manager.sync_schedule_to_db(
            entry.user_id, todo.id, self.schedule_db
        )
        logger.info(
            f"[TICKER] TODO {todo.id} rescheduled to "
            f"{backoff_time.isoformat()} after double iteration_limit"
        )

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=entry.user_id,
            task_id=todo.id,
            data={
                "notify": False,
                "content": (
                    "Task requires more steps than the current "
                    "iteration limit allows. Rescheduled for "
                    "10 minutes from now."
                ),
                "todo_id": todo.id,
            },
        )
        self.schedule_db.clear_execution(todo.id, entry.user_id)

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

        if todo.id in self._retry_counts:
            del self._retry_counts[todo.id]

        should_notify = self._should_create_autonomous_notification(thread_id)
        notification_summary = response_text[:200] if response_text else "Scheduled TODO executed"

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=entry.user_id,
            task_id=todo.id,
            data={
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
                settings=self.agent.settings,
                thread_config_manager=self.agent.thread_config_manager,
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

        self._trim_context_if_needed(entry, thread_id)

    def _handle_recurrence(self, entry: ScheduledTodoEntry, todo) -> None:
        current_todo = self.todo_manager.get_todo_by_id(entry.user_id, todo.id)
        if current_todo and current_todo.recurrence:
            recurrence_anchor = datetime.fromtimestamp(
                entry.scheduled_for,
                timezone.utc,
            )
            next_execution = self._calculate_next_execution(
                current_todo.recurrence,
                recurrence_anchor,
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

    def _trim_context_if_needed(
        self, entry: ScheduledTodoEntry, thread_id: str,
    ) -> None:
        if self.agent.settings.context_management != "sliding_window":
            return
        cycle_count = self.agent.get_context_cycle_count(thread_id)
        max_cycles = self.agent.settings.sliding_window_cycles
        if cycle_count <= max_cycles:
            return
        messages_removed = self.agent.trim_context_window(
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
        logger.error(f"[TICKER] === ERROR === TODO {todo.id} failed: {error}")
        logger.error(f"[TICKER] Traceback:\n{traceback.format_exc()}")
        try:
            sanitized_error = _sanitize_unicode(str(error))
            _console.print(f"[red]Scheduled TODO failed: {sanitized_error}[/red]")
        except Exception:
            logger.debug("Console render failed for error message")

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
            },
        )

        create_autonomous_notification(
            user_id=entry.user_id,
            thread_id=thread_id,
            task_id=todo.id,
            summary=f"Task failed: {str(error)[:180]}",
            settings=self.agent.settings,
            thread_config_manager=self.agent.thread_config_manager,
        )

        retry_count = self._retry_counts.get(todo.id, 0) + 1
        self._retry_counts[todo.id] = retry_count

        if retry_count >= self.MAX_RETRIES:
            self.schedule_db.remove_scheduled(todo.id)
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                todo_list.update_item(
                    todo.id,
                    status=TodoStatus.PENDING,
                    notes=f"Scheduled execution failed after {retry_count} retries: {str(error)[:100]}",
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
            del self._retry_counts[todo.id]
        else:
            logger.info(f"TODO {todo.id} will retry (attempt {retry_count + 1}/{self.MAX_RETRIES})")

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
            profile = self.agent.profile_manager.get_profile(user_id)
            if not profile.opt_in.rag_enabled:
                return

            rag_prefs = profile.get_rag_preferences()
            if not rag_prefs.get("include_todos", True):
                return

            # Get or create memory index
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_") or "default"
            db_path = self.agent.settings.data_dir / "users" / safe_user_id / "memory.db"
            memory_index = MemoryIndex(db_path)

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

        except Exception as e:
            logger.warning(f"Failed to index TODO completion: {e}")

    def recover_missed_schedules(self) -> int:
        """
        Recover and report missed scheduled TODOs from previous session.

        Called on startup. Finds all scheduled TODOs with scheduled_for in the past.
        They will be picked up in the next poll cycle.

        Returns:
            Number of missed schedules
        """
        now = time.time()
        missed_entries = self.schedule_db.get_due(before=now)

        if missed_entries:
            logger.info(f"Recovering {len(missed_entries)} missed scheduled TODO(s)")
            try:
                _console.print()
                _console.print(
                    Panel(
                        f"[yellow]Found {len(missed_entries)} scheduled TODO(s) from before shutdown. "
                        f"Executing now...[/yellow]",
                        title="[bold]Scheduler Recovery[/bold]",
                        border_style="yellow",
                    )
                )
                _console.print()
            except Exception:
                logger.debug("Console render failed for output spacing")

        return len(missed_entries)

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
