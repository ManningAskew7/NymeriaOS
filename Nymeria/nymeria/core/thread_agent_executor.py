"""Thread-based callable executor.

Runs callable threads through NymeriaAgent.astream(): ``request`` (the
callable-tool path, backlog #357: the callee answers through
``reply_to_thread``, routed by ``core/thread_requests.py``) and ``invoke`` (the
synchronous form kept for the ``nym.thread`` workflow verb, whose script is
its own waiter).

- Uses the same async agent stream as chat: honours all thread config (system prompt, tools, LLM)
- Publishes live events to the event bus so the frontend can stream callable thread activity
- Conversation persists in SQLite/Postgres (not in-memory)
- Thread is visible in the UI
"""

import contextvars
import json
import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple
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
    progress_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> str:
    """Run a callable thread through astream and publish autonomous events.

    ``progress_sink`` (a request's progress observer) sees every chunk as it
    streams so a ``wait_for_reply`` status can say what the thread has done.
    """
    _start = time.monotonic()
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

            owner_id: str | None = None
            get_owner = getattr(agent.accounts_repo, "get_thread_owner", None)
            if callable(get_owner):
                raw_owner = get_owner(thread_id)
                owner_id = str(raw_owner) if raw_owner else None
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
                    # A marked first chunk means the target thread was busy
                    # and this call became a queuer mirroring the holder's
                    # turn (stream_bridge fanout marker); stamp the
                    # lifecycle so consumers can skip the mirror task.
                    data={
                        "prompt": task,
                        "callable_name": callable_name,
                        **({"fanout": True} if chunk.get("fanout") else {}),
                        **event_metadata,
                    },
                )
                started_published = True

            chunk_type = chunk.get("type")
            if progress_sink is not None:
                try:
                    progress_sink(chunk)
                except Exception:  # noqa: BLE001 - a snapshot must never break the stream
                    logger.debug("progress sink failed", exc_info=True)
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

        _elapsed = time.monotonic() - _start
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
        if result.fanout_observed:
            # The content above was fanned in from the holder turn this
            # call's prompt was absorbed into; consumers must not deliver
            # it a second time (the holder's own task delivers it).
            completed_data["fanout"] = True

        publish_autonomous_event(
            event_type="task_completed",
            thread_id=thread_id,
            user_id=caller_user_id,
            task_id=task_id,
            data=completed_data,
        )

        return return_response_text

    except Exception as e:
        _elapsed = time.monotonic() - _start
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
                # A fanned-in holder error is still a mirror; the latch
                # rides the raised exception (stream_bridge stamps it).
                **(
                    {"fanout": True}
                    if getattr(e, "fanout_observed", False)
                    else {}
                ),
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


# ---------------------------------------------------------------------------
# Requests: every callable-thread call is a request (backlog #357).
#
# The caller's tool call records an open request in ``core.thread_requests``
# and returns a ``[Requested]`` receipt at once; the callee runs on a worker
# thread (or is queued behind its current turn) and answers ONLY through the
# ``reply_to_thread`` tool, which the ledger routes back to the caller exactly
# once (inline to a ``wait_for_reply`` waiter, else a wake-up prompt through
# ``core.completion_delivery``). Nothing the callee's turn prints is delivered
# implicitly. The earlier blocking "ask" (the callee's STREAM as the answer,
# ``[StillWorking]`` on overrun) is gone: it could not say whose answer a turn
# was once a second caller, a redirect, or a sub-task shared the turn.
#
# ``invoke`` above stays for ``nym.thread`` (a workflow script is its own
# waiter and has no thread to wake).
# ---------------------------------------------------------------------------


def _format_request_task(*, task: str, req) -> str:
    from .thread_requests import format_request_prompt

    return format_request_prompt(task=task, req=req)


def target_can_reply(agent, thread_id: str, *, fallback_user_id: Optional[str] = None) -> bool:
    """Best-effort: does the target thread's effective tool set include
    ``reply_to_thread``? Advisory (the receipt and the request prompt warn
    when it does not); an unreadable config counts as able."""
    from .thread_requests import REPLY_TOOL_NAME

    try:
        tc = agent.thread_config_manager.get_config(thread_id)
        if tc is not None:
            if REPLY_TOOL_NAME in (tc.disabled_tools or []):
                return False
            if REPLY_TOOL_NAME in (tc.enabled_tools or []) or REPLY_TOOL_NAME in (
                getattr(tc, "temporary_tools", None) or {}
            ):
                return True
        owner = None
        get_owner = getattr(agent.accounts_repo, "get_thread_owner", None)
        if callable(get_owner):
            owner = get_owner(thread_id)
        profile = agent.profile_manager.get_profile(owner or fallback_user_id or "default")
        from ..tools import resolve_default_tool_names

        names = resolve_default_tool_names(profile.tool_preferences.default_thread_tools)
        return REPLY_TOOL_NAME in names
    except Exception:  # noqa: BLE001 - advisory only
        logger.debug("target_can_reply: lookup failed for %s", thread_id, exc_info=True)
        return True


def request(
    thread_id: str,
    task: str,
    caller_user_id: str,
    callable_name: str,
    *,
    caller_thread_id: str,
    caller_name: Optional[str] = None,
    trigger_override: Optional[str] = None,
    scheduled_for: Optional[str] = None,
    if_busy: str = "queue",
    wait_seconds: int = 0,
) -> Tuple[str, Optional[Any]]:
    """Send ``task`` to a callable thread as a request. Returns ``(receipt,
    request)``; ``request`` is None when nothing was dispatched (an error or a
    scheduled request, whose receipt names the todo). ``wait_seconds`` > 0 also
    waits that long (clamped) for the reply, with the waiter registered BEFORE
    the callee is dispatched, and appends the outcome (the reply inline, or a
    ``[Waiting]`` status) to the receipt."""
    from .agent import get_current_agent
    from .thread_requests import (
        begin_wait,
        clamp_wait,
        discard_request,
        finish_wait,
        open_request,
        request_receipt,
    )

    agent = get_current_agent()
    target_error = _verify_callable_target(agent, thread_id, callable_name)
    if target_error:
        return target_error, None
    if agent is None:
        return "[Error]: NymeriaAgent not initialized.", None
    if if_busy not in ("queue", "error"):
        return "[Error]: if_busy must be 'queue' or 'error'.", None
    if not caller_thread_id:
        return (
            "[Error]: a request needs a calling thread to reply to (no thread "
            "context on this call).",
            None,
        )
    if thread_id == caller_thread_id:
        return (
            f"[Error]: {callable_name} is this thread; a thread cannot request "
            "work from itself. Do the task in this turn instead.",
            None,
        )

    scheduled = bool(scheduled_for and scheduled_for.strip())
    if scheduled:
        return (
            _schedule_request(
                agent=agent,
                thread_id=thread_id,
                task=task,
                caller_user_id=caller_user_id,
                callable_name=callable_name,
                caller_thread_id=caller_thread_id,
                caller_name=caller_name,
                scheduled_for=(scheduled_for or "").strip(),
            ),
            None,
        )

    busy = agent._thread_locks.is_thread_busy(thread_id)
    if if_busy == "error" and busy:
        return (
            f"[Busy]: {callable_name} is busy on thread '{thread_id}'. "
            "Ask again later or retry with if_busy='queue'.",
            None,
        )

    req = open_request(
        caller_thread_id=caller_thread_id,
        caller_user_id=caller_user_id,
        caller_name=caller_name,
        target_thread_id=thread_id,
        callable_name=callable_name,
        task=task,
        target_can_reply=target_can_reply(agent, thread_id, fallback_user_id=caller_user_id),
    )
    req.task_id = f"{req.id}-{callable_name}"
    prompt = _format_request_task(task=task, req=req)

    # A call that waits registers its waiter before the callee is dispatched:
    # a reply landing in the gap is then handed inline, not sent as a wake-up
    # prompt the caller would meet twice.
    seconds = clamp_wait(wait_seconds)
    waiter = begin_wait(req, caller_thread_id, agent) if seconds > 0 else None

    # Context copy: the hook engine's re-entrance depth is a ContextVar and a
    # bare thread reads it back as zero (``core/hooks/dispatch.py``). The
    # caller's LangChain callback resets ride along too, so the callee stream
    # stays isolated from the caller's.
    worker = threading.Thread(
        target=contextvars.copy_context().run,
        args=(_run_request_worker,),
        name=f"NymeriaRequest-{callable_name}-{req.id[-8:]}",
        daemon=True,
        kwargs={
            "req": req,
            "agent": agent,
            "thread_id": thread_id,
            "task": prompt,
            "caller_user_id": caller_user_id,
            "callable_name": callable_name,
            "task_id": req.task_id,
            "trigger_override": trigger_override,
            "is_self_invoke": True,
            "event_metadata": {
                "source": "request",
                "request_id": req.id,
                "caller_thread_id": caller_thread_id,
                "caller_thread_name": caller_name,
            },
            "log_label": "REQUEST",
            "progress_sink": req.progress.observe,
        },
    )
    try:
        worker.start()
    except Exception:
        # The call never happened: the caller's own tool result reports it,
        # so nothing is owed, waited on, nudged, or expired.
        discard_request(req)
        raise
    receipt = request_receipt(req, queued=busy)
    if waiter is None:
        return receipt, req
    if isinstance(waiter, str):
        # The wait could not be registered (a mutual wait, or a closed record);
        # the request itself went out.
        return f"{receipt}\n\n{waiter}", req
    # The caller is actively waiting: its /stop cascades into the target for
    # the duration of the wait (a request nobody waits on is the target's own
    # work and is not aborted by the caller's stop).
    registered = False
    try:
        agent.register_callable_invocation(caller_thread_id, thread_id)
        registered = True
    except Exception:  # noqa: BLE001 - the cascade edge is best-effort
        logger.debug("request wait: invocation registration failed", exc_info=True)
    try:
        outcome = finish_wait(req, waiter, seconds=seconds, agent=agent)
    finally:
        if registered:
            try:
                agent.unregister_callable_invocation(caller_thread_id, thread_id)
            except Exception:  # noqa: BLE001
                logger.debug("request wait: invocation unregistration failed", exc_info=True)
    return f"{receipt}\n\n{outcome}", req


def _run_request_worker(*, req, agent, **stream_kwargs) -> None:
    """The request's worker: run the callee's turn; if that turn died before it
    could reply (an execution error, encoded by ``_run_callable_stream`` with
    ``ERROR_MARKER_PREFIX``), close the request as failed so the caller hears
    now rather than at the nudge. A turn that merely ended without replying is
    the turn-end reminder's job, not this one's."""
    from .thread_requests import fail_request

    result = _run_callable_stream(agent=agent, **stream_kwargs)
    if isinstance(result, str) and result.startswith(ERROR_MARKER_PREFIX):
        _, _, human = result.partition("\n")
        fail_request(req, human.replace("[Error]: ", "", 1).strip() or "execution failed", agent)


def _dispatch_without_caller(
    thread_id: str,
    task: str,
    caller_user_id: str,
    callable_name: str,
    *,
    trigger_override: Optional[str],
    scheduled_for: Optional[str],
    if_busy: str,
) -> str:
    """Fire-and-forget for a call with NO calling thread (a headless workflow's
    ``nym.thread`` handoff): the bare task, no ledger record (nothing could be
    replied to), a receipt saying so."""
    from .agent import get_current_agent

    agent = get_current_agent()
    target_error = _verify_callable_target(agent, thread_id, callable_name)
    if target_error:
        return target_error
    if agent is None:
        return "[Error]: NymeriaAgent not initialized."
    if if_busy not in ("queue", "error"):
        return "[Error]: if_busy must be 'queue' or 'error'."
    if scheduled_for and scheduled_for.strip():
        return _schedule_request(
            agent=agent,
            thread_id=thread_id,
            task=task,
            caller_user_id=caller_user_id,
            callable_name=callable_name,
            caller_thread_id=None,
            caller_name=None,
            scheduled_for=scheduled_for.strip(),
        )
    busy = agent._thread_locks.is_thread_busy(thread_id)
    if if_busy == "error" and busy:
        return (
            f"[Busy]: {callable_name} is busy on thread '{thread_id}'. "
            "Ask again later or retry with if_busy='queue'."
        )
    task_id = f"handoff-{uuid4().hex[:8]}"
    threading.Thread(
        target=contextvars.copy_context().run,
        args=(_run_callable_stream,),
        name=f"NymeriaHandoff-{callable_name}-{task_id[-8:]}",
        daemon=True,
        kwargs={
            "agent": agent,
            "thread_id": thread_id,
            "task": task,
            "caller_user_id": caller_user_id,
            "callable_name": callable_name,
            "task_id": task_id,
            "trigger_override": trigger_override,
            "is_self_invoke": True,
            "event_metadata": {"source": "handoff"},
            "log_label": "HANDOFF",
        },
    ).start()
    how = "queued behind its current turn" if busy else "working on it"
    return (
        f"[Dispatched]: {callable_name} is {how} on thread '{thread_id}' "
        f"(task_id={task_id}). There is no calling thread to reply to, so "
        "nothing is returned here; it reports through its own channels."
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
    """Receipt-only request, for the ``nym.thread`` verb's ``mode="handoff"``
    (a workflow script never waits). With a calling thread the callee's
    ``reply_to_thread`` wakes that thread; without one (a headless workflow)
    the task is dispatched bare and the callee reports through its own
    channels, as before requests existed."""
    if not caller_thread_id:
        return _dispatch_without_caller(
            thread_id,
            task,
            caller_user_id,
            callable_name,
            trigger_override=trigger_override,
            scheduled_for=scheduled_for,
            if_busy=if_busy,
        )
    receipt, _ = request(
        thread_id,
        task,
        caller_user_id,
        callable_name,
        caller_thread_id=caller_thread_id or "",
        caller_name=caller_name,
        trigger_override=trigger_override,
        scheduled_for=scheduled_for,
        if_busy=if_busy,
    )
    return receipt


def _schedule_request(
    *,
    agent,
    thread_id: str,
    task: str,
    caller_user_id: str,
    callable_name: str,
    caller_thread_id: Optional[str],
    caller_name: Optional[str],
    scheduled_for: str,
) -> str:
    """Create a scheduled TODO on the target thread for a delayed request.

    The ledger record is written now, clocked from the scheduled time
    (``scheduled_for``): nothing is owed, reminded, nudged, or expired before
    the TODO is due. The TODO's own task text carries the task; its note
    carries the ``[Request Metadata]`` block (the ticker prompts with both),
    so neither is truncated by the other inside the note's cap. With no
    calling thread (a headless workflow) there is no record: the TODO runs
    the bare task and the callee reports through its own channels.
    """
    from .activity_log import ActivityType, log_activity
    from .thread_requests import format_request_block, open_request
    from .time_utils import parse_scheduled_time

    scheduled_label = scheduled_for
    if scheduled_label.lower() == "now":
        scheduled_label = "30s"

    todo_scheduled = parse_scheduled_time(scheduled_label)
    if not todo_scheduled:
        return (
            f"[Error]: Invalid scheduled_for '{scheduled_for}'. Use '30s', "
            "'17m', '1h', '1d', '1w', 'YYYY-MM-DD HH:MM', an ISO datetime, "
            "or omit scheduled_for for an immediate request."
        )

    due_epoch: Optional[float] = None
    if isinstance(todo_scheduled, datetime):
        try:
            due_epoch = todo_scheduled.timestamp()
        except (OverflowError, OSError, ValueError):
            due_epoch = None

    req = None
    notes: Optional[str] = None
    if caller_thread_id:
        req = open_request(
            caller_thread_id=caller_thread_id,
            caller_user_id=caller_user_id,
            caller_name=caller_name,
            target_thread_id=thread_id,
            callable_name=callable_name,
            task=task,
            target_can_reply=target_can_reply(agent, thread_id, fallback_user_id=caller_user_id),
            scheduled_for=due_epoch if due_epoch is not None else time.time(),
        )
        notes = format_request_block(req)[:1000]
        todo_task = f"Request {req.id} from {caller_name or caller_thread_id}: {task}"[:500]
    else:
        todo_task = f"Handoff from a workflow: {task}"[:500]

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
            f"Scheduled request to {callable_name}: {task[:80]}",
            user_id=caller_user_id,
            thread_id=caller_thread_id,
            metadata={
                "todo_id": item.id,
                "request_id": req.id if req is not None else None,
                "target_thread_id": thread_id,
                "target_callable_name": callable_name,
                "scheduled_for": scheduled_label,
            },
        )
    except Exception:
        logger.debug("Activity logging failed for scheduled request")

    when = (
        todo_scheduled.isoformat(timespec="seconds")
        if isinstance(todo_scheduled, datetime)
        else str(todo_scheduled)
    )
    if req is None:
        return (
            f"[Dispatched]: target_thread_id={thread_id} todo_id={item.id} "
            f"scheduled_for={scheduled_label} ({when}). {callable_name} handles "
            "this when the schedule fires; there is no calling thread to reply "
            "to, so it reports through its own channels."
        )
    return (
        f"[Requested]: request_id={req.id} target_thread_id={thread_id} "
        f"todo_id={item.id} scheduled_for={scheduled_label} ({when}). "
        f"{callable_name} handles this when the schedule fires and replies with "
        "reply_to_thread; the reply arrives as a prompt on this thread."
    )
