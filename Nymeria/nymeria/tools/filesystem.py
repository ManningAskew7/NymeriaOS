"""Filesystem tools for Nymeria."""

import logging
import os
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

from ..config import get_settings

logger = logging.getLogger(__name__)

# Protected directories within Nymeria that should not be modified directly
# Use self_modify for tools/agents modifications instead
NYMERIA_PROTECTED_DIRS = [
    "nymeria/core",
    "nymeria/config",
    "nymeria/triggers",
    "nymeria/gateway",
    "nymeria/__init__.py",
]

# Get the Nymeria project root for path comparison
_NYMERIA_ROOT = Path(__file__).parent.parent.parent.resolve()


def get_workspace_dir() -> Path:
    """Return the only filesystem root where mutating file tools may write."""
    return Path(os.environ.get("NYMERIA_WORKSPACE_DIR", "/workspace")).resolve()


def confine_file_tools_to_workspace() -> bool:
    """Return whether mutating file tools should reject paths outside workspace."""
    try:
        return bool(getattr(get_settings(), "nymeria_confine_file_to_workspace", False))
    except Exception:
        logger.debug("Failed to read file-tool confinement setting", exc_info=True)
        return False


def resolve_workspace_write_path(file_path: str) -> tuple[Optional[Path], Optional[str]]:
    """Resolve a requested write target, optionally enforcing workspace confinement."""
    if not confine_file_tools_to_workspace():
        return Path(file_path).resolve(), None

    workspace_dir = get_workspace_dir()
    requested = Path(file_path)
    if not requested.is_absolute():
        requested = workspace_dir / requested
    path = requested.resolve()
    if not path.is_relative_to(workspace_dir):
        return None, (
            f"Path outside workspace: {path}. Mutating file tools are confined "
            f"to {workspace_dir}. Set NYMERIA_WORKSPACE_DIR to change the root."
        )
    return path, None


@tool
def file_read(
    file_path: str,
    encoding: str = "utf-8",
    max_lines: Optional[int] = None,
) -> str:
    """
    Read the contents of a file.

    Use this tool to read text files from the filesystem.

    Args:
        file_path: Absolute or relative path to the file
        encoding: File encoding (default utf-8)
        max_lines: Maximum number of lines to read (optional, reads all if not specified)

    Returns:
        File contents as plain text. Truncated output ends with
        "[Truncated after N lines]". Errors: "[Error]: <reason>".
    """
    logger.info(f"Reading file: {file_path}")

    try:
        path = Path(file_path).resolve()

        if not path.exists():
            return f"[Error]: File not found: {file_path}"

        if not path.is_file():
            return f"[Error]: Not a file: {file_path}"

        # Check file size
        file_size = path.stat().st_size
        max_size = 10 * 1024 * 1024  # 10 MB
        if file_size > max_size:
            return f"[Error]: File too large ({file_size} bytes). Max size is {max_size} bytes."

        with open(path, "r", encoding=encoding) as f:
            if max_lines:
                lines = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        lines.append(f"\n[Truncated after {max_lines} lines]")
                        break
                    lines.append(line)
                content = "".join(lines)
            else:
                content = f.read()

        logger.debug(f"Read {len(content)} characters from {file_path}")
        return content

    except UnicodeDecodeError:
        return f"[Error]: Cannot decode file as {encoding}. Try a different encoding."

    except PermissionError:
        return f"[Error]: Permission denied reading: {file_path}"

    except Exception as e:
        error_msg = f"Failed to read file: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


@tool
def file_write(
    file_path: str,
    content: str,
    encoding: str = "utf-8",
    create_directories: bool = True,
    append: bool = False,
    attach: bool = False,
) -> str:
    """
    Write content to a file.

    Use this tool to create or modify text files on the filesystem.
    Set attach=True to send the file to the user in chat (Telegram/Discord) after writing.

    Args:
        file_path: Absolute or relative path to the file
        content: Content to write to the file
        encoding: File encoding (default utf-8)
        create_directories: Create parent directories if they don't exist (default True)
        append: Append to file instead of overwriting (default False)
        attach: Send the written file to the user as a downloadable attachment (default False)

    Returns:
        "[Success]: Wrote N characters to <path>" (or "Appended").
        If attach=True, includes "[attach:<path>]" tag for chat delivery.
        Errors: "[Error]: <reason>".
    """
    logger.info(f"Writing to file: {file_path} (append={append})")

    try:
        path, workspace_error = resolve_workspace_write_path(file_path)
        if workspace_error:
            return f"[Error]: {workspace_error}"
        assert path is not None

        # Check if this is a protected Nymeria system file
        try:
            rel_path = path.relative_to(_NYMERIA_ROOT)
            rel_path_str = str(rel_path).replace("\\", "/")

            for protected in NYMERIA_PROTECTED_DIRS:
                if rel_path_str.startswith(protected) or rel_path_str == protected:
                    logger.warning(f"Blocked write to protected path: {rel_path_str}")
                    return (
                        f"[Error]: Cannot modify protected system file: {rel_path_str}\n"
                        f"Protected directories: {', '.join(NYMERIA_PROTECTED_DIRS)}\n"
                        f"Use self_modify() to modify tools or agents instead."
                    )
        except ValueError:
            # Path is outside Nymeria project - allow it
            pass

        # Create parent directories if requested
        if create_directories:
            path.parent.mkdir(parents=True, exist_ok=True)

        if not path.parent.exists():
            return f"[Error]: Directory does not exist: {path.parent}"

        mode = "a" if append else "w"
        with open(path, mode, encoding=encoding) as f:
            f.write(content)

        action = "Appended to" if append else "Wrote"
        logger.debug(f"{action} {len(content)} characters to {file_path}")
        result = f"[Success]: {action} {len(content)} characters to {file_path}"
        if attach:
            workspace_dir = get_workspace_dir()
            if path.is_relative_to(workspace_dir):
                result += f"\n[attach:{path}]"
            else:
                result += (
                    f"\n[Info]: Attachment skipped. Only files inside "
                    f"{workspace_dir} can be delivered to chat clients."
                )
        return result

    except PermissionError:
        return f"[Error]: Permission denied writing to: {file_path}"

    except Exception as e:
        error_msg = f"Failed to write file: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"
