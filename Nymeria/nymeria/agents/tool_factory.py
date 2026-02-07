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

This is a sub-agent with specialized capabilities. Pass your task/instruction
and it will execute autonomously using its own tools and context.
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
        """Placeholder docstring - replaced below."""
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
