"""Self-modification tools for Nymeria.

These tools allow Nymeria to read, write, delete, and test its own code.
Optional tools — enable per-thread via thread config.
"""

import json
import logging
from pathlib import Path
from typing import List

from langchain_core.tools import BaseTool, tool

from .backup import BackupManager
from .validator import CodeValidator
from ..config import get_settings

logger = logging.getLogger(__name__)


# Define tools available to the self-modification agent
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

    # Resolve path
    path = Path(file_path)
    if not path.is_absolute():
        path = project_root / file_path
    path = path.resolve()

    # Security check: must be within project
    try:
        path.relative_to(project_root)
    except ValueError:
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
    settings = get_settings()
    project_root = settings.project_root
    tools_dir = project_root / "nymeria" / "tools"
    agents_dir = project_root / "nymeria" / "agents"
    trigger_sources_dir = project_root / "nymeria" / "triggers" / "sources"

    # Resolve path
    path = Path(file_path)
    if not path.is_absolute():
        path = project_root / file_path
    path = path.resolve()

    # Security check: must be within tools, agents, or trigger sources directory
    in_allowed = False
    for allowed_dir in (tools_dir, agents_dir, trigger_sources_dir):
        try:
            path.relative_to(allowed_dir)
            in_allowed = True
            break
        except ValueError:
            pass

    if not in_allowed:
        return f"[Error]: Access denied. Can only write to files in {tools_dir}, {agents_dir}, or {trigger_sources_dir}"

    # Validate Python syntax
    validator = CodeValidator(project_root)
    valid, msg = validator.validate_python_syntax(content)
    if not valid:
        return f"[Error]: Invalid Python syntax: {msg}"

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

    path = Path(directory)
    if not path.is_absolute():
        path = project_root / directory
    path = path.resolve()

    # Security check
    try:
        path.relative_to(project_root)
    except ValueError:
        return f"[Error]: Access denied. Directory must be within project"

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
    settings = get_settings()
    project_root = settings.project_root
    tools_dir = project_root / "nymeria" / "tools"
    agents_dir = project_root / "nymeria" / "agents"
    trigger_sources_dir = project_root / "nymeria" / "triggers" / "sources"

    # Resolve path
    path = Path(file_path)
    if not path.is_absolute():
        path = project_root / file_path
    path = path.resolve()

    # Security check: must be within tools, agents, or trigger sources directory
    in_allowed = False
    for allowed_dir in (tools_dir, agents_dir, trigger_sources_dir):
        try:
            path.relative_to(allowed_dir)
            in_allowed = True
            break
        except ValueError:
            pass

    if not in_allowed:
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

    # Delete the file
    try:
        path.unlink()
        return f"[Success]: Deleted {file_path}"
    except Exception as e:
        return f"[Error]: Failed to delete file: {e}"


@tool
def self_reload() -> str:
    """
    Reload all tools and agents after making changes.

    Call this after using self_file_write to create or modify tool/agent files
    and updating __init__.py. This makes newly created tools live in Nymeria's
    registry so they can be tested with self_invoke_tool.

    Returns:
        Updated tool list or error message
    """
    from .agent import get_current_agent

    try:
        agent = get_current_agent()
        if agent is None:
            return "[Error]: No active agent found. Tools cannot be reloaded."

        tool_names = agent.reload_tools()
        return f"[Success]: Reloaded. {len(tool_names)} tools available: {', '.join(tool_names)}"
    except Exception as e:
        logger.error(f"self_reload failed: {e}", exc_info=True)
        return f"[Error]: Reload failed: {str(e)}"


@tool
def self_invoke_tool(tool_name: str, arguments_json: str) -> str:
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
    from .agent import get_current_agent

    try:
        agent = get_current_agent()
        if agent is None:
            return "[Error]: No active agent found. Cannot invoke tool."

        # Find tool in registry
        tool_obj = agent.tool_registry.get_tool(tool_name)
        if not tool_obj:
            available = [t["name"] for t in agent.tool_registry.list_tools()]
            return f"[Error]: Tool '{tool_name}' not found. Available tools: {', '.join(available[:20])}"

        # Parse arguments
        try:
            args = json.loads(arguments_json)
        except json.JSONDecodeError as e:
            return f"[Error]: Invalid JSON arguments: {e}"

        # Invoke the tool
        result = tool_obj.invoke(args)
        return f"[Test result]: {result}"

    except Exception as e:
        logger.error(f"self_invoke_tool failed: {e}", exc_info=True)
        return f"[Error]: Tool invocation failed: {str(e)}"


# Tools available for self-modification (optional — enabled per-thread)
SELF_AGENT_TOOLS: List[BaseTool] = [
    self_file_read,
    self_file_write,
    self_file_list,
    self_file_delete,
    self_test_import,
    self_reload,
    self_invoke_tool,
]
