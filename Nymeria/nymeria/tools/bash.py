"""Bash/shell execution tool for Nymeria."""

import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
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
    sweep_stale_output_files,
)
from .command_guard import check_command_guard
from .execution_environment import resolve_tool_working_directory
from .utils import get_thread_id_or_none
from ..config import get_settings
from ..oom import with_tool_oom_score

logger = logging.getLogger(__name__)

# Foreground output that exceeds this many characters is truncated to its tail
# for the model, with the full output spilled to a file the agent can read/grep.
MAX_OUTPUT_CHARS = 50000
# Bytes retained in memory as the rolling tail (slack over the char budget for
# multibyte decoding and line-boundary trimming).
_TAIL_KEEP_BYTES = MAX_OUTPUT_CHARS * 2
# Once the captured full output crosses this many bytes we spill it to a file
# and stop growing the in-memory head buffer.
_SPILL_THRESHOLD_BYTES = MAX_OUTPUT_CHARS
# Ceiling on how much a single command may write to its spill file, so a fast
# infinite producer bounds disk use the way the rolling tail bounds memory.
_SPILL_MAX_BYTES = 100 * 1024 * 1024

# Timeout bounds for the blocking (foreground) path. The static upper bound
# matches the per-tool config schema in metadata.py; the EFFECTIVE cap is
# additionally bounded by the deployment's tool timeout (settings.tool_timeout,
# default 300s) minus a teardown margin, because SafeToolNode wraps every tool
# call in that timeout: bash's own deadline must always fire first so the
# process group is torn down and clean output returned, instead of the node
# abandoning the worker thread with the command still running. Past the cap the
# model is told to use run_in_background instead of blocking a turn.
MIN_TIMEOUT_SECONDS = 1
MAX_FOREGROUND_TIMEOUT_SECONDS = 600
_NODE_TIMEOUT_MARGIN_SECONDS = 10

# Drain/kill loop timing.
_POLL_INTERVAL_SECONDS = 0.05
_POST_EXIT_DRAIN_SECONDS = 0.3   # let the reader flush after the shell exits
_POST_KILL_DRAIN_SECONDS = 0.5   # join the reader after the group is killed
_TERM_GRACE_SECONDS = 2.0        # SIGTERM -> wait -> SIGKILL

# Base environment exposed to bash commands. Matches the house convention used
# by the workflow runner and the hooks run_command action: deny by default so
# the API process's secrets (DB/Redis passwords, provider API keys, the service
# token) never leak into command output or LLM context. Extra names can be
# opted back in via the ``bash_env_passthrough`` setting.
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")

# Strip ANSI/OSC escape sequences from command output so the model never copies
# terminal control codes into files it writes and the text stays clean.
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"           # CSI ... final byte
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL/ST
    r"|\x1b[@-Z\\-_]"                      # two-char escapes
)


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

    Use this tool to run bash/shell commands on the local system. The command
    runs directly without sandboxing (trusted execution). stdout and stderr are
    returned interleaved, in the order the command emitted them, followed by a
    non-zero exit-code line when the command fails.

    Output over ~50k characters is truncated to its tail (the end, where errors
    and summaries live); the complete output is saved to a file whose path is
    included in the result, which you can read with file_read or search with
    grep/sed via another bash_execute call.

    Args:
        command: The shell command to execute
        working_directory: Optional directory to run the command in. Relative
            paths resolve from Nymeria's detected default tool cwd.
        timeout_seconds: Maximum time to wait for command (default 120s).
            Set lower (e.g. 5) for commands that might hang. On timeout the
            command and everything it spawned is killed. Values above the
            deployment's foreground cap (the configured tool timeout minus a
            teardown margin, at most 600s) are rejected; for anything expected
            to run that long, use run_in_background instead.
        run_in_background: If True, launch the command as a detached
            background process and return immediately with the PID, job ID,
            and stdout/stderr temp file paths. When called from an agent
            thread with user/thread context, Nymeria submits an autonomous
            completion prompt to the same thread when the process exits.
            Use this for GUI apps, servers, or long-running processes
            that should keep running after the tool returns. Do NOT background
            a foreground command with a trailing '&' (it is killed on return);
            use run_in_background instead.

    Returns:
        Command output (interleaved stdout + stderr) with an exit-code line on
        failure, or an error/blocked message. If run_in_background=True, returns
        the PID, job ID, and output paths when agent thread context is
        available; otherwise returns the legacy PID-only message.
    """
    logger.info(f"Executing command: {command[:100]}...")

    guard_reason = check_command_guard(command)
    if guard_reason:
        logger.warning(
            "Blocked bash command by hardline guard (%s): %s",
            guard_reason,
            command[:120],
        )
        return (
            f"[Blocked]: Refused by Nymeria's hardline safety guard: {guard_reason}. "
            "This command was not run. If you genuinely intended a destructive "
            "system operation, tell the user what you want to do and have them run "
            "it themselves."
        )

    try:
        cwd = resolve_tool_working_directory(working_directory)
        if not cwd.exists():
            return f"[Error]: Working directory does not exist: {cwd}"
        if not cwd.is_dir():
            return f"[Error]: Working directory is not a directory: {cwd}"
        cwd_str = str(cwd)
        thread_id = get_thread_id_or_none(config)

        if run_in_background:
            user_id = _get_user_id_or_none(config)
            if not thread_id or not user_id:
                return _launch_legacy_background(command, cwd_str)
            return _launch_tracked_background(command, cwd_str, thread_id, user_id)

        # Foreground (blocking) execution.
        cap = _effective_foreground_cap()
        if timeout_seconds > cap:
            return (
                f"[Error]: Requested timeout {timeout_seconds}s exceeds this "
                f"deployment's {cap}s foreground limit. For a long-running "
                "command, call bash_execute again with run_in_background=true; "
                "you will be notified when it finishes."
            )
        timeout_seconds = max(MIN_TIMEOUT_SECONDS, int(timeout_seconds))
        return _run_foreground(command, cwd_str, timeout_seconds, config, thread_id)

    except Exception as e:
        error_msg = f"Failed to execute command: {str(e)}"
        logger.error(error_msg, exc_info=True)
        return f"[Error]: {error_msg}"


# --- foreground execution -----------------------------------------------------


def _effective_foreground_cap() -> int:
    """The largest foreground timeout_seconds this deployment can honour."""
    cap = MAX_FOREGROUND_TIMEOUT_SECONDS
    try:
        tool_timeout = int(get_settings().tool_timeout or 0)
        if tool_timeout > 0:
            cap = min(
                cap,
                max(MIN_TIMEOUT_SECONDS, tool_timeout - _NODE_TIMEOUT_MARGIN_SECONDS),
            )
    except Exception:  # noqa: BLE001 - a settings hiccup must not break the tool
        pass
    return cap


def _run_foreground(
    command: str,
    cwd_str: str,
    timeout_seconds: int,
    config: Optional[RunnableConfig],
    thread_id: Optional[str],
) -> str:
    """Run a command to completion, killing its whole process group on timeout,
    abort, or exit (so a backgrounded grandchild can neither hold the pipe open
    nor linger). Output is captured with a bounded-memory rolling tail and a
    full-output spill file."""
    abort_event = _get_abort_event(thread_id)

    kwargs = {
        "shell": True,
        "cwd": cwd_str,
        "env": _scrubbed_env(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "bufsize": 0,
    }
    if sys.platform != "win32":
        kwargs["start_new_session"] = True  # own process group for group-kill
    with_tool_oom_score(kwargs)

    try:
        proc = subprocess.Popen(command, **kwargs)
    except Exception as e:  # noqa: BLE001
        return f"[Error]: Failed to launch command: {e}"

    pgid = _safe_pgid(proc)
    ingestor = _OutputIngestor(_make_spill_target(thread_id))
    reader = threading.Thread(
        target=_drain_stream,
        args=(proc.stdout, ingestor),
        name="bash-fg-drain",
        daemon=True,
    )
    reader.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    aborted = False
    while True:
        if abort_event is not None and abort_event.is_set():
            aborted = True
            break
        if proc.poll() is not None:
            # The shell exited. Give the reader a moment to flush the last
            # buffered bytes before we tear down the group.
            reader.join(timeout=_POST_EXIT_DRAIN_SECONDS)
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(_POLL_INTERVAL_SECONDS)

    # Always tear the whole group down: on timeout/abort to stop the command, on
    # normal exit to reap any grandchild it backgrounded (which would otherwise
    # keep the pipe open and hang this read, the classic 'server &' bug).
    if timed_out or aborted:
        _terminate_process_group(proc, pgid)
    else:
        _kill_process_group(proc, pgid)

    reader.join(timeout=_POST_KILL_DRAIN_SECONDS)
    try:
        exit_code = proc.wait(timeout=1.0)
    except Exception:  # noqa: BLE001
        exit_code = proc.returncode  # None = unknown (wait timed out)
    ingestor.close()

    return _format_foreground_result(
        command, ingestor, exit_code, timed_out, aborted, timeout_seconds
    )


def _drain_stream(stream, ingestor: "_OutputIngestor") -> None:
    """Read a pipe to EOF into the ingestor. Runs on a daemon thread; returns
    when the write ends close (which the group-kill guarantees)."""
    if stream is None:
        return
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            ingestor.feed(chunk)
    except (OSError, ValueError):
        pass  # pipe torn down by the group kill; EOF/closed-fd here is expected
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


class _OutputIngestor:
    """Bounded-memory, bounded-disk capture of a command's output.

    Keeps a rolling tail (the last ``_TAIL_KEEP_BYTES``) for the model and, once
    the full output crosses ``_SPILL_THRESHOLD_BYTES``, streams the output to a
    spill file (capped at ``_SPILL_MAX_BYTES``). Small outputs never touch disk.
    Fed from the single drain thread; a lock makes the read/close side safe even
    if that thread outlives its timed join (e.g. a D-state process pinning the
    pipe open past the group SIGKILL).
    """

    def __init__(self, spill_target) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._tail = bytearray()
        self._head = bytearray()
        self._spill_fh = None
        self._spill_target = spill_target
        self._spill_disabled = False
        self._spill_written = 0
        self.spill_path: Optional[str] = None
        self.spill_truncated = False
        self.total_bytes = 0

    def feed(self, chunk: bytes) -> None:
        with self._lock:
            if self._closed:
                return
            self.total_bytes += len(chunk)

            self._tail.extend(chunk)
            excess = len(self._tail) - _TAIL_KEEP_BYTES
            if excess > 0:
                del self._tail[:excess]

            if self._spill_fh is not None:
                self._write_spill(chunk)
                return
            if self._spill_disabled:
                return
            self._head.extend(chunk)
            if len(self._head) > _SPILL_THRESHOLD_BYTES:
                self._open_spill()

    def _write_spill(self, chunk: bytes) -> None:
        fh = self._spill_fh
        if fh is None:
            return
        remaining = _SPILL_MAX_BYTES - self._spill_written
        if remaining <= 0:
            self.spill_truncated = True
            return
        part = chunk[:remaining]
        try:
            fh.write(part)
            self._spill_written += len(part)
        except OSError:
            return
        if len(chunk) > len(part):
            self.spill_truncated = True

    def _open_spill(self) -> None:
        target = None
        try:
            target = self._spill_target()
        except Exception:  # noqa: BLE001
            target = None
        if target is None:
            # Cannot spill (no thread dir and temp-file creation failed): cap the
            # head so memory stays bounded; the model still gets the rolling tail.
            self._spill_disabled = True
            del self._head[:-_SPILL_THRESHOLD_BYTES]
            return
        path, fh = target
        try:
            fh.write(self._head)
        except OSError:
            try:
                fh.close()
            except OSError:
                pass  # best-effort close of a spill file that already failed to write
            self._spill_disabled = True
            return
        self._spill_fh = fh
        self._spill_written = len(self._head)
        self.spill_path = path
        self._head = bytearray()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._spill_fh is not None:
                try:
                    self._spill_fh.close()
                except OSError:
                    pass  # close is best-effort; the data already on disk stays readable

    def tail_bytes(self) -> bytes:
        with self._lock:
            return bytes(self._tail)

    def full_head_bytes(self) -> Optional[bytes]:
        """Full output when it was never spilled, else None."""
        with self._lock:
            if self._spill_fh is not None or self._spill_disabled:
                return None
            return bytes(self._head)


def _make_spill_target(thread_id: Optional[str]):
    """Return a zero-arg callable that lazily opens a spill file and returns
    ``(path, file_handle)`` or ``None``. Prefers the auto-cleaned per-thread
    command dir (like web_fetch), falling back to a swept system temp file."""

    def _open():
        job = secrets.token_hex(4)
        if thread_id:
            try:
                from ..core.attachment_sandbox import get_thread_command_dir

                path = get_thread_command_dir(thread_id) / f"bash-{job}.log"
                fh = open(path, "wb")
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass  # tightening perms is best-effort; the dir is already per-thread
                return str(path), fh
            except Exception:  # noqa: BLE001 - fall back to system temp
                pass
        try:
            # Temp-dir spills have no thread lifecycle owner; opportunistically
            # age-sweep old ones before adding another.
            sweep_stale_output_files()
            fh = tempfile.NamedTemporaryFile(
                mode="wb", prefix=f"nymeria-bash-{job}-", suffix=".log", delete=False
            )
            return fh.name, fh
        except Exception:  # noqa: BLE001
            return None

    return _open


def _format_foreground_result(
    command: str,
    ingestor: "_OutputIngestor",
    exit_code: Optional[int],
    timed_out: bool,
    aborted: bool,
    timeout_seconds: int,
) -> str:
    full = ingestor.full_head_bytes()
    if full is not None:
        body = _clean_output_text(full.decode("utf-8", errors="replace"))
        if not body.strip():
            body = "[No output]"
    else:
        body = _clean_output_text(ingestor.tail_bytes().decode("utf-8", errors="replace"))
        if len(body) > MAX_OUTPUT_CHARS:
            body = body[-MAX_OUTPUT_CHARS:]
            nl = body.find("\n")
            if 0 <= nl < 200:  # drop a leading partial line
                body = body[nl + 1:]
        if ingestor.spill_path and ingestor.spill_truncated:
            location = (
                f"The first {_SPILL_MAX_BYTES} bytes are saved to "
                f"{ingestor.spill_path} (output exceeded the save cap; read it "
                "with file_read, or grep/sed it with bash_execute)."
            )
        elif ingestor.spill_path:
            location = (
                f"Full output saved to {ingestor.spill_path} (read it with "
                "file_read, or grep/sed it with bash_execute)."
            )
        else:
            location = "Full output was too large to save; only the tail is shown."
        body = (
            f"[Output truncated: {ingestor.total_bytes} bytes total, showing the "
            f"last ~{len(body)} characters. {location}]\n\n{body}"
        )

    parts = [body]
    if timed_out:
        parts.append(
            f"[Error]: Command timed out after {timeout_seconds}s and was "
            "terminated along with everything it spawned (exit code 124)."
        )
    elif aborted:
        parts.append(
            "[Aborted]: Command was cancelled and terminated along with "
            "everything it spawned (exit code 130)."
        )
    elif exit_code is None:
        parts.append("[exit code: unknown (process state could not be read)]")
    elif exit_code != 0:
        meaning = _annotate_exit_code(command, exit_code)
        suffix = f" - {meaning}" if meaning else ""
        parts.append(f"[exit code: {exit_code}{suffix}]")
    return "\n".join(parts)


# --- output cleaning / annotation ---------------------------------------------


def _clean_output_text(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    return "".join(ch if ch in "\n\r\t" or ord(ch) >= 32 else "?" for ch in text)


# Common non-zero exits that are not real errors, keyed by the leading tool.
_EXIT_CODE_MEANINGS = {
    ("grep", 1): "no matches found (not an error)",
    ("egrep", 1): "no matches found (not an error)",
    ("fgrep", 1): "no matches found (not an error)",
    ("rg", 1): "no matches found (not an error)",
    ("ag", 1): "no matches found (not an error)",
    ("diff", 1): "files differ (expected)",
    ("cmp", 1): "files differ (expected)",
    ("test", 1): "condition was false (not an error)",
    ("[", 1): "condition was false (not an error)",
}


def _annotate_exit_code(command: str, exit_code: int) -> str:
    """Best-effort hint for a non-zero exit code so the model does not waste a
    turn treating an expected code as a failure."""
    if exit_code == 124:
        return "timed out"
    if exit_code == 130:
        return "interrupted (SIGINT)"
    if exit_code < 0:
        return f"killed by signal {-exit_code}"
    # Leading verb of the last command in a pipeline/sequence.
    tail_cmd = re.split(r"\|\||&&|[;\n|&]", command)[-1].strip()
    tail_cmd = re.sub(r"^[\s(){]*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*", "", tail_cmd)
    verb = (tail_cmd.split() or [""])[0]
    verb = verb.rsplit("/", 1)[-1]  # strip any path
    return _EXIT_CODE_MEANINGS.get((verb, exit_code), "")


# --- process-group control ----------------------------------------------------


def _safe_pgid(proc) -> Optional[int]:
    if not hasattr(os, "getpgid"):
        return None
    try:
        return os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return None


def _terminate_process_group(proc, pgid: Optional[int]) -> None:
    """SIGTERM the group, wait a grace period, then SIGKILL. Never raises."""
    try:
        if pgid is not None and hasattr(os, "killpg"):
            os.killpg(pgid, signal.SIGTERM)
        elif proc.poll() is None:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        return

    end = time.monotonic() + _TERM_GRACE_SECONDS
    while time.monotonic() < end:
        if proc.poll() is not None:
            break
        time.sleep(_POLL_INTERVAL_SECONDS)
    # Always follow with a group SIGKILL: even if the shell was reaped during the
    # grace window, a grandchild it spawned may still be alive.
    _kill_process_group(proc, pgid)


def _kill_process_group(proc, pgid: Optional[int]) -> None:
    """SIGKILL the whole group (falls back to the single child). Never raises."""
    try:
        if pgid is not None and hasattr(os, "killpg"):
            os.killpg(pgid, signal.SIGKILL)
        elif proc.poll() is None:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass  # group already gone (or unkillable); nothing further to clean up


# --- environment / abort ------------------------------------------------------


def _scrubbed_env() -> dict:
    names = set(_ENV_PASSTHROUGH)
    try:
        extra = get_settings().bash_env_passthrough or ""
        names.update(part.strip() for part in extra.split(",") if part.strip())
    except Exception:  # noqa: BLE001 - a settings hiccup must not break the tool
        pass
    return {name: os.environ[name] for name in names if name in os.environ}


def _get_abort_event(thread_id: Optional[str]):
    """The thread's cancellation Event, so a running command dies on user Stop."""
    if not thread_id:
        return None
    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return None
        return agent._thread_locks.get_abort_event(thread_id)
    except Exception:  # noqa: BLE001
        return None


# --- background execution -----------------------------------------------------


def _launch_tracked_background(
    command: str, cwd_str: str, thread_id: str, user_id: str
) -> str:
    # Best-effort housekeeping of old spill files before creating new ones.
    sweep_stale_output_files()

    job_id = secrets.token_hex(4)
    stdout_path = ""
    stderr_path = ""
    stdout_file = None
    stderr_file = None
    try:
        stdout_path, stderr_path, stdout_file, stderr_file = open_output_files(job_id)
        kwargs = _background_popen_kwargs(cwd_str, stdout=stdout_file, stderr=stderr_file)
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
        "A completion notification will be submitted to this thread when the "
        "process exits. To check on it in the meantime, bind and use the "
        "bash_job tool (action=status/log/kill). Temp files are swept after a "
        "few days. If the backend process restarts first, no completion "
        "notification will be sent."
    )


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
        "env": _scrubbed_env(),
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
