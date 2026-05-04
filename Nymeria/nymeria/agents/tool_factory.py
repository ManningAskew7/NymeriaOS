"""Factory for creating LangChain tools from callable thread configurations.

Callable threads replace the old sub-agent system. Any thread with callable=True
gets a tool wrapper so it can be invoked directly (e.g., MyAgent(task="...")).
"""

import logging
from typing import Annotated, List, Literal, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, tool as tool_decorator

logger = logging.getLogger(__name__)


def create_callable_thread_tool(thread_config) -> BaseTool:
    """Create a LangChain tool from a callable thread config.

    The generated tool wraps the thread executor and routes calls through
    NymeriaAgent.chat() to a persistent callable thread.

    Args:
        thread_config: ThreadConfig with callable=True

    Returns:
        A BaseTool that delegates to the callable thread
    """
    name = thread_config.callable_name
    thread_id = thread_config.thread_id
    description = thread_config.callable_description or f"Invoke the {name} thread"

    tool_description = f"""{description}

This is a specialized thread with its own tools and context. Use mode="ask" to
delegate and wait for the final answer, or mode="handoff" to transfer work to
the target thread without waiting for its final output. In handoff mode, the
target thread responds through its own autonomous output channels.
"""

    # Capture in closure
    _name = name
    _thread_id = thread_id

    @tool_decorator(_name, return_direct=False)
    def callable_thread_tool_func(
        task: str,
        mode: Literal["ask", "handoff"] = "ask",
        scheduled_for: Optional[str] = None,
        if_busy: Literal["queue", "error"] = "queue",
        *,
        config: Annotated[RunnableConfig, InjectedToolArg],
    ) -> str:
        """Invoke this thread with a task.

        Args:
            task: A clear description of what you want this thread to do. Be specific about the goal and any constraints.
            mode: "ask" waits for this thread's final answer and returns it to you. "handoff" starts autonomous work in this thread and returns only a dispatch receipt.
            scheduled_for: For mode="handoff" only, delay execution until a time such as "30s", "5m", "1h", "1d", or "YYYY-MM-DD HH:MM". Omit for immediate handoff.
            if_busy: For handoff mode, "queue" waits for the target thread lock in the background; "error" returns immediately if the target thread is busy. For ask mode, "error" performs a best-effort busy check before waiting.
        """
        from langchain_core.runnables.config import var_child_runnable_config
        from langchain_core.tracers.context import tracing_v2_callback_var, run_collector_var
        from ..core.thread_agent_executor import (
            handoff as thread_handoff,
            invoke as thread_invoke,
        )

        user_id = config.get("configurable", {}).get("user_id", "default") if config else "default"
        parent_thread_id = config.get("configurable", {}).get("thread_id") if config else None

        logger.info(f"{_name} callable thread tool called: mode={mode}, task={task[:100]}...")

        if mode not in ("ask", "handoff"):
            return "[Error]: mode must be 'ask' or 'handoff'."
        if if_busy not in ("queue", "error"):
            return "[Error]: if_busy must be 'queue' or 'error'."
        if mode == "ask" and scheduled_for and scheduled_for.strip():
            return "[Error]: scheduled_for is only supported when mode='handoff'."

        # Register parent→child relationship for cascading abort
        from ..core.agent import get_current_agent
        _agent = get_current_agent()

        # Cross-user ownership gate: the global tool registry is shared
        # across users (sync_agent_tools rebuilds one default graph), so a
        # callable thread created by user A would otherwise be invocable
        # from user B's chat. Reject mismatched ownership at runtime.
        # Admins are allowed through (X-Nymeria-Act-As impersonation and
        # service-token routing keep working) AND admins can also touch
        # legacy unowned callables for migration. Non-admin invocation of an
        # unowned callable is rejected — the per-user graph filter usually
        # prevents the binding from existing in the first place, but if it
        # leaked via a stale cache or guessed name, fail closed here.
        if _agent and user_id:
            try:
                owner = _agent.accounts_repo.get_thread_owner(_thread_id)
                caller = _agent.accounts_repo.get_user_by_id(user_id)
                caller_is_admin = bool(caller and caller.role == "admin")
                if not caller_is_admin:
                    if owner is None:
                        logger.warning(
                            f"{_name}: invocation blocked — legacy unowned callable "
                            f"thread {_thread_id} can only be invoked by an admin"
                        )
                        return (
                            f"[Error]: {_name} is not available to this user. "
                            f"Callable threads are scoped to the user who created them."
                        )
                    if owner != user_id:
                        logger.warning(
                            f"{_name}: invocation blocked — caller user_id={user_id} "
                            f"does not own callable thread {_thread_id} (owned by {owner})"
                        )
                        return (
                            f"[Error]: {_name} is not available to this user. "
                            f"Callable threads are scoped to the user who created them."
                        )
            except Exception as e:  # noqa: BLE001
                # Fail closed: this is an auth boundary, not a best-effort
                # lookup. If ownership can't be verified, refuse rather than
                # let an unverified caller through.
                logger.warning(f"{_name}: ownership check failed: {e}")
                return (
                    f"[Error]: {_name} ownership could not be verified. "
                    f"Refusing invocation."
                )

        if _agent and parent_thread_id and hasattr(_agent, "is_callable_visible_to_thread"):
            if not _agent.is_callable_visible_to_thread(parent_thread_id, _thread_id):
                return (
                    f"[Error]: {_name} is not in this thread's callable team. "
                    "Use a callable thread from the same team or update the team's membership."
                )

        # Detect circular calls for blocking asks: if the target thread is an
        # ancestor waiting for this thread's output, invoking it would deadlock.
        # Handoffs do not wait, so a child can hand work back to its caller.
        if mode == "ask" and _agent and parent_thread_id:
            if _agent.is_ancestor_invocation(parent_thread_id, _thread_id):
                logger.warning(
                    f"{_name} circular call blocked: thread {parent_thread_id} "
                    f"tried to call {_thread_id} which is waiting for its response"
                )
                return (
                    f"[Error]: {_name} is currently waiting for YOUR response. "
                    f"Do not call a thread that invoked you — just return your "
                    f"final answer directly to complete your turn."
                )

        if mode == "ask" and if_busy == "error" and _agent:
            if _agent._thread_locks.is_thread_busy(_thread_id):
                return (
                    f"[Busy]: {_name} is busy on thread '{_thread_id}'. "
                    "Ask again later or retry with if_busy='queue'."
                )

        registered_invocation = False
        if mode == "ask" and _agent and parent_thread_id:
            _agent.register_callable_invocation(parent_thread_id, _thread_id)
            registered_invocation = True

        # Resolve parent thread name for trigger metadata
        trigger_override = None
        parent_name = None
        if _agent and parent_thread_id:
            # Try thread title first
            try:
                parent_meta = _agent.thread_metadata_manager.get_thread(user_id, parent_thread_id)
                if parent_meta and parent_meta.title:
                    parent_name = parent_meta.title
            except Exception:
                logger.debug("Failed to resolve parent thread title")
            # Fall back to callable_name if parent is itself a callable thread
            if not parent_name:
                try:
                    parent_tc = _agent.thread_config_manager.get_config(parent_thread_id)
                    if parent_tc and parent_tc.callable_name:
                        parent_name = parent_tc.callable_name
                except Exception:
                    logger.debug("Failed to resolve parent callable name")
            # Last resort: raw thread ID
            if not parent_name:
                parent_name = parent_thread_id
            trigger_override = f'Thread("{parent_thread_id}", "{parent_name}")'

        # Break the LangChain callback/tracing inheritance chain so the inner
        # graph.invoke() doesn't propagate LLM token events back to the
        # parent's astream_events() — prevents stream leakage.
        config_token = var_child_runnable_config.set(None)
        callback_token = tracing_v2_callback_var.set(None)
        collector_token = run_collector_var.set(None)
        try:
            if mode == "handoff":
                return thread_handoff(
                    _thread_id,
                    task,
                    user_id,
                    _name,
                    caller_thread_id=parent_thread_id,
                    caller_name=parent_name,
                    trigger_override=trigger_override,
                    scheduled_for=scheduled_for,
                    if_busy=if_busy,
                )
            return thread_invoke(
                _thread_id,
                task,
                user_id,
                _name,
                trigger_override=trigger_override,
            )
        except Exception as e:
            logger.error(f"{_name} callable thread failed: {e}", exc_info=True)
            return f"[Error]: {_name} invocation failed: {str(e)}"
        finally:
            run_collector_var.reset(collector_token)
            tracing_v2_callback_var.reset(callback_token)
            var_child_runnable_config.reset(config_token)
            # Unregister parent→child (child is done or failed)
            if registered_invocation and _agent and parent_thread_id:
                _agent.unregister_callable_invocation(parent_thread_id, _thread_id)

    callable_thread_tool_func.description = tool_description
    return callable_thread_tool_func


def get_callable_thread_tools(thread_config_manager) -> List[BaseTool]:
    """Get tools for all callable threads.

    Scans all callable=True thread configs and creates a LangChain tool
    for each one.

    Args:
        thread_config_manager: ThreadConfigManager instance

    Returns:
        List of BaseTool instances for callable threads
    """
    tools = []
    for tc in thread_config_manager.list_callable_threads():
        if not tc.callable_name:
            continue
        try:
            tool = create_callable_thread_tool(tc)
            tools.append(tool)
            logger.debug(f"Created callable thread tool: {tc.callable_name} -> {tc.thread_id}")
        except Exception as e:
            logger.error(f"Failed to create callable thread tool for {tc.callable_name}: {e}")
    return tools
