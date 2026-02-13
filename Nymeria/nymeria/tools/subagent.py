"""Sub-agent management tools.

Sub-agents are invoked directly as tools (e.g., BrowserAgent(task="Go to google.com")).
This module provides utilities for managing agent context, reloading definitions,
and rolling back self-modifications.
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
def reload_all() -> str:
    """
    Reload all tools, agents, and trigger sources.

    Use this after making manual changes to tool or agent files, or after
    SelfModifyAgent has created/modified code. This reloads all Python modules,
    refreshes agent registrations, and rebuilds graphs.

    NOTE: Due to how LangGraph works, newly created tools are NOT available
    in the same conversation turn. They will work on the next user message.

    Returns:
        Number of tools and trigger sources loaded
    """
    logger.info("reload_all called")

    from ..core.agent import get_current_agent

    try:
        agent = get_current_agent()
        if agent is None:
            return "[Error]: No active agent found. Cannot reload."

        # reload_tools() handles everything: tool modules, agents, agent tools, graphs
        tool_names = agent.reload_tools()

        # Also reload trigger sources
        source_count = 0
        try:
            from ..triggers.sources import reload_sources
            source_count = reload_sources()
        except Exception as e:
            logger.warning(f"Trigger source reload failed: {e}")

        return (
            f"[Success]: Reloaded {len(tool_names)} tools, {source_count} trigger source(s).\n"
            f"New tools will be available on the next message."
        )
    except Exception as e:
        logger.error(f"reload_all failed: {e}", exc_info=True)
        return f"[Error]: Failed to reload: {str(e)}"


@tool
def self_modify_rollback(file_path: str) -> str:
    """
    Rollback a file to its previous version if a self-modification broke something.

    Every file written by SelfModifyAgent is automatically backed up. This tool
    restores the most recent backup for the given file.

    Args:
        file_path: File to rollback (e.g., "nymeria/tools/my_tool.py")
    """
    logger.info(f"self_modify_rollback called: file_path={file_path}")

    from ..core.self_agent import SelfModifyAgent

    try:
        agent = SelfModifyAgent()
        return agent.rollback_last(file_path)
    except Exception as e:
        logger.error(f"self_modify_rollback failed: {e}", exc_info=True)
        return f"[Error]: Rollback failed: {str(e)}"


# Export tools
SUBAGENT_TOOLS = [
    clear_agent_context,
    reload_all,
    self_modify_rollback,
]
