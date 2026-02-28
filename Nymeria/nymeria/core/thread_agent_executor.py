"""Thread-based callable executor.

Delegates tasks to callable threads via NymeriaAgent.stream(), replacing the old
SubAgentExecutor for agents that have been migrated to thread-based execution.

Key differences from SubAgentExecutor:
- Uses agent.stream() directly — honors all thread config (system prompt, tools, LLM)
- Publishes live events to the event bus so the frontend can stream callable thread activity
- Conversation persists in SQLite/Postgres (not in-memory)
- Thread is visible in the UI
"""

import json
import logging
from uuid import uuid4

from .event_bus import publish_autonomous_event

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


def invoke(thread_id: str, task: str, caller_user_id: str, callable_name: str) -> str:
    """Delegate a task to a callable thread via NymeriaAgent.stream().

    Streams events in real-time to the event bus so the frontend can display
    live thinking, tool calls, and responses for the callable thread.

    Args:
        thread_id: The thread_id of the callable thread to invoke
        task: The task description to send
        caller_user_id: The user_id of the caller (for profile access)
        callable_name: Display name for error messages

    Returns:
        The thread's response string
    """
    from .agent import get_current_agent

    agent = get_current_agent()
    if agent is None:
        return _build_error_result(
            code="agent_not_initialized",
            message=f"Cannot invoke {callable_name}: NymeriaAgent not initialized.",
            callable_name=callable_name,
        )

    # Verify the target thread is callable
    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None or not tc.callable:
        return _build_error_result(
            code="not_callable_thread",
            message=f"Thread {thread_id} is not callable.",
            callable_name=callable_name,
        )

    task_id = f"callable-{callable_name}-{uuid4().hex[:8]}"

    import time as _time
    _start = _time.monotonic()

    try:
        logger.info(
            f"[CALLABLE] === START === name={callable_name}, thread={thread_id}, "
            f"task_id={task_id}, user={caller_user_id}, task={task[:80]}..."
        )

        # Publish task_started immediately so frontend can enter streaming mode
        publish_autonomous_event(
            event_type="task_started",
            thread_id=thread_id,
            user_id=caller_user_id,
            task_id=task_id,
            data={"prompt": task, "callable_name": callable_name},
        )

        response_parts = []
        thinking_parts = []
        chunk_count = 0
        tool_call_count = 0
        iteration_limit_hit = False

        for chunk in agent.stream(
            message=task,
            thread_id=thread_id,
            user_id=caller_user_id,
            _is_self_invoke=False,
        ):
            chunk_type = chunk.get("type")
            chunk_count += 1

            if chunk_type == "tool_call":
                tool_call_count += 1
                logger.debug(f"[CALLABLE] {callable_name}: tool_call #{tool_call_count} name={chunk.get('name')}")
                publish_autonomous_event(
                    event_type="tool_call",
                    thread_id=thread_id,
                    user_id=caller_user_id,
                    task_id=task_id,
                    data={
                        "id": chunk.get("id"),
                        "name": chunk.get("name"),
                        "args": chunk.get("args", {}),
                    },
                )

            elif chunk_type == "tool_result":
                result_preview = str(chunk.get("result", ""))[:100]
                logger.debug(f"[CALLABLE] {callable_name}: tool_result name={chunk.get('name')}, result={result_preview}...")
                publish_autonomous_event(
                    event_type="tool_result",
                    thread_id=thread_id,
                    user_id=caller_user_id,
                    task_id=task_id,
                    data={
                        "id": chunk.get("id"),
                        "name": chunk.get("name"),
                        "result": chunk.get("result"),
                    },
                )

            elif chunk_type == "thinking":
                content = chunk.get("content", "")
                if content:
                    thinking_parts.append(content)
                publish_autonomous_event(
                    event_type="thinking",
                    thread_id=thread_id,
                    user_id=caller_user_id,
                    task_id=task_id,
                    data={"content": content},
                )

            elif chunk_type == "response":
                content = chunk.get("content", "")
                if content:
                    response_parts.append(content)
                publish_autonomous_event(
                    event_type="response",
                    thread_id=thread_id,
                    user_id=caller_user_id,
                    task_id=task_id,
                    data={"content": content},
                )

            elif chunk_type == "error":
                # Stream-level error (e.g. lock timeout, LLM failure)
                content = chunk.get("content", "")
                logger.warning(f"[CALLABLE] {callable_name}: stream error: {content}")
                raise RuntimeError(content or f"{callable_name} encountered a stream error")

            elif chunk_type == "iteration_limit":
                iteration_limit_hit = True
                logger.warning(
                    f"[CALLABLE] {callable_name}: hit iteration limit "
                    f"(scope={chunk.get('scope')}, "
                    f"max_iterations={chunk.get('max_iterations')})"
                )

        # Compute final response text
        if response_parts:
            response_text = "".join(response_parts)
        elif thinking_parts:
            # No response chunks — reclassify thinking as response
            response_text = "".join(thinking_parts)
        else:
            response_text = ""

        # Annotate response if iteration limit was hit so the parent LLM
        # knows the callable's work may be incomplete
        if iteration_limit_hit and response_text:
            response_text += (
                f"\n\n[Note: This response may be incomplete — "
                f"{callable_name} was stopped after reaching its iteration limit.]"
            )
        elif iteration_limit_hit and not response_text:
            response_text = (
                f"[{callable_name} hit its iteration limit without producing "
                f"a response. The task may require manual follow-up.]"
            )

        _elapsed = _time.monotonic() - _start
        logger.info(
            f"[CALLABLE] === END === name={callable_name}, thread={thread_id}, "
            f"task_id={task_id}, chunks={chunk_count}, tools={tool_call_count}, "
            f"response_len={len(response_text)}, partial={iteration_limit_hit}, "
            f"elapsed={_elapsed:.1f}s"
        )

        # Publish task_completed with final response
        completed_data = {
            "content": response_text,
            "callable_name": callable_name,
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

        return response_text

    except Exception as e:
        _elapsed = _time.monotonic() - _start
        logger.error(
            f"[CALLABLE] === ERROR === name={callable_name}, thread={thread_id}, "
            f"task_id={task_id}, elapsed={_elapsed:.1f}s: {e}",
            exc_info=True,
        )

        # Always publish task_completed on error so frontend exits streaming state
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
            },
        )

        return _build_error_result(
            code="thread_execution_failed",
            message=f"{callable_name} execution failed: {str(e)}",
            callable_name=callable_name,
        )
