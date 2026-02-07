"""Bash/shell execution tool for Nymeria."""

import logging
import subprocess
from typing import Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
def bash_execute(
    command: str,
    working_directory: Optional[str] = None,
    timeout_seconds: int = 120,
) -> str:
    """
    Execute a shell command and return the output.

    Use this tool to run bash/shell commands on the local system.
    The command runs directly without sandboxing (trusted execution).

    Args:
        command: The shell command to execute
        working_directory: Optional directory to run the command in
        timeout_seconds: Maximum time to wait for command (default 120s)

    Returns:
        Command output (stdout + stderr) or error message
    """
    logger.info(f"Executing command: {command[:100]}...")

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=working_directory,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

        output_parts = []

        if result.stdout:
            output_parts.append(result.stdout)

        if result.stderr:
            output_parts.append(f"[stderr]: {result.stderr}")

        if result.returncode != 0:
            output_parts.append(f"[exit code: {result.returncode}]")

        output = "\n".join(output_parts) if output_parts else "[No output]"

        # Truncate very long outputs
        max_length = 50000
        if len(output) > max_length:
            output = output[:max_length] + f"\n\n[Output truncated, {len(output)} total chars]"

        logger.debug(f"Command output: {output[:200]}...")
        return output

    except subprocess.TimeoutExpired:
        error_msg = f"Command timed out after {timeout_seconds} seconds"
        logger.warning(error_msg)
        return f"[Error]: {error_msg}"

    except Exception as e:
        error_msg = f"Failed to execute command: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"
