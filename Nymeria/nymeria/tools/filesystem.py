"""Filesystem tools for Nymeria."""

import logging
import os
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

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
        File contents or error message
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
) -> str:
    """
    Write content to a file.

    Use this tool to create or modify text files on the filesystem.

    Args:
        file_path: Absolute or relative path to the file
        content: Content to write to the file
        encoding: File encoding (default utf-8)
        create_directories: Create parent directories if they don't exist (default True)
        append: Append to file instead of overwriting (default False)

    Returns:
        Success message or error message
    """
    logger.info(f"Writing to file: {file_path} (append={append})")

    try:
        path = Path(file_path).resolve()

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
        return f"[Success]: {action} {len(content)} characters to {file_path}"

    except PermissionError:
        return f"[Error]: Permission denied writing to: {file_path}"

    except Exception as e:
        error_msg = f"Failed to write file: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


@tool
def file_list(
    directory: str,
    pattern: str = "*",
    recursive: bool = False,
) -> str:
    """
    List files in a directory.

    Use this tool to see what files exist in a directory.

    Args:
        directory: Path to the directory
        pattern: Glob pattern to filter files (default "*" for all)
        recursive: Search recursively in subdirectories (default False)

    Returns:
        List of files or error message
    """
    logger.info(f"Listing directory: {directory} (pattern={pattern}, recursive={recursive})")

    try:
        path = Path(directory).resolve()

        if not path.exists():
            return f"[Error]: Directory not found: {directory}"

        if not path.is_dir():
            return f"[Error]: Not a directory: {directory}"

        if recursive:
            files = list(path.rglob(pattern))
        else:
            files = list(path.glob(pattern))

        if not files:
            return f"No files matching '{pattern}' in {directory}"

        # Sort and format output
        files.sort()
        output_lines = []
        for f in files[:500]:  # Limit to 500 entries
            rel_path = f.relative_to(path) if f.is_relative_to(path) else f
            if f.is_dir():
                output_lines.append(f"[DIR]  {rel_path}")
            else:
                size = f.stat().st_size
                output_lines.append(f"[FILE] {rel_path} ({size:,} bytes)")

        output = "\n".join(output_lines)
        if len(files) > 500:
            output += f"\n\n[Showing 500 of {len(files)} items]"

        return output

    except PermissionError:
        return f"[Error]: Permission denied accessing: {directory}"

    except Exception as e:
        error_msg = f"Failed to list directory: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"
