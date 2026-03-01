"""Nymeria tools module.

Consolidated tool set for maximum autonomy with minimal complexity.
Callable threads replace the old sub-agent system — any thread can become
a callable tool with its own system prompt, LLM config, and tool set.

Use get_all_tools_with_agents() for backward compatibility (returns ALL_TOOLS).
Callable thread tools are added per-graph in _build_graph_with_prompt(), not globally.
"""

from .bash import bash_execute
from .filesystem import file_read, file_write
from .web import web_search
from .think import consult, CONSULT_TOOLS
from .claude_code import claude_code
from .memory import (
    memory_save,
    memory_forget,
    memory_list,
    personality_set,
    rag_search,
    MEMORY_TOOLS,
)
from .todo import (
    todo,
    todo_delete,
    todo_list,
    TODO_TOOLS,
)
from .subagent import (
    reload_all,
    self_modify_rollback,
    SUBAGENT_TOOLS,
)
from .outlook_auth import AUTH_TOOLS
from .outlook_email import EMAIL_TOOLS
from .browser import BROWSER_TOOLS
from .calendar import CALENDAR_TOOLS
from .notify import notify, NOTIFY_TOOLS
from .triggers import (
    trigger_create,
    trigger_list,
    trigger_update,
    trigger_delete,
    TRIGGER_TOOLS,
)
from .hello_test import hello_test
from .sticky_note import sticky_note, STICKY_NOTE_TOOLS

# Combined Outlook tools list
OUTLOOK_TOOLS = AUTH_TOOLS + EMAIL_TOOLS

# Self-modify tools (from core/self_agent.py)
from ..core.self_agent import SELF_AGENT_TOOLS

# Optional tools — available for per-thread enabling but NOT loaded by default.
# Maps tool name -> tool object. Users enable these via thread config UI.
OPTIONAL_TOOLS = {t.name: t for t in (
    OUTLOOK_TOOLS
    + TRIGGER_TOOLS
    + BROWSER_TOOLS
    + CALENDAR_TOOLS
    + SELF_AGENT_TOOLS
    + SUBAGENT_TOOLS
)}

# All available tools
ALL_TOOLS = [
    # Core system tools
    bash_execute,
    file_read,
    file_write,
    web_search,
    consult,
    claude_code,
    # Memory tools
    memory_save,
    memory_forget,
    memory_list,
    personality_set,
    rag_search,
    # TODO tools
    todo,
    todo_delete,
    todo_list,
    # Sticky note checklist
    sticky_note,
    # Unified notification tool
    notify,
    # Test tool
    hello_test,
]

__all__ = [
    "bash_execute",
    "file_read",
    "file_write",
    "web_search",
    "consult",
    "CONSULT_TOOLS",
    "claude_code",
    "memory_save",
    "memory_forget",
    "memory_list",
    "personality_set",
    "rag_search",
    "MEMORY_TOOLS",
    "todo",
    "todo_delete",
    "todo_list",
    "TODO_TOOLS",
    "reload_all",
    "self_modify_rollback",
    "SUBAGENT_TOOLS",
    "notify",
    "NOTIFY_TOOLS",
    "trigger_create",
    "trigger_list",
    "trigger_update",
    "trigger_delete",
    "TRIGGER_TOOLS",
    "AUTH_TOOLS",
    "EMAIL_TOOLS",
    "OUTLOOK_TOOLS",
    "BROWSER_TOOLS",
    "CALENDAR_TOOLS",
    "SELF_AGENT_TOOLS",
    "OPTIONAL_TOOLS",
    "ALL_TOOLS",
    "get_all_tools_with_agents",
    "hello_test",
    "sticky_note",
    "STICKY_NOTE_TOOLS",
]


def get_all_tools_with_agents() -> list:
    """
    Get ALL_TOOLS list.

    Kept for backward compatibility. Callable thread tools are now added
    per-graph in _build_graph_with_prompt(), not globally.

    Returns:
        List of core tools
    """
    return list(ALL_TOOLS)
