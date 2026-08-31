"""Inspect and control background bash jobs.

``bash_execute(run_in_background=True)`` launches a detached process, registers
it, and submits an autonomous completion prompt when it exits. This tool is the
optional companion for tending such a job WHILE it runs: check its status, tail
its output, or kill it. It is a CATALOG (opt-in, non-seeded) tool -- the agent
can always fall back to plain ``bash_execute`` (``kill``, ``file_read`` on the
stdout/stderr paths), so it is convenience, not a dependency.

All actions are scoped to the calling user: a job started by another user is
never listed, shown, or killable here.
"""
from .registry import ToolGroup, register_tool_group

import logging
import os
import time
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .bash_background import (
    BackgroundJobRecord,
    get_registry,
    kill_process_group,
)

logger = logging.getLogger(__name__)

_LOG_TAIL_BYTES = 65536


@tool
def bash_job(
    action: str,
    job_id: str = "",
    lines: int = 50,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Inspect or control background bash jobs (from bash_execute run_in_background).

    Use this to tend a background command while it runs. You are also notified
    automatically when a background job finishes, so you do not need to poll.

    Args:
        action: One of:
            - "list": list your recent background jobs (id, status, pid, command).
            - "status": show one job's status, exit code, runtime, output paths
              (requires job_id).
            - "log": show the tail of a job's stdout and stderr (requires
              job_id; use `lines` to control how many).
            - "kill": terminate a running job and everything it spawned
              (requires job_id).
        job_id: The job id (from the bash_execute launch message), for
            status/log/kill.
        lines: For action="log", how many trailing lines of each stream to show
            (default 50).

    Returns:
        A human-readable status/log/confirmation, or an error message.
    """
    user_id = _user_id(config)
    if not user_id:
        return "[Error]: bash_job needs user context; call it from an agent thread."

    normalized = (action or "").strip().lower()
    if normalized == "list":
        return _list_jobs(user_id)
    if normalized in {"status", "log", "kill"}:
        if not job_id:
            return f"[Error]: action='{normalized}' requires a job_id."
        record = get_registry().get(job_id.strip())
        if record is None or record.user_id != user_id:
            return f"[Error]: No background job {job_id!r} found for you."
        if normalized == "status":
            return _status(record)
        if normalized == "log":
            return _log(record, lines)
        return _kill(record)
    return (
        f"[Error]: Unknown action {action!r}. Use list, status, log, or kill."
    )


def _user_id(config: Optional[RunnableConfig]) -> Optional[str]:
    if config is None:
        return None
    return config.get("configurable", {}).get("user_id") or None


def _list_jobs(user_id: str) -> str:
    records = [r for r in get_registry().records() if r.user_id == user_id]
    if not records:
        return "No background jobs. Start one with bash_execute(run_in_background=True)."
    # Newest last in the registry; show newest first.
    lines = ["Your background jobs (newest first):"]
    for record in reversed(records):
        lines.append(
            f"- {record.id}  [{record.status}]  pid={record.pid}  "
            f"{_short(record.command, 70)}"
        )
    return "\n".join(lines)


def _status(record: BackgroundJobRecord) -> str:
    now = time.time()
    end = record.finished_at or now
    duration = max(0.0, end - record.started_at)
    exit_code = record.exit_code if record.exit_code is not None else "n/a"
    return (
        f"job_id: {record.id}\n"
        f"status: {record.status}\n"
        f"pid: {record.pid}\n"
        f"exit_code: {exit_code}\n"
        f"runtime: {duration:.1f}s\n"
        f"command: {_short(record.command, 200)}\n"
        f"cwd: {record.working_directory}\n"
        f"stdout: {record.stdout_path}\n"
        f"stderr: {record.stderr_path}"
    )


def _log(record: BackgroundJobRecord, lines: int) -> str:
    count = max(1, min(int(lines) if lines else 50, 1000))
    stdout_tail = _tail_lines(record.stdout_path, count)
    stderr_tail = _tail_lines(record.stderr_path, count)
    return (
        f"job_id: {record.id}  status: {record.status}\n\n"
        f"stdout (last {count} lines):\n"
        "--- stdout starts ---\n"
        f"{stdout_tail or '[empty]'}\n"
        "--- stdout ends ---\n\n"
        f"stderr (last {count} lines):\n"
        "--- stderr starts ---\n"
        f"{stderr_tail or '[empty]'}\n"
        "--- stderr ends ---"
    )


def _kill(record: BackgroundJobRecord) -> str:
    if record.status != "running":
        return f"Job {record.id} is already {record.status} (exit_code={record.exit_code})."
    ok = kill_process_group(record.pid)
    if ok:
        return (
            f"Sent termination to job {record.id} (pid {record.pid}) and its "
            "process group. A completion notification will follow when it exits."
        )
    return (
        f"Job {record.id} (pid {record.pid}) could not be signalled; it may have "
        "already exited. Check status."
    )


def _tail_lines(path: str, count: int) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_TAIL_BYTES), os.SEEK_SET)
            data = handle.read(_LOG_TAIL_BYTES)
    except FileNotFoundError:
        return "[file missing]"
    except OSError as exc:
        return f"[error reading file: {exc}]"
    text = data.decode("utf-8", errors="backslashreplace")
    cleaned = "".join(ch if ch in "\n\r\t" or ord(ch) >= 32 else "?" for ch in text)
    tail = cleaned.splitlines()[-count:]
    return "\n".join(tail)


def _short(value: str, limit: int) -> str:
    compact = " ".join(value.splitlines())
    return compact if len(compact) <= limit else compact[:limit] + "..."


BASH_JOB_TOOLS = [bash_job]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="bash_job", tools=tuple(BASH_JOB_TOOLS)))
