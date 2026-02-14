"""Nymeria tools module.

Consolidated tool set for maximum autonomy with minimal complexity.
Sub-agents are exposed as directly callable tools (e.g., BrowserAgent).
Use get_all_tools_with_agents() to get ALL_TOOLS combined with agent tools.

Changes from original:
- notify: Unified from telegram_notify, discord_notify, slack_notify
- todo_complete: Removed, use todo_update(status="done")
- sub_agent, list_agents: Removed, agents are direct tools
- self_modify: Removed, SelfModifyAgent is now a direct sub-agent tool
- reload_all: Replaces reload_agents + auto-reload from self_modify
- memory_list: Removed, memories are auto-injected
- rag_settings: Removed, configure via UI
"""

from .bash import bash_execute
from .filesystem import file_read, file_write, file_list
from .web import web_search
from .think import think, THINK_TOOLS
from .claude_code import claude_code
from .memory import (
    memory_save,
    memory_forget,
    memory_clear_all,
    personality_set,
    rag_search,
    MEMORY_TOOLS,
)
from .todo import (
    todo_add,
    todo_update,
    todo_delete,
    todo_list,
    TODO_TOOLS,
)
from .subagent import (
    clear_agent_context,
    reload_all,
    self_modify_rollback,
    SUBAGENT_TOOLS,
)
from .visibility import mute_response
from .outlook_auth import AUTH_TOOLS
from .outlook_email import EMAIL_TOOLS

# Combined Outlook tools list (used by OutlookAgent)
OUTLOOK_TOOLS = AUTH_TOOLS + EMAIL_TOOLS
# Optional tools — available for per-thread enabling but NOT loaded by default.
# Maps tool name -> tool object. Users enable these via thread config UI.
OPTIONAL_TOOLS = {t.name: t for t in OUTLOOK_TOOLS}
from .browser import BROWSER_TOOLS
from .notify import notify, NOTIFY_TOOLS
from .triggers import (
    trigger_create,
    trigger_list,
    trigger_update,
    trigger_delete,
    TRIGGER_TOOLS,
)

# All available tools (20 core tools + sub-agents as direct tools)
ALL_TOOLS = [
    # Core system tools
    bash_execute,
    file_read,
    file_write,
    file_list,
    web_search,
    think,
    claude_code,
    # Memory tools (memories are auto-injected into system prompt)
    memory_save,
    memory_forget,
    memory_clear_all,
    personality_set,
    rag_search,
    # TODO tools (task tracking + scheduling for autonomous operation)
    todo_add,
    todo_update,  # Use status="done" to complete
    todo_delete,
    todo_list,
    # Sub-agent management + self-modification utilities
    clear_agent_context,
    reload_all,
    self_modify_rollback,
    # Visibility control
    mute_response,
    # Unified notification tool
    notify,
    # Trigger tools (event-driven automation)
    trigger_create,
    trigger_list,
    trigger_update,
    trigger_delete,
    # NOTE: Outlook tools handled by OutlookAgent sub-agent
    # NOTE: Browser tools handled by BrowserAgent sub-agent
]

__all__ = [
    # Core tools
    "bash_execute",
    "file_read",
    "file_write",
    "file_list",
    "web_search",
    "think",
    "THINK_TOOLS",
    "claude_code",
    # Memory tools
    "memory_save",
    "memory_forget",
    "memory_clear_all",
    "personality_set",
    "rag_search",
    "MEMORY_TOOLS",
    # TODO tools
    "todo_add",
    "todo_update",
    "todo_delete",
    "todo_list",
    "TODO_TOOLS",
    # Sub-agent tools + self-modification utilities
    "clear_agent_context",
    "reload_all",
    "self_modify_rollback",
    "SUBAGENT_TOOLS",
    # Visibility control
    "mute_response",
    # Notification
    "notify",
    "NOTIFY_TOOLS",
    # Trigger tools
    "trigger_create",
    "trigger_list",
    "trigger_update",
    "trigger_delete",
    "TRIGGER_TOOLS",
    # Outlook tools (handled by OutlookAgent, exported for reference)
    "AUTH_TOOLS",
    "EMAIL_TOOLS",
    "OUTLOOK_TOOLS",
    # Optional tools (per-thread enabling)
    "OPTIONAL_TOOLS",
    # Browser tools (handled by BrowserAgent, exported for reference)
    "BROWSER_TOOLS",
    # Main exports
    "ALL_TOOLS",
    "get_all_tools_with_agents",
]


def get_all_tools_with_agents() -> list:
    """
    Get ALL_TOOLS combined with dynamically generated agent tools.

    This function returns the complete list of tools including:
    - Static tools defined in ALL_TOOLS (20 core tools)
    - Dynamically generated tools for each registered sub-agent
      (BrowserAgent, OutlookAgent, SelfModifyAgent)

    Returns:
        List of all tools including agent tools
    """
    from ..agents import get_agent_tools

    # Start with static tools
    all_tools = list(ALL_TOOLS)

    # Add agent tools
    agent_tools = get_agent_tools()
    all_tools.extend(agent_tools)

    return all_tools
