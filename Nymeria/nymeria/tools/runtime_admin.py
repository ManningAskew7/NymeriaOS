"""Runtime administration tools: full reload and self-modification rollback."""
from .registry import ToolGroup, register_tool_group

import logging
from typing import Tuple

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _do_full_reload() -> Tuple[int, int, int]:
    """Shared reload logic used by both reload_all and self_reload.

    Returns (tool_count, skill_count, source_count).
    Raises if no active agent is found.
    """
    from ..core.agent import get_current_agent

    agent = get_current_agent()
    if agent is None:
        raise RuntimeError("No active agent found. Cannot reload.")

    skill_count = 0
    if hasattr(agent, "skill_manager") and agent.skill_manager is not None:
        sm = agent.skill_manager
        sm.reload()
        with sm._lock:
            skill_count = (
                len(sm._bundled) + len(sm._global)
                + sum(len(v) for v in sm._per_user.values())
            )

    tool_names = agent.reload_tools()

    source_count = 0
    try:
        from ..triggers.sources import reload_sources
        source_count = reload_sources()
    except Exception as e:
        logger.warning(f"Trigger source reload failed: {e}")

    return len(tool_names), skill_count, source_count


@tool
def reload_all() -> str:
    """
    Reload all tools, skills, and trigger sources.

    Use this after making manual changes to tool files, creating/editing skills,
    or after self-modify tools have created/modified code. This rescans skill
    directories, reloads all Python tool modules, and rebuilds graphs.

    NOTE: Due to how LangGraph works, newly created tools are NOT available
    in the same conversation turn. They will work on the next user message.

    Returns:
        Number of tools, skills, and trigger sources loaded
    """
    logger.info("reload_all called")

    try:
        tool_count, skill_count, source_count = _do_full_reload()
        return (
            f"[Success]: Reloaded {tool_count} tools, {skill_count} skill(s) indexed, "
            f"{source_count} trigger source(s).\n"
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
    from ..core.self_agent import (
        resolve_in_writable_dir,
        self_edit_allowed,
        self_edit_disabled_error,
    )
    from ..config import get_settings

    try:
        settings = get_settings()
        project_root = settings.project_root

        # A restore is a WRITE to a caller-supplied destination, so it owes the
        # same two controls the write it undoes already paid (audit C12-01).
        # Without them this was the laundering route past every path policy in
        # the tree: an ordinary `file_write` to `backups/<...>/x` is allowed
        # (`backups` is not a secret store), and this call then copied that
        # content over any path the caller named, reaching the credential
        # stores, the global prompt overrides and the protected source dirs
        # that every other write path refuses.
        #
        # The writable allowlist is the right screen rather than a stack of
        # denials because it costs nothing: `create_backup` has exactly two
        # callers, `self_file_write` and `self_file_delete`, and both resolve
        # through this same helper first. So every backup that can exist is
        # already inside the allowlist, and a rollback target outside it can
        # only be a destination somebody chose, never one this tool produced.
        if not self_edit_allowed():
            return self_edit_disabled_error()

        path = resolve_in_writable_dir(file_path, project_root)
        if path is None:
            return (
                f"[Error]: Cannot roll back {file_path}: rollback restores the "
                "files the self-modification tools may write, and this path is "
                "not one of them. Writable: nymeria/tools/, nymeria/agents/, "
                "nymeria/triggers/sources/."
            )

        backup_manager = BackupManager(settings.backups_dir, project_root)
        success = backup_manager.restore_backup(path)
        if success:
            return f"[Success]: Rolled back {file_path} to previous version"
        else:
            return f"[Error]: No backup found for {file_path}"
    except Exception as e:
        logger.error(f"self_modify_rollback failed: {e}", exc_info=True)
        return f"[Error]: Rollback failed: {str(e)}"


RUNTIME_ADMIN_TOOLS = [
    reload_all,
    self_modify_rollback,
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="runtime_admin", tools=tuple(RUNTIME_ADMIN_TOOLS), admin_only=True))
