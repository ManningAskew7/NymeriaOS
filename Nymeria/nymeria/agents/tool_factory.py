"""Factory for creating LangChain tools from callable thread configurations.

Callable threads replace the old sub-agent system. Any thread with callable=True
gets a tool wrapper so it can be invoked directly (e.g., MyAgent(task="...")).
"""

import logging
from typing import Annotated, List

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

This is a specialized thread with its own tools and context. Provide a clear task
description and it will execute autonomously.
"""

    # Capture in closure
    _name = name
    _thread_id = thread_id

    @tool_decorator(_name, return_direct=False)
    def callable_thread_tool_func(
        task: str,
        *,
        config: Annotated[RunnableConfig, InjectedToolArg],
    ) -> str:
        """Invoke this thread with a task.

        Args:
            task: A clear description of what you want this thread to do. Be specific about the goal and any constraints.
        """
        from langchain_core.runnables.config import var_child_runnable_config
        from langchain_core.callbacks.manager import tracing_v2_callback_var
        from langchain_core.tracers.context import run_collector_var
        from ..core.thread_agent_executor import invoke as thread_invoke

        user_id = config.get("configurable", {}).get("user_id", "default") if config else "default"

        logger.info(f"{_name} callable thread tool called: task={task[:100]}...")

        # Break the LangChain callback/tracing inheritance chain so the inner
        # graph.invoke() doesn't propagate LLM token events back to the
        # parent's astream_events() — prevents stream leakage.
        config_token = var_child_runnable_config.set(None)
        callback_token = tracing_v2_callback_var.set(None)
        collector_token = run_collector_var.set(None)
        try:
            return thread_invoke(_thread_id, task, user_id, _name)
        except Exception as e:
            logger.error(f"{_name} callable thread failed: {e}", exc_info=True)
            return f"[Error]: {_name} invocation failed: {str(e)}"
        finally:
            run_collector_var.reset(collector_token)
            tracing_v2_callback_var.reset(callback_token)
            var_child_runnable_config.reset(config_token)

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
