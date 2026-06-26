"""Claude Code integration tool for Nymeria."""

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# VS Code installs the Claude Code extension into versioned directories named
# "anthropic.claude-code-<version>-win32-x64". The version moves on every update,
# so match it with a glob and pick the newest rather than pinning a literal.
_CLAUDE_CODE_EXTENSION_RE = re.compile(r"^anthropic\.claude-code-(.+)-win32-x64$")
_CLAUDE_CODE_EXTENSION_GLOB = (
    "anthropic.claude-code-*-win32-x64/resources/native-binary/claude.exe"
)


def _extension_version_key(extension_dir_name: str) -> tuple[int, ...]:
    """Return a numeric sort key for a claude-code extension directory name.

    Parses the version out of names like ``anthropic.claude-code-2.1.113-win32-x64``
    into a tuple of ints (``(2, 1, 113)``) so versions order numerically rather
    than lexicographically (``2.1.113`` must rank above ``2.1.29``). Unparseable
    names return an empty tuple, which sorts lowest.
    """
    match = _CLAUDE_CODE_EXTENSION_RE.match(extension_dir_name)
    if not match:
        return ()
    parts: list[int] = []
    for segment in match.group(1).split("."):
        leading_digits = re.match(r"\d+", segment)
        parts.append(int(leading_digits.group()) if leading_digits else 0)
    return tuple(parts)


def _find_claude_code_executable() -> Optional[str]:
    """Find the Claude Code executable for the current platform."""
    # First, try to find 'claude' in PATH (works for npm global install on Linux/Docker)
    claude_path = shutil.which("claude")
    if claude_path:
        return claude_path

    # On Windows, check the VS Code extension install location.
    if sys.platform == "win32":
        extensions_dir = Path.home() / ".vscode" / "extensions"
        # glob only yields claude.exe paths that exist; pick the highest version.
        candidates = list(extensions_dir.glob(_CLAUDE_CODE_EXTENSION_GLOB))
        if candidates:
            # exe is <ext-dir>/resources/native-binary/claude.exe, so
            # parents[2] is the versioned extension dir (matches the glob tail).
            newest = max(
                candidates,
                key=lambda exe: _extension_version_key(exe.parents[2].name),
            )
            return str(newest)

        # Also check for claude.exe in PATH on Windows
        claude_exe = shutil.which("claude.exe")
        if claude_exe:
            return claude_exe

    return None


# Find Claude Code executable at module load time
CLAUDE_CODE_EXE = _find_claude_code_executable()


@tool
def claude_code(
    prompt: str,
    working_dir: Optional[str] = None,
    model: str = "sonnet",
    allow_edit: bool = True,
    allow_bash: bool = True,
    timeout: int = 300,
) -> str:
    """
    Invoke Claude Code in headless mode to create, modify, or analyze code in a specific directory.

    Use this for complex coding tasks that benefit from Claude Code's specialized
    capabilities. Claude Code can read files, edit code, and run commands in the
    specified directory.

    Args:
        prompt: The coding task or question for Claude Code
        working_dir: Directory to run in (defaults to current directory)
        model: Model to use - "sonnet", "opus", or "haiku" (default "sonnet")
        allow_edit: Allow Claude Code to edit files (default True)
        allow_bash: Allow Claude Code to run commands (default True)
        timeout: Timeout in seconds (default 300)

    Returns:
        Claude Code's response or error message

    Examples:
        claude_code("Create a Python script that sorts a CSV file")
        claude_code("Fix the bug in main.py", working_dir="/path/to/project")
        claude_code("Analyze the code structure", allow_edit=False)
    """
    logger.info(f"claude_code called: prompt='{prompt[:50]}...', working_dir={working_dir}, model={model}")

    # Validate Claude Code executable exists
    if not CLAUDE_CODE_EXE:
        error_msg = "Claude Code executable not found. Install with: npm install -g @anthropic-ai/claude-code"
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    if not os.path.exists(CLAUDE_CODE_EXE):
        error_msg = f"Claude Code executable not found at: {CLAUDE_CODE_EXE}"
        logger.error(error_msg)
        return f"[Error]: {error_msg}"

    # Validate model parameter
    valid_models = ["sonnet", "opus", "haiku"]
    if model not in valid_models:
        return f"[Error]: Invalid model '{model}'. Must be one of: {', '.join(valid_models)}"

    # Set working directory
    work_dir = working_dir
    if work_dir:
        work_dir_path = Path(work_dir).resolve()
        if not work_dir_path.exists():
            return f"[Error]: Working directory does not exist: {work_dir}"
        if not work_dir_path.is_dir():
            return f"[Error]: Working directory is not a directory: {work_dir}"
        work_dir = str(work_dir_path)

    # Build tools list
    tools = ["Read"]  # Always include Read
    if allow_edit:
        tools.append("Edit")
    if allow_bash:
        tools.append("Bash")

    # Build command with prompt as argument
    cmd = [
        CLAUDE_CODE_EXE,
        "--print",
        "--model", model,
        "--tools", ",".join(tools),
        prompt,  # Pass prompt as positional argument
    ]

    logger.debug(f"Executing Claude Code: {' '.join(cmd[:6])}...")

    try:
        # Pass current environment to ensure auth tokens are available
        env = os.environ.copy()
        result = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        output_parts = []

        # Add stdout
        if result.stdout:
            output_parts.append(result.stdout)

        # Add stderr if present
        if result.stderr:
            output_parts.append(f"\n[stderr]:\n{result.stderr}")

        # Add exit code if non-zero
        if result.returncode != 0:
            output_parts.append(f"\n[exit code: {result.returncode}]")
            logger.warning(f"Claude Code exited with code {result.returncode}")

        output = "\n".join(output_parts) if output_parts else "[No output]"

        # Truncate very long outputs
        max_length = 50000
        if len(output) > max_length:
            output = output[:max_length] + f"\n\n[Output truncated, {len(output)} total chars]"

        logger.debug(f"Claude Code output: {output[:200]}...")
        return output

    except subprocess.TimeoutExpired:
        error_msg = f"Claude Code timed out after {timeout} seconds"
        logger.warning(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"Failed to execute Claude Code: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"
