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
from typing import TYPE_CHECKING, Dict, Optional, Set

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .activity_log import ActivityType, log_activity
from .event_bus import publish_agent_stream_chunk, publish_autonomous_event
from .memory_index import MemoryIndex
from .notifications import create_notification
from .response_handler import create_response
from .stream_bridge import iter_agent_astream
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
        pass


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

        # Trigger system: poll-based sources checked at a slower interval
        self.trigger_poll_interval = max(poll_interval * 6, 30)  # Default 30s
        self._last_trigger_check: float = 0.0
        self._trigger_manager: Optional[TriggerManager] = None

        # Auto-purge: archive completed TODOs periodically (~1 hour)
        self._archive_interval = 3600  # 1 hour
        self._last_archive_check: float = 0.0

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
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Ticker stopped")

    def _get_trigger_manager(self) -> TriggerManager:
        """Lazy-init the trigger manager."""
        if self._trigger_manager is None:
            from ..config import get_settings
            settings = get_settings()
            self._trigger_manager = TriggerManager(settings.data_dir)
        return self._trigger_manager

    def _poll_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                self._check_and_execute()
            except Exception as e:
                logger.error(f"Ticker poll error: {e}", exc_info=True)

            # Auto-purge completed TODOs (~every hour)
            now = time.time()
            if now - self._last_archive_check >= self._archive_interval:
                try:
                    self._archive_completed_todos()
                except Exception as e:
                    logger.error(f"Archive completed TODOs error: {e}", exc_info=True)
                self._last_archive_check = now

            # Check poll-based trigger sources at a slower interval
            if now - self._last_trigger_check >= self.trigger_poll_interval:
                try:
                    self._check_triggers()
                except Exception as e:
                    logger.error(f"Trigger poll error: {e}", exc_info=True)
                self._last_trigger_check = now

            # Sleep in small increments to allow fast shutdown
            sleep_increments = int(self.poll_interval * 10)
            for _ in range(sleep_increments):
                if not self._running:
                    break
                time.sleep(0.1)

    def _archive_completed_todos(self) -> None:
        """Archive completed TODOs older than 7 days for all users."""
        users = self.todo_manager.get_all_users_with_todos()
        for user_id in users:
            with self.todo_manager.atomic_update(user_id) as todo_list:
                archived = todo_list.archive_completed(days_old=7)
                if archived > 0:
                    logger.info(f"Archived {archived} completed TODO(s) for user {user_id}")

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
        from .todo_constants import RECURRENCE_DELTAS
        delta = RECURRENCE_DELTAS.get(recurrence)
        if delta:
            return from_time + delta
        return None

    def _in_app_notification_level(self, thread_id: str) -> str:
        """Return per-thread notification-center behavior."""
        try:
            tc = self.agent.thread_config_manager.get_config(thread_id)
            if tc is None:
                return "notify_only"
            return getattr(tc, "in_app_notification_level", "notify_only") or "notify_only"
        except Exception as e:
            logger.debug("Failed to read notification level for %s: %s", thread_id, e)
            return "notify_only"

    def _should_create_autonomous_notification(self, thread_id: str) -> bool:
        return self._in_app_notification_level(thread_id) == "all_autonomous"

    def _execute_scheduled_todo(self, entry: ScheduledTodoEntry) -> None:
        """
        Execute a scheduled TODO.

        Streams from the agent, buffering events until streaming completes,
        then publishes task_started + buffered events + task_completed to
        the event bus.

        Args:
            entry: The scheduled TODO entry to execute
        """
        if not self.schedule_db.mark_execution_started(entry.todo_id, entry.user_id, entry.thread_id):
            logger.info(f"Scheduled TODO {entry.todo_id} is already executing, skipping duplicate run")
            return

        # Get the full TODO from the manager
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

        # Determine thread_id - use stored or generate from TODO
        thread_id = entry.thread_id or todo.thread_id or f"todo-{todo.id}"

        logger.info(f"Executing scheduled TODO {todo.id} for user {entry.user_id}: {todo.task[:50]}...")

        # Mark TODO as in_progress
        try:
            with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                todo_list.update_item(todo.id, status=TodoStatus.IN_PROGRESS)
        except Exception:
            self.schedule_db.clear_execution(todo.id, entry.user_id)
            raise

        # Log activity for scheduled execution start
        log_activity(
            ActivityType.SELF_INVOKE,
            f"Scheduled TODO started: {todo.task[:100]}",
            user_id=entry.user_id,
            thread_id=thread_id,
            metadata={"todo_id": todo.id},
        )

        # Build prompt from TODO
        prompt = f"Work on TODO {todo.id}: {todo.task}"
        if todo.notes:
            prompt += f"\n\nNotes: {todo.notes}"

        try:
            # Print wake-up message (inside try so console errors don't prevent execution)
            try:
                sanitized_task = _sanitize_unicode(todo.task)
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
            logger.info(f"[TICKER] === START === TODO {todo.id}, thread={thread_id}, user={entry.user_id}")
            logger.info(f"[TICKER] Prompt: {prompt[:200]}...")

            response_parts = []
            thinking_parts = []
            pending_calls = {}  # call_id → {name, args} for console rendering
            response_buffer = ""  # for inline console rendering
            printed_header = False  # "Nymeria:" label
            had_tool_calls = False
            chunk_count = 0
            iteration_limit_hit = False
            started_published = False
            for chunk in iter_agent_astream(
                self.agent,
                message=prompt,
                thread_id=thread_id,
                user_id=entry.user_id,
                _is_self_invoke=True,
            ):
                if not started_published:
                    publish_autonomous_event(
                        event_type="task_started",
                        thread_id=thread_id,
                        user_id=entry.user_id,
                        task_id=todo.id,
                        data={"prompt": prompt, "todo_id": todo.id},
                    )
                    started_published = True

                chunk_count += 1
                chunk_type = chunk.get('type', 'unknown')
                chunk_content_preview = str(chunk.get('content', ''))[:100] if chunk.get('content') else ''
                logger.info(f"[TICKER] Chunk #{chunk_count}: type={chunk_type}, content_preview={chunk_content_preview}")
                chunk_type = chunk.get("type")
                publish_agent_stream_chunk(
                    chunk,
                    thread_id=thread_id,
                    user_id=entry.user_id,
                    task_id=todo.id,
                )

                # Publish each event live as it arrives
                if chunk_type == "tool_call":
                    # Flush buffered preamble text before tool one-liners
                    if response_buffer.strip():
                        try:
                            if not printed_header:
                                _console.print()
                                _console.print("[bold green]Nymeria:[/bold green]")
                                printed_header = True
                            elif had_tool_calls:
                                _console.print()
                            _console.print(Markdown(_sanitize_unicode(response_buffer.strip())))
                        except Exception:
                            pass
                        response_buffer = ""

                    pending_calls[chunk.get("id", "")] = {
                        "name": chunk.get("name", "unknown"),
                        "args": chunk.get("args", {}),
                    }
                elif chunk_type == "tool_result":
                    _render_tool_line(pending_calls, chunk)
                    had_tool_calls = True
                elif chunk_type == "thinking":
                    content = chunk.get("content", "")
                    if content:
                        thinking_parts.append(content)
                elif chunk_type == "response":
                    content = chunk.get("content", "")
                    if content:
                        response_parts.append(content)
                        response_buffer += content

                elif chunk_type == "error":
                    error_content = chunk.get("content", "")
                    error_code = chunk.get("code", "unknown")
                    logger.error(
                        f"[TICKER] Stream error for TODO {todo.id}: "
                        f"code={error_code}, content={error_content}"
                    )
                    raise RuntimeError(
                        error_content or f"Agent stream error (code={error_code})"
                    )

                elif chunk_type == "iteration_limit":
                    scope = chunk.get("scope", "unknown")
                    reason = chunk.get("reason", "max_iterations")
                    logger.warning(
                        f"[TICKER] Iteration limit for TODO {todo.id}: "
                        f"scope={scope}, "
                        f"reason={reason}, "
                        f"max_iterations={chunk.get('max_iterations')}, "
                        f"tool_call_count={chunk.get('tool_call_count')}"
                    )
                    # Only trigger continuation for main_agent limits.
                    # Sub-agent limits are informational — the main agent
                    # can still continue working.
                    # Repeated tool/result loops are likely runaways, not
                    # useful continuation checkpoints.
                    if scope == "main_agent" and reason != "repeated_tool_result":
                        iteration_limit_hit = True

            # --- Continuation on iteration_limit (one attempt max) ---
            if iteration_limit_hit:
                continuation_prompt = (
                    f"Continue working on the scheduled task: {todo.task}. "
                    f"If you have already completed everything, please confirm "
                    f"the results."
                )
                logger.info(
                    f"[TICKER] Sending continuation prompt for TODO {todo.id} "
                    f"after iteration_limit"
                )
                continuation_limit_hit = False

                for chunk in iter_agent_astream(
                    self.agent,
                    message=continuation_prompt,
                    thread_id=thread_id,
                    user_id=entry.user_id,
                    _is_self_invoke=True,
                ):
                    chunk_count += 1
                    chunk_type = chunk.get("type")
                    publish_agent_stream_chunk(
                        chunk,
                        thread_id=thread_id,
                        user_id=entry.user_id,
                        task_id=todo.id,
                    )

                    if chunk_type == "tool_call":
                        # Flush buffered preamble text
                        if response_buffer.strip():
                            try:
                                if not printed_header:
                                    _console.print()
                                    _console.print("[bold green]Nymeria:[/bold green]")
                                    printed_header = True
                                elif had_tool_calls:
                                    _console.print()
                                _console.print(Markdown(_sanitize_unicode(response_buffer.strip())))
                            except Exception:
                                pass
                            response_buffer = ""

                        pending_calls[chunk.get("id", "")] = {
                            "name": chunk.get("name", "unknown"),
                            "args": chunk.get("args", {}),
                        }
                    elif chunk_type == "tool_result":
                        _render_tool_line(pending_calls, chunk)
                        had_tool_calls = True
                    elif chunk_type == "thinking":
                        content = chunk.get("content", "")
                        if content:
                            thinking_parts.append(content)
                    elif chunk_type == "response":
                        content = chunk.get("content", "")
                        if content:
                            response_parts.append(content)
                            response_buffer += content
                    elif chunk_type == "error":
                        error_content = chunk.get("content", "")
                        error_code = chunk.get("code", "unknown")
                        logger.error(
                            f"[TICKER] Continuation stream error for TODO "
                            f"{todo.id}: code={error_code}, "
                            f"content={error_content}"
                        )
                        raise RuntimeError(
                            error_content
                            or f"Agent continuation error (code={error_code})"
                        )
                    elif chunk_type == "iteration_limit":
                        logger.warning(
                            f"[TICKER] Continuation also hit iteration_limit "
                            f"for TODO {todo.id}. Task too complex — leaving "
                            f"schedule for next tick cycle."
                        )
                        continuation_limit_hit = True

                if continuation_limit_hit:
                    # Task too complex even with a second pass.
                    # Reschedule 10 minutes ahead to avoid hot-looping
                    # (the old scheduled_for is in the past, so leaving it
                    # as-is would re-trigger on the next tick poll).
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
                    return
            # --- End continuation ---

            # Compute final response text
            if response_parts:
                response_text = "".join(response_parts)
            elif thinking_parts:
                response_text = "".join(thinking_parts)
                logger.info(f"[TICKER] No response chunks, using thinking content as response ({len(thinking_parts)} parts)")
            else:
                response_text = ""
            logger.info(f"[TICKER] === STREAM DONE === chunks={chunk_count}, response_parts={len(response_parts)}, thinking_parts={len(thinking_parts)}, response_len={len(response_text)}")

            # Create response object
            logger.info(f"Raw autonomous response (first 500 chars): {response_text[:500] if response_text else 'empty'}")
            response = create_response(
                content=response_text,
                notify=False,
            )
            logger.info(f"Response: notify={response.notify}")

            # Handle recurring TODOs: reschedule instead of clearing
            # Re-fetch the TODO to get the recurrence field
            current_todo = self.todo_manager.get_todo_by_id(entry.user_id, todo.id)
            if current_todo and current_todo.recurrence:
                # Calculate next execution time
                next_execution = self._calculate_next_execution(
                    current_todo.recurrence,
                    datetime.now(timezone.utc)
                )
                if next_execution:
                    logger.info(f"Rescheduling recurring TODO {todo.id} ({current_todo.recurrence}) for {next_execution}")
                    with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                        todo_list.update_item(
                            todo.id,
                            scheduled_for=next_execution,
                            status=TodoStatus.PENDING,  # Reset to pending for next execution
                        )
                        # Update last_execution
                        item = todo_list.get_item(todo.id)
                        if item:
                            item.last_execution = datetime.now(timezone.utc)
                    # Sync the new schedule
                    self.todo_manager.sync_schedule_to_db(entry.user_id, todo.id, self.schedule_db)
                else:
                    # Invalid recurrence, clear schedule
                    self.todo_manager.clear_todo_schedule(entry.user_id, todo.id, self.schedule_db)
            else:
                # Non-recurring TODO: clear schedule after execution
                self.todo_manager.clear_todo_schedule(entry.user_id, todo.id, self.schedule_db)

            # Clear retry count on success
            if todo.id in self._retry_counts:
                del self._retry_counts[todo.id]

            should_notify = response.notify or self._should_create_autonomous_notification(thread_id)
            notification_summary = (
                response.summary
                or (response.content[:200] if response.content else "Scheduled TODO executed")
            )

            # Publish task completed event
            publish_autonomous_event(
                event_type="task_completed",
                thread_id=thread_id,
                user_id=entry.user_id,
                task_id=todo.id,
                data={
                    "notify": should_notify,
                    "content": response.content,
                    "summary": response.summary,
                    "todo_id": todo.id,
                },
            )

            # Send FCM push to registered devices
            if response.content and self.agent.settings.fcm_enabled:
                try:
                    from .fcm import send_to_all_devices
                    data_dir = str(self.agent.settings.data_dir)
                    send_to_all_devices(
                        data_dir=data_dir,
                        text=response.content,
                        thread_id=thread_id,
                        task_id=todo.id,
                        summary=response.summary or "",
                        user_id=entry.user_id,
                    )
                except Exception as e:
                    logger.warning(f"[TICKER] FCM push failed: {e}")

            # Index TODO completion in RAG (if enabled)
            self._index_todo_completion(
                user_id=entry.user_id,
                thread_id=thread_id,
                todo_id=todo.id,
                todo_task=todo.task,
                response_summary=response.summary or response.content[:200] if response.content else "",
            )

            # Log activity
            log_activity(
                ActivityType.TASK_COMPLETED,
                response.summary or response.content[:200] if response.content else "Scheduled TODO executed",
                user_id=entry.user_id,
                thread_id=thread_id,
                metadata={"todo_id": todo.id, "notify": should_notify},
            )

            # Show remaining response in console (non-fatal — task already succeeded)
            try:
                remaining = response_buffer.strip()
                if remaining:
                    if not printed_header:
                        _console.print()
                        _console.print("[bold green]Nymeria:[/bold green]")
                    elif had_tool_calls:
                        _console.print()  # separator after tool one-liners
                    _console.print(Markdown(_sanitize_unicode(remaining)))
            except Exception as console_err:
                logger.warning(f"Console print failed (non-fatal): {console_err}")

            # Create notification if requested or the thread wants all autonomous completions.
            if should_notify and notification_summary:
                try:
                    create_notification(
                        user_id=entry.user_id,
                        summary=notification_summary,
                        thread_id=thread_id,
                        task_id=todo.id,
                    )
                    _console.print(f"[yellow]Notification sent: {notification_summary}[/yellow]")
                except Exception as notify_err:
                    logger.error(f"Failed to create notification for TODO {todo.id}: {notify_err}")

            try:
                _console.print()
            except Exception:
                pass
            logger.info(f"TODO {todo.id} scheduled execution completed, notify={should_notify}")

            # Trim context window if needed (only in sliding_window mode)
            if self.agent.settings.context_management == "sliding_window":
                cycle_count = self.agent.get_context_cycle_count(thread_id)
                max_cycles = self.agent.settings.sliding_window_cycles
                if cycle_count > max_cycles:
                    messages_removed = self.agent.trim_context_window(thread_id, max_cycles, user_id=entry.user_id)
                    if messages_removed > 0:
                        logger.info(
                            f"Thread {thread_id}: Sliding window trimmed {messages_removed} messages "
                            f"(was {cycle_count} cycles, now {max_cycles})"
                        )
                        try:
                            _console.print(
                                f"[dim]Context window trimmed: kept last {max_cycles} cycles[/dim]"
                            )
                        except Exception:
                            pass  # Console output is cosmetic

        except Exception as e:
            import traceback
            logger.error(f"[TICKER] === ERROR === TODO {todo.id} failed: {e}")
            logger.error(f"[TICKER] Traceback:\n{traceback.format_exc()}")
            try:
                sanitized_error = _sanitize_unicode(str(e))
                _console.print(f"[red]Scheduled TODO failed: {sanitized_error}[/red]")
            except Exception:
                pass  # Console output is cosmetic

            # Always publish task_completed so frontend can exit streaming state
            publish_autonomous_event(
                event_type="task_completed",
                thread_id=thread_id,
                user_id=entry.user_id,
                task_id=todo.id,
                data={
                    "error": True,
                    "error_message": str(e)[:200],
                    "content": f"Task failed: {str(e)[:200]}",
                    "todo_id": todo.id,
                },
            )

            if self._should_create_autonomous_notification(thread_id):
                try:
                    create_notification(
                        user_id=entry.user_id,
                        summary=f"Task failed: {str(e)[:180]}",
                        thread_id=thread_id,
                        task_id=todo.id,
                    )
                except Exception as notify_err:
                    logger.error(f"Failed to create failure notification for TODO {todo.id}: {notify_err}")

            # Check retry count
            retry_count = self._retry_counts.get(todo.id, 0) + 1
            self._retry_counts[todo.id] = retry_count

            if retry_count >= self.MAX_RETRIES:
                # Remove from schedule after too many retries
                self.schedule_db.remove_scheduled(todo.id)

                # Update TODO with failure info and clear schedule
                with self.todo_manager.atomic_update(entry.user_id) as todo_list:
                    todo_list.update_item(
                        todo.id,
                        status=TodoStatus.PENDING,
                        notes=f"Scheduled execution failed after {retry_count} retries: {str(e)[:100]}",
                        clear_schedule=True,
                    )

                logger.error(f"TODO {todo.id} failed permanently after {retry_count} retries")

                # Log activity for task failure
                log_activity(
                    ActivityType.TASK_FAILED,
                    f"Scheduled TODO failed: {todo.task[:80]} - {str(e)[:50]}",
                    user_id=entry.user_id,
                    thread_id=thread_id,
                    metadata={"todo_id": todo.id, "error": str(e), "retries": retry_count},
                )

                # Clear retry count
                del self._retry_counts[todo.id]
            else:
                # Keep in schedule for retry
                logger.info(f"TODO {todo.id} will retry (attempt {retry_count + 1}/{self.MAX_RETRIES})")

        self.schedule_db.clear_execution(todo.id, entry.user_id)

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
                pass  # Console output is cosmetic

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
