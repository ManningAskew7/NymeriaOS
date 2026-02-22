"""Utility tools for reloading and rolling back self-modifications."""

import logging
from pathlib import Path

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
def reload_all() -> str:
    """
    Reload all tools and trigger sources.

    Use this after making manual changes to tool files or after
    self-modify tools have created/modified code. This reloads all Python modules
    and rebuilds graphs.

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

    Every file written by self-modify tools is automatically backed up. This tool
    restores the most recent backup for the given file.

    Args:
        file_path: File to rollback (e.g., "nymeria/tools/my_tool.py")
    """
    logger.info(f"self_modify_rollback called: file_path={file_path}")

    from ..core.backup import BackupManager
    from ..config import get_settings

    try:
        settings = get_settings()
        backup_manager = BackupManager(settings.backups_dir, settings.project_root)

        path = Path(file_path)
        if not path.is_absolute():
            path = settings.project_root / file_path

        success = backup_manager.restore_backup(path)
        if success:
            return f"[Success]: Rolled back {file_path} to previous version"
        else:
            return f"[Error]: No backup found for {file_path}"
    except Exception as e:
        logger.error(f"self_modify_rollback failed: {e}", exc_info=True)
        return f"[Error]: Rollback failed: {str(e)}"


# Export tools
SUBAGENT_TOOLS = [
    reload_all,
    self_modify_rollback,
]
