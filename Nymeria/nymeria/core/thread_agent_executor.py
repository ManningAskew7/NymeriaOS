"""Thread-based callable executor.

Delegates tasks to callable threads via NymeriaAgent.chat(), replacing the old
SubAgentExecutor for agents that have been migrated to thread-based execution.

Key differences from SubAgentExecutor:
- Uses agent.chat() directly — honors all thread config (system prompt, tools, LLM)
- Conversation persists in SQLite/Postgres (not in-memory)
- Thread is visible in the UI
- No context var isolation needed (different thread_id prevents streaming leakage)
"""

import json
import logging

logger = logging.getLogger(__name__)

# Reuse the same error marker format as SubAgentExecutor for backward compat
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
    """Delegate a task to a callable thread via NymeriaAgent.chat().

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

    # Check required env vars (from the agent template, if one exists)
    from ..agents import AVAILABLE_AGENTS
    import os
    template = AVAILABLE_AGENTS.get(callable_name)
    if template:
        required = template.get("required_env_vars", [])
        missing = [var for var in required if not os.environ.get(var)]
        if missing:
            return _build_error_result(
                code="missing_env_vars",
                message=f"Missing environment variables for {callable_name}: {', '.join(missing)}",
                callable_name=callable_name,
            )

    try:
        logger.info(f"ThreadExecutor: invoking {callable_name} (thread={thread_id})")
        response = agent.chat(
            message=task,
            thread_id=thread_id,
            user_id=caller_user_id,
        )
        return response
    except Exception as e:
        logger.error(f"ThreadExecutor: {callable_name} failed: {e}", exc_info=True)
        return _build_error_result(
            code="thread_execution_failed",
            message=f"{callable_name} execution failed: {str(e)}",
            callable_name=callable_name,
        )
