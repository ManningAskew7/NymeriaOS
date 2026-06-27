"""Bash/shell execution tool for Nymeria."""

import logging
import secrets
import subprocess
import sys
import time
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .bash_background import (
    BackgroundJobRecord,
    cleanup_output_files,
    get_registry,
    open_output_files,
    spawn_watcher,
)
from .execution_environment import resolve_tool_working_directory
from .utils import get_thread_id_or_none
from ..oom import oom_score_preexec, with_tool_oom_score

logger = logging.getLogger(__name__)


@tool
def bash_execute(
    command: str,
    working_directory: Optional[str] = None,
    timeout_seconds: int = 120,
    run_in_background: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
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
            background process and return immediately with the PID, job ID,
            and stdout/stderr temp file paths. When called from an agent
            thread with user/thread context, Nymeria submits an autonomous
            completion prompt to the same thread when the process exits.
            Use this for GUI apps, servers, or long-running processes
            that should keep running after the tool returns.

    Returns:
        Command output (stdout + stderr) or error message.
        If run_in_background=True, returns the PID, job ID, and output paths
        when agent thread context is available; otherwise returns the legacy
        PID-only message.
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
            thread_id = get_thread_id_or_none(config)
            user_id = _get_user_id_or_none(config)
            if not thread_id or not user_id:
                return _launch_legacy_background(command, cwd_str)

            job_id = secrets.token_hex(4)
            stdout_path = ""
            stderr_path = ""
            stdout_file = None
            stderr_file = None
            try:
                stdout_path, stderr_path, stdout_file, stderr_file = (
                    open_output_files(job_id)
                )
                kwargs = _background_popen_kwargs(
                    cwd_str,
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
                started_at = time.time()
                proc = subprocess.Popen(command, **kwargs)
            except Exception:
                if stdout_file is not None:
                    stdout_file.close()
                if stderr_file is not None:
                    stderr_file.close()
                cleanup_output_files(stdout_path, stderr_path)
                raise
            else:
                stdout_file.close()
                stderr_file.close()

            record = BackgroundJobRecord(
                id=job_id,
                pid=proc.pid,
                command=command,
                working_directory=cwd_str,
                started_at=started_at,
                finished_at=None,
                exit_code=None,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                status="running",
                thread_id=thread_id,
                user_id=user_id,
            )
            get_registry().register(record)
            spawn_watcher(record, proc)
            logger.info(
                f"Background process started: job_id={job_id}, PID={proc.pid}, "
                f"thread={thread_id}, user={user_id}, command={command[:80]}"
            )
            return (
                "Process started in background.\n"
                f"job_id: {job_id}\n"
                f"pid: {proc.pid}\n"
                f"stdout: {stdout_path}\n"
                f"stderr: {stderr_path}\n"
                "A completion notification will be submitted to this thread when "
                "the process exits. Temp files persist until manually removed. "
                "If the backend process restarts first, no completion notification "
                "will be sent."
            )

        # Normal (blocking) execution. Tag the child's OOM score so a heavy
        # command is the kernel's eviction target under memory pressure, not
        # the API server (see nymeria/oom.py).
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd_str,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            preexec_fn=oom_score_preexec(),
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


def _get_user_id_or_none(config: Optional[RunnableConfig]) -> Optional[str]:
    if config is None:
        return None
    return config.get("configurable", {}).get("user_id") or None


def _background_popen_kwargs(
    cwd_str: str,
    *,
    stdout,
    stderr,
) -> dict:
    kwargs = {
        "shell": True,
        "cwd": cwd_str,
        "stdout": stdout,
        "stderr": stderr,
        "stdin": subprocess.DEVNULL,
    }
    # On Windows, fully detach from the parent process tree.
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    # A long-running background command can grow; make it the OOM victim
    # rather than the API server it was launched from.
    with_tool_oom_score(kwargs)
    return kwargs


def _launch_legacy_background(command: str, cwd_str: str) -> str:
    # Launch detached; the process keeps running independently.
    kwargs = _background_popen_kwargs(
        cwd_str,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc = subprocess.Popen(command, **kwargs)
    logger.info(
        f"Background process started without thread context: PID={proc.pid}, "
        f"command={command[:80]}"
    )
    return f"Process started in background (PID: {proc.pid})"
