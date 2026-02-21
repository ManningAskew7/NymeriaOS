"""Factory for creating LangChain tools from sub-agent configurations.

This module enables sub-agents to appear directly in Nymeria's tool list,
allowing calls like `BrowserAgent(task="Go to google.com")` instead of
`sub_agent("BrowserAgent", "Go to google.com")`.
"""

import logging
from typing import Annotated, Any, Dict, List

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, tool as tool_decorator

logger = logging.getLogger(__name__)

# Cache of agent tools (rebuilt on reload_agents)
_agent_tools_cache: List[BaseTool] = []
_cache_initialized: bool = False


def create_agent_tool(agent_name: str, agent_config: Dict[str, Any]) -> BaseTool:
    """
    Create a LangChain tool from an agent configuration.

    The generated tool wraps SubAgentExecutor.invoke() and appears in
    the tool list alongside other tools like bash_execute, file_read, etc.

    Args:
        agent_name: Name of the agent (e.g., "BrowserAgent")
        agent_config: Agent configuration dict containing description, system_prompt, etc.

    Returns:
        A BaseTool that can be called directly
    """
    description = agent_config.get("description", f"Invoke the {agent_name} sub-agent")

    # Build a descriptive docstring for the tool
    tool_description = f"""{description}

This is a sub-agent with specialized capabilities. Provide a clear task
description and it will execute autonomously using its own tools and context.
"""

    # Capture agent_name in closure
    _agent_name = agent_name

    # Use the @tool decorator as a function to create the tool
    # This properly handles InjectedToolArg like the existing sub_agent tool
    @tool_decorator(_agent_name, return_direct=False)
    def agent_tool_func(
        task: str,
        *,
        config: Annotated[RunnableConfig, InjectedToolArg],
    ) -> str:
        """Invoke this sub-agent with a task.

        Args:
            task: A clear description of what you want the sub-agent to do. Be specific about the goal and any constraints.
        """
        from ..core.subagent_executor import SubAgentExecutor

        user_id = config.get("configurable", {}).get("user_id", "default") if config else "default"

        logger.info(f"{_agent_name} tool called: task={task[:100]}...")

        try:
            executor = SubAgentExecutor()
            return executor.invoke(_agent_name, task, user_id)
        except Exception as e:
            logger.error(f"{_agent_name} failed: {e}", exc_info=True)
            return f"[Error]: {_agent_name} invocation failed: {str(e)}"

    # Override the description with our custom one
    agent_tool_func.description = tool_description

    return agent_tool_func


def get_agent_tools() -> List[BaseTool]:
    """
    Get all agent tools (cached).

    Returns a list of LangChain tools, one for each registered sub-agent.
    The cache is initialized on first call and refreshed by refresh_agent_tools().

    Returns:
        List of BaseTool instances representing sub-agents
    """
    global _cache_initialized

    if not _cache_initialized:
        refresh_agent_tools()

    return _agent_tools_cache.copy()


def refresh_agent_tools() -> List[str]:
    """
    Rebuild the agent tools cache.

    Call this after reload_agents() to regenerate tools for any
    new, modified, or removed agents.

    Returns:
        List of agent names that now have tools
    """
    global _agent_tools_cache, _cache_initialized

    from . import AVAILABLE_AGENTS

    _agent_tools_cache.clear()
    agent_names = []

    for agent_name, agent_config in AVAILABLE_AGENTS.items():
        try:
            agent_tool = create_agent_tool(agent_name, agent_config)
            _agent_tools_cache.append(agent_tool)
            agent_names.append(agent_name)
            logger.debug(f"Created tool for agent: {agent_name}")
        except Exception as e:
            logger.error(f"Failed to create tool for agent {agent_name}: {e}")

    _cache_initialized = True
    logger.info(f"Refreshed agent tools: {agent_names}")
    return agent_names


def clear_agent_tools_cache() -> None:
    """
    Clear the agent tools cache.

    This forces a rebuild on the next get_agent_tools() call.
    """
    global _agent_tools_cache, _cache_initialized
    _agent_tools_cache.clear()
    _cache_initialized = False
    logger.debug("Cleared agent tools cache")


# ========================================================================
# Callable thread tools
# ========================================================================


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
        # Matches the isolation pattern used in SubAgentExecutor.
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
