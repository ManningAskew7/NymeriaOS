"""Self-modification tool for Nymeria."""

import json
import logging
from typing import Any, Dict, Literal

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _reload_tools_internal() -> tuple[bool, str]:
    """
    Internal function to reload tools after self_modify.
    Returns (success, message).
    """
    from ..core import get_current_agent

    try:
        agent = get_current_agent()
        if agent is None:
            return False, "No active agent found"

        tool_names = agent.reload_tools()
        return True, f"Tools reloaded ({len(tool_names)} available)"

    except Exception as e:
        logger.error(f"_reload_tools_internal failed: {e}", exc_info=True)
        return False, str(e)


@tool
def self_modify(
    instruction: str,
    category: Literal["add_tool", "remove_tool", "fix_bug", "explain", "add_agent", "modify_agent", "remove_agent"],
) -> str:
    """
    Modify Nymeria's tools or agents. Only affects nymeria/tools/ and nymeria/agents/.
    Tools are automatically reloaded after successful modifications.

    Args:
        instruction: What to do (be specific)
        category: "add_tool", "remove_tool", "fix_bug", "explain", "add_agent", "modify_agent", "remove_agent"
    """
    logger.info(f"self_modify called: category={category}, instruction={instruction[:100]}...")

    # Import here to avoid circular imports
    from ..core.self_agent import SelfModifyAgent

    try:
        agent = SelfModifyAgent()
        result = agent.execute(instruction, category)

        # Auto-reload tools if modification was successful (not an explain or error)
        if category != "explain" and not result.startswith("[Error]"):
            success, reload_msg = _reload_tools_internal()
            if success:
                result += f"\n\n[Auto-reload]: {reload_msg}. New tools available on next message."
            else:
                result += f"\n\n[Warning]: Auto-reload failed: {reload_msg}. Use may need to restart."

        return result

    except Exception as e:
        logger.error(f"self_modify failed: {e}", exc_info=True)
        return f"[Error]: Self-modification failed: {str(e)}"


@tool
def self_modify_rollback(file_path: str) -> str:
    """
    Rollback a file to its previous version if self_modify broke something.

    Args:
        file_path: File to rollback (e.g., "nymeria/tools/my_tool.py")
    """
    logger.info(f"self_modify_rollback called: file_path={file_path}")

    from ..core.self_agent import SelfModifyAgent

    try:
        agent = SelfModifyAgent()
        result = agent.rollback_last(file_path)
        return result

    except Exception as e:
        logger.error(f"self_modify_rollback failed: {e}", exc_info=True)
        return f"[Error]: Rollback failed: {str(e)}"


# Export tools
# tools_reload and invoke_tool removed - self_modify now auto-reloads
SELF_MODIFY_TOOLS = [
    self_modify,
    self_modify_rollback,
]
