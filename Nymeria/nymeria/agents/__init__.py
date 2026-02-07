"""Sub-agents created by Nymeria.

This module provides the registration system for sub-agents.
Sub-agents are specialized assistants that handle specific domains
(email, calendar, etc.) with their own tools and context.

Sub-agents can be invoked in two ways:
1. Directly as tools: BrowserAgent(task="Go to google.com")
2. Via wrapper: sub_agent("BrowserAgent", "Go to google.com")

The direct tool approach is preferred as it makes agents discoverable
in the tool list alongside other tools.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Registry of available sub-agents
# Key: agent name, Value: agent config dict
AVAILABLE_AGENTS: Dict[str, Dict[str, Any]] = {}


def register_agent(name: str, config: Dict[str, Any]) -> None:
    """
    Register a sub-agent.

    Args:
        name: Unique name for the agent
        config: Agent configuration dictionary containing:
            - name: Display name
            - description: What the agent does
            - system_prompt: The agent's system prompt
            - tools: List of tool functions
            - context_turns: Number of conversation turns to remember (3-10)
            - required_env_vars: List of required environment variable names
            - allowed_tools: List of global tool names this agent can use
            - llm_provider: Optional LLM provider override ("anthropic", "openai", "openrouter")
            - llm_model: Optional model name override
            - llm_temperature: Optional temperature override (0.0-2.0)
    """
    if name in AVAILABLE_AGENTS:
        logger.warning(f"Re-registering agent '{name}' (replacing existing)")

    AVAILABLE_AGENTS[name] = config
    logger.info(f"Registered sub-agent: {name}")


def unregister_agent(name: str) -> bool:
    """
    Unregister a sub-agent.

    Args:
        name: Name of the agent to unregister

    Returns:
        True if agent was unregistered, False if not found
    """
    if name in AVAILABLE_AGENTS:
        del AVAILABLE_AGENTS[name]
        logger.info(f"Unregistered sub-agent: {name}")
        return True
    return False


def get_agent(name: str) -> Optional[Dict[str, Any]]:
    """
    Get a sub-agent configuration by name.

    Args:
        name: Name of the agent

    Returns:
        Agent config dict or None if not found
    """
    return AVAILABLE_AGENTS.get(name)


def list_agents() -> Dict[str, Dict[str, Any]]:
    """
    Get all registered sub-agents.

    Returns:
        Dictionary of all registered agents
    """
    return AVAILABLE_AGENTS.copy()


def reload_agents() -> int:
    """
    Reload all agents by re-importing agent modules.

    This clears the registry and re-imports all agent files
    in the agents directory.

    Returns:
        Number of agents loaded
    """
    import importlib
    import sys
    from pathlib import Path

    # Clear existing registrations
    AVAILABLE_AGENTS.clear()

    # Get agents directory
    agents_dir = Path(__file__).parent

    # Find and import all agent modules (excluding __init__, tools files, and tool_factory)
    count = 0
    for py_file in agents_dir.glob("*.py"):
        if py_file.name.startswith("_") or py_file.name.endswith("_tools.py") or py_file.name == "tool_factory.py":
            continue

        module_name = f"nymeria.agents.{py_file.stem}"

        # Remove from cache if already loaded
        if module_name in sys.modules:
            del sys.modules[module_name]

        try:
            importlib.import_module(module_name)
            count += 1
            logger.debug(f"Loaded agent module: {module_name}")
        except Exception as e:
            logger.error(f"Failed to load agent module {module_name}: {e}")

    logger.info(f"Reloaded {count} agent modules, {len(AVAILABLE_AGENTS)} agents registered")
    return len(AVAILABLE_AGENTS)


# Auto-load agents on import
def _auto_load_agents():
    """Auto-load all agents in the directory on import."""
    from pathlib import Path
    import importlib

    agents_dir = Path(__file__).parent

    for py_file in agents_dir.glob("*.py"):
        if py_file.name.startswith("_") or py_file.name.endswith("_tools.py") or py_file.name == "tool_factory.py":
            continue

        module_name = f"nymeria.agents.{py_file.stem}"
        try:
            importlib.import_module(module_name)
            logger.info(f"Auto-loaded agent module: {module_name}")
        except Exception as e:
            logger.warning(f"Failed to auto-load {module_name}: {e}", exc_info=True)


# Run auto-load
_auto_load_agents()


# Export tool factory functions
from .tool_factory import (
    create_agent_tool,
    get_agent_tools,
    refresh_agent_tools,
    clear_agent_tools_cache,
)
