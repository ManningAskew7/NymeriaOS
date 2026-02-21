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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, Optional, Set

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .activity_log import ActivityType, log_activity
from .event_bus import publish_autonomous_event
from .memory_index import MemoryIndex
from .notifications import create_notification
from .response_handler import create_response
from .todo_schedule_db import ScheduledTodoEntry, TodoScheduleDB
from .todo_manager import TodoManager, TodoStatus
from .trigger_manager import TriggerManager
from ..tools.visibility import get_and_clear_mute_flag

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)

# Console for printing ticker output with UTF-8 encoding
_console = Console(force_terminal=True)


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
            fired = manager.check_triggers(user_id)
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

    def _execute_scheduled_todo(self, entry: ScheduledTodoEntry) -> None:
        """
        Execute a scheduled TODO with buffered output.

        Streams from the agent internally but buffers all events until
        the mute decision is made. If the agent calls mute_response,
        nothing is published to the frontend — the response only appears
        in the activity log. If not muted, task_started + all buffered
        events are flushed to the event bus, then task_completed.

        Args:
            entry: The scheduled TODO entry to execute
        """
        # Get the full TODO from the manager
        todo = self.todo_manager.get_todo_by_id(entry.user_id, entry.todo_id)
        if not todo:
            logger.warning(f"Scheduled TODO {entry.todo_id} not found, removing from schedule")
            self.schedule_db.remove_scheduled(entry.todo_id)
            return

        if not todo.is_active():
            logger.info(f"Scheduled TODO {entry.todo_id} is no longer active, removing from schedule")
            self.schedule_db.remove_scheduled(entry.todo_id)
            return

        # Determine thread_id - use stored or generate from TODO
        thread_id = entry.thread_id or todo.thread_id or f"todo-{todo.id}"

        logger.info(f"Executing scheduled TODO {todo.id} for user {entry.user_id}: {todo.task[:50]}...")

        # Mark TODO as in_progress
        with self.todo_manager.atomic_update(entry.user_id) as todo_list:
            todo_list.update_item(todo.id, status=TodoStatus.IN_PROGRESS)

        # Log activity for scheduled execution start
        log_activity(
            ActivityType.SELF_INVOKE,
            f"Scheduled TODO started: {todo.task[:100]}",
            user_id=entry.user_id,
            thread_id=thread_id,
            metadata={"todo_id": todo.id},
        )

        # Print wake-up message (sanitize for Windows console)
        sanitized_task = _sanitize_unicode(todo.task)
        _console.print()
        _console.print(
            Panel(
                f"[italic]{sanitized_task}[/italic]",
                title="[bold yellow]Wake up Nymeria, you have work to do[/bold yellow]",
                border_style="yellow",
            )
        )

        # Build prompt from TODO
        prompt = f"Work on TODO {todo.id}: {todo.task}"
        if todo.notes:
            prompt += f"\n\nNotes: {todo.notes}"

        try:
            # Execute through agent, buffering events until mute decision is made.
            # Events are only published to the frontend AFTER streaming completes:
            # - Muted: nothing reaches the frontend (no flash of content)
            # - Not muted: task_started + buffered events flushed, then task_completed
            logger.info(f"[TICKER] === START === TODO {todo.id}, thread={thread_id}, user={entry.user_id}")
            logger.info(f"[TICKER] Prompt: {prompt[:200]}...")
            response_parts = []
            thinking_parts = []
            buffered_events = []  # Buffer events until mute decision
            chunk_count = 0
            for chunk in self.agent.stream(
                message=prompt,
                thread_id=thread_id,
                user_id=entry.user_id,
                _is_self_invoke=True,
            ):
                chunk_count += 1
                chunk_type = chunk.get('type', 'unknown')
                chunk_content_preview = str(chunk.get('content', ''))[:100] if chunk.get('content') else ''
                logger.info(f"[TICKER] Chunk #{chunk_count}: type={chunk_type}, content_preview={chunk_content_preview}")
                chunk_type = chunk.get("type")

                # Buffer streaming events (published after mute decision)
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

            # Combine response parts
            if response_parts:
                response_text = "".join(response_parts)
            elif thinking_parts:
                response_text = "".join(thinking_parts)
                logger.info(f"[TICKER] No response chunks, using thinking content as response ({len(thinking_parts)} parts)")
                # Convert thinking events to response events in the buffer so the
                # frontend places the text in `content` (below tool cards) instead
                # of `intermediateContent` (above tool cards), preventing duplication
                # with the same text sent in task_completed.
                for i, evt in enumerate(buffered_events):
                    if evt["event_type"] == "thinking":
                        buffered_events[i] = {
                            "event_type": "response",
                            "data": {"content": evt["data"]["content"]},
                        }
            else:
                response_text = ""
            logger.info(f"[TICKER] === STREAM DONE === chunks={chunk_count}, response_parts={len(response_parts)}, thinking_parts={len(thinking_parts)}, response_len={len(response_text)}")

            # Check mute BEFORE publishing any events to the frontend
            mute_info = get_and_clear_mute_flag(thread_id)
            if mute_info and mute_info.get("muted"):
                visibility = "activity"
                logger.info(f"Response muted via tool: {mute_info.get('reason', 'no reason')}")
                # Persist muted turn so UI hides it on history reload too
                self.agent.mark_last_turn_muted(thread_id)
            else:
                visibility = "full"
                # Not muted — flush task_started + buffered events to frontend
                publish_autonomous_event(
                    event_type="task_started",
                    thread_id=thread_id,
                    user_id=entry.user_id,
                    task_id=todo.id,
                    data={"prompt": prompt, "todo_id": todo.id},
                )
                for event in buffered_events:
                    publish_autonomous_event(
                        event_type=event["event_type"],
                        thread_id=thread_id,
                        user_id=entry.user_id,
                        task_id=todo.id,
                        data=event["data"],
                    )

            # Create response object (no parsing needed - just raw content)
            logger.info(f"Raw autonomous response (first 500 chars): {response_text[:500] if response_text else 'empty'}")
            response = create_response(
                content=response_text,
                visibility=visibility,
                notify=False,  # Notifications will be handled by separate tool later
            )
            logger.info(f"Response: visibility={response.visibility}, notify={response.notify}")
            is_activity_only = response.visibility == "activity"

            # Handle recurring TODOs: reschedule instead of clearing
            # Re-fetch the TODO to get the recurrence field
            current_todo = self.todo_manager.get_todo_by_id(entry.user_id, todo.id)
            if current_todo and current_todo.recurrence:
                # Calculate next execution time
                next_execution = self._calculate_next_execution(
                    current_todo.recurrence,
                    datetime.utcnow()
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
                            item.last_execution = datetime.utcnow()
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

            # Publish task completed event with visibility info
            publish_autonomous_event(
                event_type="task_completed",
                thread_id=thread_id,
                user_id=entry.user_id,
                task_id=todo.id,
                data={
                    "visibility": response.visibility,
                    "notify": response.notify,
                    "content": response.content,
                    "summary": response.summary,
                    "todo_id": todo.id,
                },
            )

            # Index TODO completion in RAG (if enabled)
            self._index_todo_completion(
                user_id=entry.user_id,
                thread_id=thread_id,
                todo_id=todo.id,
                todo_task=todo.task,
                response_summary=response.summary or response.content[:200] if response.content else "",
            )

            # Route based on visibility mode
            if is_activity_only:
                # Activity log only - minimal console output
                log_activity(
                    ActivityType.TASK_COMPLETED,
                    response.content[:200] if response.content else "Scheduled TODO executed",
                    user_id=entry.user_id,
                    thread_id=thread_id,
                    metadata={"todo_id": todo.id, "visibility": "activity"},
                )

                # Show minimal console output
                next_entry = self.schedule_db.get_next_for_user(entry.user_id)
                if next_entry:
                    time_remaining = next_entry.scheduled_for - time.time()
                    mins = max(0, int(time_remaining // 60))
                    next_task_sanitized = _sanitize_unicode(next_entry.task_preview[:50])
                    _console.print(
                        Panel(
                            f"[dim]Nothing important to report. Next check: {next_task_sanitized}... in {mins}m[/dim]",
                            title="[dim]Background task complete[/dim]",
                            border_style="dim",
                        )
                    )
                else:
                    _console.print("[dim]Background task complete, nothing to report.[/dim]")
            else:
                # Full visibility - show in thread
                log_activity(
                    ActivityType.TASK_COMPLETED,
                    response.summary or response.content[:200] if response.content else "Scheduled TODO executed",
                    user_id=entry.user_id,
                    thread_id=thread_id,
                    metadata={"todo_id": todo.id, "visibility": "full", "notify": response.notify},
                )

                # Show full response
                sanitized_content = _sanitize_unicode(response.content)
                _console.print()
                _console.print("[bold green]Nymeria:[/bold green]")
                _console.print(Markdown(sanitized_content))

                # Create notification if requested
                if response.notify and response.summary:
                    create_notification(
                        user_id=entry.user_id,
                        summary=response.summary,
                        thread_id=thread_id,
                        task_id=todo.id,
                    )
                    _console.print(f"[yellow]Notification sent: {response.summary}[/yellow]")

            _console.print()
            logger.info(f"TODO {todo.id} scheduled execution completed, visibility={response.visibility}, notify={response.notify}")

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
                        _console.print(
                            f"[dim]Context window trimmed: kept last {max_cycles} cycles[/dim]"
                        )

        except Exception as e:
            import traceback
            logger.error(f"[TICKER] === ERROR === TODO {todo.id} failed: {e}")
            logger.error(f"[TICKER] Traceback:\n{traceback.format_exc()}")
            sanitized_error = _sanitize_unicode(str(e))
            _console.print(f"[red]Scheduled TODO failed: {sanitized_error}[/red]")

            # Always publish task_completed so frontend can exit streaming state
            publish_autonomous_event(
                event_type="task_completed",
                thread_id=thread_id,
                user_id=entry.user_id,
                task_id=todo.id,
                data={
                    "visibility": "activity",
                    "error": True,
                    "error_message": str(e)[:200],
                    "content": f"Task failed: {str(e)[:200]}",
                    "todo_id": todo.id,
                },
            )

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
