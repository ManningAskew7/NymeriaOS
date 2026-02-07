"""Sub-agent management tools.

Sub-agents are invoked directly as tools (e.g., BrowserAgent(task="Go to google.com")).
This module provides utilities for managing agent context and reloading definitions.
"""

import logging
from typing import Annotated

from langchain_core.tools import tool, InjectedToolArg
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)


@tool
def clear_agent_context(agent_name: str) -> str:
    """
    Clear the conversation context for a sub-agent.

    Use this if you want the sub-agent to start fresh without remembering
    previous conversations.

    Args:
        agent_name: Name of the sub-agent

    Returns:
        Success or error message
    """
    logger.info(f"clear_agent_context called: agent_name={agent_name}")

    from ..core.subagent_executor import SubAgentExecutor

    try:
        executor = SubAgentExecutor()
        if executor.clear_context(agent_name):
            return f"[Success]: Cleared context for {agent_name}"
        else:
            return f"[Info]: No saved context found for {agent_name}"
    except Exception as e:
        logger.error(f"clear_agent_context failed: {e}", exc_info=True)
        return f"[Error]: Failed to clear context: {str(e)}"


@tool
def reload_agents() -> str:
    """
    Reload all sub-agents from the agents directory.

    Use this after creating or modifying a sub-agent with self_modify
    to make it available for use. After reloading, agents will appear
    as directly callable tools in the tool list.

    NOTE: Due to how LangGraph works, newly created agent tools are NOT
    available in the same conversation turn. They will work on the next
    user message.

    Returns:
        Number of agents loaded and their names
    """
    logger.info("reload_agents called")

    from ..agents import reload_agents as do_reload, refresh_agent_tools, AVAILABLE_AGENTS
    from ..core.agent import get_current_agent

    try:
        # Reload agent definitions
        count = do_reload()

        # Refresh agent tools cache (regenerates tools from updated configs)
        agent_tool_names = refresh_agent_tools()
        logger.info(f"Refreshed agent tools: {agent_tool_names}")

        # Trigger graph rebuild in NymeriaAgent if available
        current_agent = get_current_agent()
        if current_agent:
            # Re-register agent tools with the tool registry
            from ..agents import get_agent_tools
            agent_tools = get_agent_tools()
            for tool in agent_tools:
                current_agent.tool_registry.register(tool)

            # Clear cached graphs to pick up new tools
            current_agent._user_graphs.clear()
            current_agent._async_user_graphs.clear()
            current_agent._default_graph = current_agent._build_graph_with_prompt(
                current_agent._base_system_prompt
            )
            current_agent._default_async_graph = current_agent._build_async_graph_with_prompt(
                current_agent._base_system_prompt
            )
            logger.info("Rebuilt agent graphs with new tools")

        if count > 0:
            names = ", ".join(AVAILABLE_AGENTS.keys())
            return (
                f"[Success]: Loaded {count} agent(s): {names}\n"
                f"These agents are now available as tools and will work on the next message."
            )
        else:
            return "[Info]: No agents found in nymeria/agents/"
    except Exception as e:
        logger.error(f"reload_agents failed: {e}", exc_info=True)
        return f"[Error]: Failed to reload agents: {str(e)}"


# Export tools
# sub_agent and list_agents removed - agents are now direct tools (e.g., BrowserAgent)
SUBAGENT_TOOLS = [
    clear_agent_context,
    reload_agents,
]
