"""Self-modification tools for Nymeria.

These tools allow Nymeria to read, write, delete, and test its own code.
Optional tools — enable per-thread via thread config.
"""

import json
import logging
from pathlib import Path
from typing import Annotated, List

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, InjectedToolArg, tool

from .backup import BackupManager
from .validator import CodeValidator
from ..config import get_settings

logger = logging.getLogger(__name__)


# The three below are the self-edit POLICY, and they are public because the
# capability is wider than this module. `tools/runtime_admin.py` holds two of
# the same family (`self_modify_rollback` writes source, `reload_all` applies
# it), and audit finding C12-01 was exactly that: the gate had been applied per
# MODULE rather than per capability, so the rollback tool wrote source with the
# kill switch off and reached directories every other write path refuses. A
# helper two modules enforce is not module-private.
def self_edit_allowed() -> bool:
    settings = get_settings()
    return bool(getattr(settings, "nymeria_allow_self_edit", False))


def self_edit_disabled_error() -> str:
    return (
        "[Error]: Self-modification writes are disabled. Set "
        "NYMERIA_ALLOW_SELF_EDIT=true and enable these admin-only tools only "
        "for a trusted maintenance thread."
    )


# Directories self_file_write / self_file_delete may modify, relative to the
# project root. This is the single source of truth for the writable allowlist;
# read/list use the broader project-root containment check instead.
_WRITABLE_SUBDIRS: tuple[tuple[str, ...], ...] = (
    ("nymeria", "tools"),
    ("nymeria", "agents"),
    ("nymeria", "triggers", "sources"),
)


def _canonical_path(file_path: str, project_root: Path) -> Path:
    """Resolve ``file_path`` to an absolute, symlink-canonical path.

    Relative inputs are taken against ``project_root``. ``.resolve()`` runs
    last so the containment checks below see a path with ``..`` segments and
    symlinks already collapsed: this ordering is the traversal/symlink guard.
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = project_root / file_path
    return path.resolve()


def _resolve_in_project(file_path: str, project_root: Path) -> "Path | None":
    """Return the resolved path if it stays within ``project_root``, else None."""
    path = _canonical_path(file_path, project_root)
    try:
        path.relative_to(project_root)
    except ValueError:
        return None
    return path


def _writable_dirs(project_root: Path) -> tuple[Path, ...]:
    """The absolute writable allowlist, in (tools, agents, trigger_sources) order."""
    return tuple(project_root.joinpath(*parts) for parts in _WRITABLE_SUBDIRS)


def resolve_in_writable_dir(file_path: str, project_root: Path) -> "Path | None":
    """Return the resolved path if it lands inside a writable dir, else None."""
    path = _canonical_path(file_path, project_root)
    for allowed_dir in _writable_dirs(project_root):
        try:
            path.relative_to(allowed_dir)
            return path
        except ValueError:
            continue
    return None


# Self-modification tools (optional — enabled per-thread)
@tool
def self_file_read(file_path: str) -> str:
    """
    Read a file from the Nymeria codebase.

    Args:
        file_path: Relative path from the project root (e.g., "nymeria/tools/my_tool.py") or absolute path

    Returns:
        File contents or error message
    """
    settings = get_settings()
    project_root = settings.project_root

    # Security check: must be within project
    path = _resolve_in_project(file_path, project_root)
    if path is None:
        return f"[Error]: Access denied. File must be within {project_root}"

    if not path.exists():
        return f"[Error]: File not found: {file_path}"

    if not path.is_file():
        return f"[Error]: Not a file: {file_path}"

    try:
        content = path.read_text(encoding="utf-8")
        return content
    except Exception as e:
        return f"[Error]: Failed to read file: {e}"


@tool
def self_file_write(file_path: str, content: str) -> str:
    """
    Write content to a file in the tools or agents directory.

    IMPORTANT: Only files in nymeria/tools/ or nymeria/agents/ can be modified.

    Args:
        file_path: Relative path from the project root (e.g., "nymeria/tools/my_tool.py") or absolute path
        content: Content to write

    Returns:
        Success or error message
    """
    if not self_edit_allowed():
        return self_edit_disabled_error()

    settings = get_settings()
    project_root = settings.project_root
    tools_dir, agents_dir, trigger_sources_dir = _writable_dirs(project_root)

    # Security check: must be within tools, agents, or trigger sources directory
    path = resolve_in_writable_dir(file_path, project_root)
    if path is None:
        return f"[Error]: Access denied. Can only write to files in {tools_dir}, {agents_dir}, or {trigger_sources_dir}"

    # Validate Python syntax
    validator = CodeValidator(project_root)
    valid, msg = validator.validate_python_syntax(content)
    if not valid:
        return f"[Error]: Invalid Python syntax: {msg}"

    # For NEW files in nymeria/tools/, verify @tool decorator is present
    # (prevents writing a plain module that won't export any callable tools)
    # Existing files are excluded — they may be legitimate helpers (utils.py, metadata.py)
    try:
        path.relative_to(tools_dir)
        if not path.exists() and path.name != "__init__.py":
            tool_valid, tool_msg = validator.validate_tool_definition(content)
            if not tool_valid:
                return f"[Error]: {tool_msg}. New tool files in nymeria/tools/ must contain at least one @tool decorated function."
    except ValueError:
        pass  # File is in agents/ or triggers/sources/ — no @tool check needed

    # Create backup if file exists
    backup_manager = BackupManager(settings.backups_dir, project_root)
    if path.exists():
        backup_path = backup_manager.create_backup(path)
        if backup_path:
            logger.info(f"Created backup: {backup_path}")

    # Write the file
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"[Success]: Wrote {len(content)} characters to {file_path}"
    except Exception as e:
        return f"[Error]: Failed to write file: {e}"


@tool
def self_file_list(directory: str = "nymeria/tools") -> str:
    """
    List files in a directory.

    Args:
        directory: Directory path (default: nymeria/tools)

    Returns:
        List of files
    """
    settings = get_settings()
    project_root = settings.project_root

    # Security check
    path = _resolve_in_project(directory, project_root)
    if path is None:
        return "[Error]: Access denied. Directory must be within project"

    if not path.exists():
        return f"[Error]: Directory not found: {directory}"

    if not path.is_dir():
        return f"[Error]: Not a directory: {directory}"

    files = []
    for f in sorted(path.iterdir()):
        if f.is_file():
            files.append(f"  {f.name} ({f.stat().st_size} bytes)")
        else:
            files.append(f"  {f.name}/")

    return f"Contents of {directory}:\n" + "\n".join(files)


@tool
def self_test_import() -> str:
    """
    Test that all tools can be imported successfully.

    Use this after making changes to verify nothing is broken.

    Returns:
        Success or error message
    """
    settings = get_settings()
    validator = CodeValidator(settings.project_root)
    success, msg = validator.test_tool_import()

    if success:
        return f"[Success]: {msg}"
    else:
        return f"[Error]: {msg}"


@tool
def self_file_delete(file_path: str) -> str:
    """
    Delete a file from the tools or agents directory.

    IMPORTANT: Only files in nymeria/tools/ or nymeria/agents/ can be deleted.

    Args:
        file_path: Relative path from the project root (e.g., "nymeria/tools/my_tool.py") or absolute path

    Returns:
        Success or error message
    """
    if not self_edit_allowed():
        return self_edit_disabled_error()

    settings = get_settings()
    project_root = settings.project_root
    tools_dir, agents_dir, trigger_sources_dir = _writable_dirs(project_root)

    # Security check: must be within tools, agents, or trigger sources directory
    path = resolve_in_writable_dir(file_path, project_root)
    if path is None:
        return f"[Error]: Access denied. Can only delete files in {tools_dir}, {agents_dir}, or {trigger_sources_dir}"

    # Don't allow deleting __init__.py
    if path.name == "__init__.py":
        return "[Error]: Cannot delete __init__.py - this would break the module"

    if not path.exists():
        return f"[Error]: File not found: {file_path}"

    # Create backup before deleting
    backup_manager = BackupManager(settings.backups_dir, project_root)
    backup_path = backup_manager.create_backup(path)
    if backup_path:
        logger.info(f"Created backup before delete: {backup_path}")
    else:
        logger.warning(f"Could not create backup before deleting {path}")

    # Delete the file
    try:
        path.unlink()
        backup_note = " (backup saved)" if backup_path else " (warning: no backup created)"
        return f"[Success]: Deleted {file_path}{backup_note}"
    except Exception as e:
        return f"[Error]: Failed to delete file: {e}"


@tool
def self_reload() -> str:
    """
    Reload all tools, skills, and trigger sources after making changes.

    Call this after using self_file_write to create or modify tool/agent files
    and updating __init__.py. This makes newly created tools live in Nymeria's
    registry so they can be tested with self_invoke_tool.

    Returns:
        Updated tool list or error message
    """
    if not self_edit_allowed():
        return self_edit_disabled_error()

    from ..tools.runtime_admin import _do_full_reload

    try:
        tool_count, skill_count, source_count = _do_full_reload()
        return (
            f"[Success]: Reloaded {tool_count} tools, {skill_count} skill(s), "
            f"{source_count} trigger source(s).\n"
            f"New tools will be available on the next message."
        )
    except Exception as e:
        logger.error(f"self_reload failed: {e}", exc_info=True)
        return f"[Error]: Reload failed: {str(e)}"


@tool
def self_invoke_tool(
    tool_name: str,
    arguments_json: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Test a tool by invoking it with the given arguments.

    Use this after self_reload() to verify a newly created tool works correctly.
    If the tool fails, fix the code and repeat the create->reload->test cycle.

    Args:
        tool_name: Name of the tool to test (e.g., "my_new_tool")
        arguments_json: JSON string of arguments to pass (e.g., '{"param": "value"}')

    Returns:
        The tool's output or an error message
    """
    from ..tools.utils import caller_role, get_thread_id, get_user_id
    from .agent import get_current_agent
    from .tool_execution import (
        ToolDenied,
        by_name_gate_reason,
        resolve_by_name,
        run_tool_envelope,
        superset_tool_names,
    )

    try:
        agent = get_current_agent()
        if agent is None:
            return "[Error]: No active agent found. Cannot invoke tool."

        user_id = get_user_id(config)
        thread_id = get_thread_id(config)

        # This is a by-name dispatch like any other, so it takes the shared gate
        # rather than none at all. It previously applied no denylist, no role
        # gate and no disabled_tools check, which made it the widest by-name
        # path in the system despite being the narrowest in purpose. The
        # create -> reload -> test cycle it exists for is unaffected: a freshly
        # written tool is not protected, not admin-only and not disabled.
        gate = by_name_gate_reason(
            agent, tool_name, user_id, thread_id, caller_role(user_id, agent=agent)
        )
        if gate:
            return f"[Error]: {gate}"

        # Resolved from the caller's dispatch superset, not agent.tool_registry.
        # The registry holds EVERY user's callable-thread tools, so resolving
        # there reached across accounts and left the runtime ownership gate as
        # the only thing in the way. The superset is team-scoped per user, so
        # the reach is now structural rather than a check that has to fire.
        tool_obj = resolve_by_name(agent, user_id, thread_id, tool_name)
        if not tool_obj:
            # Enumerated from the SAME superset the resolution used, not from
            # agent.tool_registry: the registry spans every user's callable
            # threads, so a help string built from it both leaks other accounts'
            # callable names and omits catalog tools this caller can reach.
            available = sorted(superset_tool_names(agent, user_id, thread_id))
            return f"[Error]: Tool '{tool_name}' not found. Available tools: {', '.join(available[:20])}"

        # Parse arguments
        try:
            args = json.loads(arguments_json)
        except json.JSONDecodeError as e:
            return f"[Error]: Invalid JSON arguments: {e}"

        # The caller's RunnableConfig is forwarded so InjectedToolArg-bearing
        # tools (notably the callable-thread closures in agents/tool_factory.py)
        # see the real user_id/thread_id and the runtime ownership gate fires.
        # Without it, callable closures default to user_id="default", which is
        # admin, so any user with self_invoke_tool enabled could invoke another
        # user's callable as the bootstrap admin.
        result = run_tool_envelope(
            call={"name": tool_name, "args": args, "id": f"self-invoke-{tool_name}"},
            config=config,
            execute=lambda effective: tool_obj.invoke(
                effective.get("args") or {}, config=config
            ),
        )
        return f"[Test result]: {result}"

    except ToolDenied as denied:
        return f"[Error]: '{tool_name}' was blocked by a lifecycle hook: {denied.reason}"
    except Exception as e:
        logger.error(f"self_invoke_tool failed: {e}", exc_info=True)
        return f"[Error]: Tool invocation failed: {str(e)}"


@tool
def self_modify_instructions() -> str:
    """
    Get the self-modification workflow guide, code templates, and safety rules.

    IMPORTANT: Call this tool FIRST before using any other self_* tools.
    It returns the complete instructions for how to correctly create, modify,
    test, and register tools — including the required create->reload->test cycle,
    the code template to follow, codebase structure, and safety rules.

    Returns:
        The full self-modification instruction guide
    """
    settings = get_settings()
    prompt_path = settings.project_root / "nymeria" / "config" / "self_agent_prompt.md"

    try:
        content = prompt_path.read_text(encoding="utf-8")
        return content
    except FileNotFoundError:
        return "[Error]: Self-modification instructions file not found at nymeria/config/self_agent_prompt.md"
    except Exception as e:
        return f"[Error]: Failed to read instructions: {e}"


# Tools available for self-modification (optional — enabled per-thread)
SELF_AGENT_TOOLS: List[BaseTool] = [
    self_modify_instructions,
    self_file_read,
    self_file_write,
    self_file_list,
    self_file_delete,
    self_test_import,
    self_reload,
    self_invoke_tool,
]
