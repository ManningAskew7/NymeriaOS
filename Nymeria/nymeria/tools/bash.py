"""Bash/shell execution tool for Nymeria."""

import logging
import subprocess
import sys
from typing import Optional

from langchain_core.tools import tool

from .execution_environment import resolve_tool_working_directory

logger = logging.getLogger(__name__)


@tool
def bash_execute(
    command: str,
    working_directory: Optional[str] = None,
    timeout_seconds: int = 120,
    run_in_background: bool = False,
) -> str:
    """
    Execute a shell command and return the output.

    Use this tool to run bash/shell commands on the local system.
    The command runs directly without sandboxing (trusted execution).

    Args:
        command: The shell command to execute
        working_directory: Optional directory to run the command in. Relative
            paths resolve from Nymeria's detected default tool cwd.
        timeout_seconds: Maximum time to wait for command (default 120s).
            Set lower (e.g. 5) for commands that might hang.
        run_in_background: If True, launch the command as a detached
            background process and return immediately with the PID.
            Use this for GUI apps, servers, or long-running processes
            that should keep running after the tool returns.

    Returns:
        Command output (stdout + stderr) or error message.
        If run_in_background=True, returns the PID of the background process.
    """
    logger.info(f"Executing command: {command[:100]}...")

    try:
        cwd = resolve_tool_working_directory(working_directory)
        if not cwd.exists():
            return f"[Error]: Working directory does not exist: {cwd}"
        if not cwd.is_dir():
            return f"[Error]: Working directory is not a directory: {cwd}"
        cwd_str = str(cwd)

        if run_in_background:
            # Launch detached — the process keeps running independently
            kwargs = {
                "shell": True,
                "cwd": cwd_str,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "stdin": subprocess.DEVNULL,
            }
            # On Windows, fully detach from the parent process tree
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                kwargs["start_new_session"] = True

            proc = subprocess.Popen(command, **kwargs)
            logger.info(
                f"Background process started: PID={proc.pid}, "
                f"command={command[:80]}"
            )
            return f"Process started in background (PID: {proc.pid})"

        # Normal (blocking) execution
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd_str,
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
