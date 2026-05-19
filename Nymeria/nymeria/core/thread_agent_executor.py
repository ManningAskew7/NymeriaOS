"""Thread-based callable executor.

Delegates tasks to callable threads via NymeriaAgent.astream(), replacing the old
SubAgentExecutor for agents that have been migrated to thread-based execution.

Key differences from SubAgentExecutor:
- Uses the same async agent stream as chat — honors all thread config (system prompt, tools, LLM)
- Publishes live events to the event bus so the frontend can stream callable thread activity
- Conversation persists in SQLite/Postgres (not in-memory)
- Thread is visible in the UI
"""

import json
import logging
import threading
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import uuid4

from .event_bus import publish_agent_stream_chunk, publish_autonomous_event
from .stream_bridge import stream_and_collect

logger = logging.getLogger(__name__)

ERROR_MARKER_PREFIX = "[NymeriaSubAgentError]"


def _build_error_result(code: str, message: str, **metadata) -> str:
    """Encode a machine-readable error marker plus a human-readable message."""
    payload = {
        "code": code,
        "message": message,
        "metadata": metadata,
    }
    marker = f"{ERROR_MARKER_PREFIX}{json.dumps(payload, ensure_ascii=True, default=str)}"
    return f"{marker}\n[Error]: {message}"


def _verify_callable_target(agent, thread_id: str, callable_name: str) -> Optional[str]:
    """Return an encoded error if the target thread cannot be invoked."""
    if agent is None:
        return _build_error_result(
            code="agent_not_initialized",
            message=f"Cannot invoke {callable_name}: NymeriaAgent not initialized.",
            callable_name=callable_name,
        )

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None or not tc.callable:
        return _build_error_result(
            code="not_callable_thread",
            message=f"Thread {thread_id} is not callable.",
            callable_name=callable_name,
        )
    return None


def _format_handoff_prompt(
    *,
    task: str,
    handoff_id: str,
    caller_thread_id: Optional[str],
    caller_name: Optional[str],
    callable_name: str,
) -> str:
    """Wrap a handoff task with routing context for the receiving thread."""
    caller_thread = caller_thread_id or "unknown"
    caller_display = caller_name or caller_thread
    return (
        "[Handoff Metadata]\n"
        f"handoff_id: {handoff_id}\n"
        f"source_thread_id: {caller_thread}\n"
        f"source_thread_name: {caller_display}\n"
        f"target_callable_name: {callable_name}\n"
        "mode: handoff\n"
        "The source thread will not automatically receive your final answer. "
        "If it is useful, call the source thread after you finish.\n"
        "[/Handoff Metadata]\n\n"
        f"{task}"
    )


def _run_callable_stream(
    *,
    agent,
    thread_id: str,
    task: str,
    caller_user_id: str,
    callable_name: str,
    task_id: str,
    trigger_override: Optional[str],
    is_self_invoke: bool,
    event_metadata: Optional[Dict[str, Any]] = None,
    log_label: str = "CALLABLE",
) -> str:
    """Run a callable thread through astream and publish autonomous events."""
    import time as _time

    _start = _time.monotonic()
    event_metadata = dict(event_metadata or {})

    try:
        logger.info(
            f"[{log_label}] === START === name={callable_name}, thread={thread_id}, "
            f"task_id={task_id}, user={caller_user_id}, task={task[:80]}..."
        )

        # Refresh the temporary-lifetime idle clock for the thread being invoked.
        # The owner may differ from the caller (cross-user callables), so we
        # resolve the owner from the accounts repo before updating metadata.
        try:
            from ..tools.spawn_thread import refresh_thread_activity

            owner_id = None
            get_owner = getattr(agent.accounts_repo, "get_thread_owner", None)
            if callable(get_owner):
                owner_id = get_owner(thread_id)
            refresh_thread_activity(
                agent, owner_id or caller_user_id, thread_id
            )
        except Exception:
            logger.debug(
                "Activity refresh failed for callable target",
                exc_info=True,
            )

        started_published = False

        def handle_chunk(chunk: Dict[str, Any], collection) -> None:
            nonlocal started_published
            if not started_published and chunk.get("type") not in ("queued", "prompt_queued"):
                publish_autonomous_event(
                    event_type="task_started",
                    thread_id=thread_id,
                    user_id=caller_user_id,
                    task_id=task_id,
                    data={
                        "prompt": task,
                        "callable_name": callable_name,
                        **event_metadata,
                    },
                )
                started_published = True

            chunk_type = chunk.get("type")
            publish_agent_stream_chunk(
                chunk,
                thread_id=thread_id,
                user_id=caller_user_id,
                task_id=task_id,
            )

            if chunk_type == "tool_call":
                logger.debug(
                    f"[{log_label}] {callable_name}: tool_call "
                    f"#{collection.tool_call_count} name={chunk.get('name')}"
                )

            elif chunk_type == "tool_result":
                result_preview = str(chunk.get("result", ""))[:100]
                logger.debug(
                    f"[{log_label}] {callable_name}: tool_result "
                    f"name={chunk.get('name')}, result={result_preview}..."
                )

            elif chunk_type == "error":
                content = chunk.get("content", "")
                logger.warning(f"[{log_label}] {callable_name}: stream error: {content}")

            elif chunk_type == "iteration_limit":
                logger.warning(
                    f"[{log_label}] {callable_name}: hit iteration limit "
                    f"(scope={chunk.get('scope')}, "
                    f"reason={chunk.get('reason')}, "
                    f"max_iterations={chunk.get('max_iterations')})"
                )

        def stream_error_message(chunk: Dict[str, Any]) -> str:
            content = chunk.get("content", "")
            return content or f"{callable_name} encountered a stream error"

        result = stream_and_collect(
            agent,
            astream_kwargs={
                "message": task,
                "thread_id": thread_id,
                "user_id": caller_user_id,
                "_is_self_invoke": is_self_invoke,
                "_trigger_override": trigger_override,
                "source": "callable",
                "source_id": task_id,
                "source_label": callable_name,
            },
            on_chunk=handle_chunk,
            error_message_factory=stream_error_message,
        )

        response_text = result.response_text()
        return_response_text = response_text
        iteration_limit_hit = result.iteration_limit_hit
        iteration_limit_event = result.iteration_limit_event

        if iteration_limit_hit and response_text:
            limit_message = (
                iteration_limit_event.get("content")
                if isinstance(iteration_limit_event, dict)
                else None
            )
            response_text += (
                f"\n\n[Note: This response may be incomplete -- "
                f"{limit_message or f'{callable_name} was stopped by a turn safety limit.'}]"
            )
            return_response_text = response_text
        elif iteration_limit_hit and not response_text:
            limit_message = (
                iteration_limit_event.get("content")
                if isinstance(iteration_limit_event, dict)
                else None
            )
            response_text = (
                f"[{limit_message or f'{callable_name} hit a turn safety limit without producing a response.'} "
                "The task may require manual follow-up.]"
            )
            return_response_text = response_text

        if iteration_limit_hit:
            metadata = {
                "agent_name": callable_name,
                "max_iterations": (
                    iteration_limit_event.get("max_iterations")
                    if isinstance(iteration_limit_event, dict)
                    else None
                ),
                "tool_call_count": (
                    iteration_limit_event.get("tool_call_count")
                    if isinstance(iteration_limit_event, dict)
                    else result.tool_call_count
                ),
                "reason": (
                    iteration_limit_event.get("reason")
                    if isinstance(iteration_limit_event, dict)
                    else "max_iterations"
                ),
                "repeated_tool_name": (
                    iteration_limit_event.get("repeated_tool_name")
                    if isinstance(iteration_limit_event, dict)
                    else None
                ),
                "repeated_count": (
                    iteration_limit_event.get("repeated_count")
                    if isinstance(iteration_limit_event, dict)
                    else None
                ),
            }
            message = (
                iteration_limit_event.get("content")
                if isinstance(iteration_limit_event, dict)
                else f"{callable_name} was stopped by a turn safety limit."
            )
            payload = {
                "code": "subagent_iteration_limit",
                "message": message,
                "metadata": metadata,
            }
            marker = f"{ERROR_MARKER_PREFIX}{json.dumps(payload, ensure_ascii=True, default=str)}"
            return_response_text = f"{marker}\n{return_response_text}"

        _elapsed = _time.monotonic() - _start
        logger.info(
            f"[{log_label}] === END === name={callable_name}, thread={thread_id}, "
            f"task_id={task_id}, chunks={result.chunk_count}, tools={result.tool_call_count}, "
            f"response_len={len(response_text)}, partial={iteration_limit_hit}, "
            f"elapsed={_elapsed:.1f}s"
        )

        completed_data = {
            "content": response_text,
            "callable_name": callable_name,
            **event_metadata,
        }
        if iteration_limit_hit:
            completed_data["partial"] = True

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=caller_user_id,
            task_id=task_id,
            data=completed_data,
        )

        return return_response_text

    except Exception as e:
        _elapsed = _time.monotonic() - _start
        logger.error(
            f"[{log_label}] === ERROR === name={callable_name}, thread={thread_id}, "
            f"task_id={task_id}, elapsed={_elapsed:.1f}s: {e}",
            exc_info=True,
        )

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=caller_user_id,
            task_id=task_id,
            data={
                "error": True,
                "error_message": str(e)[:200],
                "content": f"Task failed: {str(e)[:200]}",
                "callable_name": callable_name,
                **event_metadata,
            },
        )

        return _build_error_result(
            code="thread_execution_failed",
            message=f"{callable_name} execution failed: {str(e)}",
            callable_name=callable_name,
        )


def invoke(thread_id: str, task: str, caller_user_id: str, callable_name: str,
           trigger_override: str | None = None) -> str:
    """Delegate a task to a callable thread via NymeriaAgent.astream().

    Streams events in real-time to the event bus so the frontend can display
    live thinking, tool calls, and responses for the callable thread.

    Args:
        thread_id: The thread_id of the callable thread to invoke
        task: The task description to send
        caller_user_id: The user_id of the caller (for profile access)
        callable_name: Display name for error messages
        trigger_override: If provided, use as the trigger label in time context metadata

    Returns:
        The thread's response string
    """
    from .agent import get_current_agent

    agent = get_current_agent()
    target_error = _verify_callable_target(agent, thread_id, callable_name)
    if target_error:
        return target_error
    task_id = f"callable-{callable_name}-{uuid4().hex[:8]}"
    return _run_callable_stream(
        agent=agent,
        thread_id=thread_id,
        task=task,
        caller_user_id=caller_user_id,
        callable_name=callable_name,
        task_id=task_id,
        trigger_override=trigger_override,
        is_self_invoke=False,
        log_label="CALLABLE",
    )


def handoff(
    thread_id: str,
    task: str,
    caller_user_id: str,
    callable_name: str,
    *,
    caller_thread_id: Optional[str] = None,
    caller_name: Optional[str] = None,
    trigger_override: Optional[str] = None,
    scheduled_for: Optional[str] = None,
    if_busy: str = "queue",
) -> str:
    """Hand off work to a callable thread without returning its final output."""
    from .agent import get_current_agent

    agent = get_current_agent()
    target_error = _verify_callable_target(agent, thread_id, callable_name)
    if target_error:
        return target_error

    if if_busy not in ("queue", "error"):
        return "[Error]: if_busy must be 'queue' or 'error'."

    handoff_id = f"handoff-{uuid4().hex[:8]}"
    handoff_prompt = _format_handoff_prompt(
        task=task,
        handoff_id=handoff_id,
        caller_thread_id=caller_thread_id,
        caller_name=caller_name,
        callable_name=callable_name,
    )

    metadata = {
        "source": "handoff",
        "handoff_id": handoff_id,
        "caller_thread_id": caller_thread_id,
        "caller_thread_name": caller_name,
    }

    if scheduled_for and scheduled_for.strip():
        return _schedule_handoff(
            agent=agent,
            thread_id=thread_id,
            task=task,
            handoff_prompt=handoff_prompt,
            caller_user_id=caller_user_id,
            callable_name=callable_name,
            handoff_id=handoff_id,
            caller_thread_id=caller_thread_id,
            caller_name=caller_name,
            scheduled_for=scheduled_for.strip(),
        )

    if agent is None:
        return "[Error]: NymeriaAgent not initialized."
    if if_busy == "error" and agent._thread_locks.is_thread_busy(thread_id):
        return (
            f"[Busy]: {callable_name} is busy on thread '{thread_id}'. "
            "Ask again later or retry with if_busy='queue'."
        )

    task_id = f"{handoff_id}-{callable_name}"

    worker = threading.Thread(
        target=_run_callable_stream,
        name=f"NymeriaHandoff-{callable_name}-{handoff_id[-8:]}",
        daemon=True,
        kwargs={
            "agent": agent,
            "thread_id": thread_id,
            "task": handoff_prompt,
            "caller_user_id": caller_user_id,
            "callable_name": callable_name,
            "task_id": task_id,
            "trigger_override": trigger_override,
            "is_self_invoke": True,
            "event_metadata": metadata,
            "log_label": "HANDOFF",
        },
    )
    worker.start()

    return (
        f"[HandedOff]: handoff_id={handoff_id} target_thread_id={thread_id} "
        "scheduled=immediate. The target thread will handle this through its "
        "own autonomous output channels; no final response will be returned here."
    )


def _schedule_handoff(
    *,
    agent,
    thread_id: str,
    task: str,
    handoff_prompt: str,
    caller_user_id: str,
    callable_name: str,
    handoff_id: str,
    caller_thread_id: Optional[str],
    caller_name: Optional[str],
    scheduled_for: str,
) -> str:
    """Create a scheduled TODO on the target thread for delayed handoff."""
    from .activity_log import ActivityType, log_activity
    from .time_utils import parse_scheduled_time

    scheduled_label = scheduled_for
    if scheduled_label.lower() == "now":
        scheduled_label = "30s"

    todo_scheduled = parse_scheduled_time(scheduled_label)
    if not todo_scheduled:
        return (
            f"[Error]: Invalid scheduled_for '{scheduled_for}'. Use '30s', "
            "'17m', '1h', '1d', '1w', 'YYYY-MM-DD HH:MM', an ISO datetime, "
            "or omit scheduled_for for immediate handoff."
        )

    todo_task = f"Handoff {handoff_id} from {caller_name or caller_thread_id or 'unknown'}: {task}"
    todo_task = todo_task[:500]
    notes = handoff_prompt[:1000]

    with agent.todo_manager.atomic_update(caller_user_id) as todo_list:
        item = todo_list.add_item(
            todo_task,
            scheduled_for=todo_scheduled,
            thread_id=thread_id,
            notes=notes,
        )
        if not item:
            return (
                f"[Error]: TODO limit reached. Complete or delete some tasks "
                f"on thread '{thread_id}' first."
            )

        schedule_db = getattr(agent, "_schedule_db", None)
        if schedule_db:
            schedule_db.add_scheduled(
                todo_id=item.id,
                user_id=caller_user_id,
                scheduled_for=todo_scheduled,
                task_preview=todo_task[:100],
                thread_id=thread_id,
            )

    try:
        log_activity(
            ActivityType.SELF_INVOKE,
            f"Scheduled handoff to {callable_name}: {task[:80]}",
            user_id=caller_user_id,
            thread_id=caller_thread_id,
            metadata={
                "todo_id": item.id,
                "handoff_id": handoff_id,
                "target_thread_id": thread_id,
                "target_callable_name": callable_name,
                "scheduled_for": scheduled_label,
            },
        )
    except Exception:
        logger.debug("Activity logging failed for handoff")

    when = (
        todo_scheduled.isoformat(timespec="seconds")
        if isinstance(todo_scheduled, datetime)
        else str(todo_scheduled)
    )
    return (
        f"[HandedOff]: handoff_id={handoff_id} target_thread_id={thread_id} "
        f"todo_id={item.id} scheduled_for={scheduled_label} ({when}). "
        "The target thread will handle this when the schedule fires; no final "
        "response will be returned here."
    )
