"""Spawn, supervise, and kill one workflow run; assemble its envelope.

The abort-gap closer the plan calls for: ``asyncio.create_subprocess_exec``
with ``start_new_session`` plus an unconditional process-group SIGKILL in
``finally``, so a wall-clock timeout, a turn abort (task cancellation), or a
parent error can never leak a running child (unlike the ``to_thread``-wrapped
``subprocess.run`` the Python custom-tool path uses).

Environment scrubbing follows the hooks run_command allowlist: the child sees
PATH/HOME/LANG/LC_ALL/TMPDIR plus run-identifying NYMERIA_WORKFLOW_* vars and
nothing else (no provider keys, no DB creds, no vault key).
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import shutil
import signal
import socket as socket_module
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .budget import BudgetUsage, WorkflowBudget
from .envelope import (
    ERROR_KINDS,
    KIND_RUNNER_ERROR,
    STATUS_NEEDS_APPROVAL,
    STATUS_OK,
    WorkflowEnvelope,
    WorkflowError,
    error_envelope,
    ok_envelope,
    timeout_envelope,
)
from .pump import FRAME_LIMIT_BYTES, VerbPump
from .registry import ApprovalRuntime, VerbContext, load_builtin_verbs
from .trace import StepTrace, persist_run_record

logger = logging.getLogger(__name__)

RUNNER_PATH = Path(__file__).with_name("runner.py")
_ENV_PASSTHROUGH = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
# Grace between the wall-clock cap and the child's own socket-IO timeout so
# the parent, not the child, is the one that decides a timeout.
_CHILD_IO_SLACK_SECONDS = 30.0
_STREAM_CHUNK = 65536


@dataclass
class WorkflowRunResult:
    envelope: WorkflowEnvelope
    trace: StepTrace

    def to_dict(self) -> dict:
        return {"envelope": self.envelope.to_dict(), "trace": self.trace.to_dict()}


def _discard_unfinished_suspension(approval: Optional[ApprovalRuntime]) -> None:
    """Drop a pending record whose run did not end cleanly suspended."""
    if approval is None or not approval.record_id:
        return
    try:
        from .approvals import delete_approval

        delete_approval(approval.record_id)
        logger.info(
            "discarded approval record %s (run did not suspend cleanly)",
            approval.record_id,
        )
    except Exception:  # noqa: BLE001 - cleanup must not mask the real outcome
        logger.warning("approval record cleanup failed", exc_info=True)
    approval.record_id = None
    approval.token = None


def _scrubbed_env(run_id: str, user_id: str) -> dict:
    env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    env["NYMERIA_WORKFLOW_RUN_ID"] = run_id
    env["NYMERIA_WORKFLOW_USER_ID"] = user_id
    return env


def _kill_process_group(proc, pgid: Optional[int]) -> None:
    """Best-effort SIGKILL of the child's whole group; never raises.

    Kills by ``pgid`` (captured at spawn, still valid after the main child is
    reaped) so a lingering GRANDCHILD the author backgrounded is reaped too,
    on EVERY exit path, not just timeout. Falls back to killing the main child
    where process groups are unavailable (Windows).
    """
    if proc is None:
        return
    try:
        if pgid is not None and hasattr(os, "killpg"):
            os.killpg(pgid, signal.SIGKILL)
        elif proc.returncode is None:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass  # group already gone


async def _write_stdin(proc, payload: bytes) -> None:
    """Write the bootstrap and close stdin; never raises (child may be gone)."""
    if proc.stdin is None:
        return
    try:
        proc.stdin.write(payload)
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # child already exited; the bootstrap is moot, not an error


async def _collect(task, fallback: str) -> str:
    """Collect a capture task's retained text; never raises.

    Called AFTER the process group is killed, so the log pipes' write ends are
    all closed and the reader hits EOF within milliseconds; awaiting it (with a
    short bound) both returns the buffered logs and lets the reader process the
    EOF, which is what allows the asyncio subprocess transport to close cleanly
    (a lingering grandchild would otherwise leave it half-open). On the rare
    timeout, ``wait_for`` cancels the task and we fall back.
    """
    if task is None:
        return fallback
    try:
        return await asyncio.wait_for(task, timeout=2.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return fallback
    except Exception:  # noqa: BLE001
        return fallback


async def _capture_stream(stream, cap: int) -> str:
    """Drain ``stream`` to EOF or cancellation, retaining at most ``cap`` chars.

    Reading is decoupled from completion detection (the child's exit, not pipe
    EOF, ends a run) and bounded at INGESTION: past ``cap`` we keep reading so
    a full pipe never blocks the child, but discard, so a chatty child cannot
    grow the PARENT's heap (the OOM bias only protects against child-side
    memory, not this). Returns whatever was retained when cancelled.
    """
    buf = bytearray()
    if stream is None:
        return ""
    try:
        while True:
            chunk = await stream.read(_STREAM_CHUNK)
            if not chunk:
                break
            if len(buf) < cap:
                buf.extend(chunk[: cap - len(buf)])
    except asyncio.CancelledError:
        pass  # cleanup cancelled the reader; return what was retained so far
    except Exception:  # noqa: BLE001 - a log read must never fail the run
        pass
    return buf.decode("utf-8", errors="replace")


async def _start_server(pump: VerbPump, rt_dir: Path):
    """A per-run listener: Unix socket where available, else loopback TCP.

    Returns ``(server, socket_spec)`` where the spec is the child's bootstrap
    connect instruction.
    """
    if hasattr(socket_module, "AF_UNIX"):
        path = str(rt_dir / "rpc.sock")
        server = await asyncio.start_unix_server(
            pump.serve, path=path, limit=FRAME_LIMIT_BYTES
        )
        return server, {"family": "unix", "path": path}
    server = await asyncio.start_server(
        pump.serve, host="127.0.0.1", port=0, limit=FRAME_LIMIT_BYTES
    )
    port = server.sockets[0].getsockname()[1]
    return server, {"family": "tcp", "host": "127.0.0.1", "port": port}


def _finish_to_envelope(
    finish: Optional[dict],
    usage_snapshot: dict,
    returncode,
    stderr_tail: str,
    approval: Optional[ApprovalRuntime] = None,
) -> WorkflowEnvelope:
    """Map the child's terminal report (or its absence) to the envelope."""
    if finish is None:
        detail = f" stderr: {stderr_tail.strip()}" if stderr_tail.strip() else ""
        return error_envelope(
            WorkflowError(
                kind=KIND_RUNNER_ERROR,
                message=(
                    "workflow child exited without a result "
                    f"(exit code {returncode}).{detail}"
                ),
            ),
            usage_snapshot,
        )
    status = str(finish.get("status") or "")
    if status == STATUS_OK:
        return ok_envelope(finish.get("output"), usage_snapshot)
    if status == STATUS_NEEDS_APPROVAL:
        # A suspension is only real when it matches the record the approve
        # verb minted for THIS run; the child cannot forge one (a phase 1
        # deferred item: never trust child-supplied finish fields).
        reported = str(finish.get("resume_token") or "")
        if (
            approval is None
            or not approval.record_id
            or not approval.token
            or not hmac.compare_digest(reported, approval.token)
        ):
            return error_envelope(
                WorkflowError(
                    kind=KIND_RUNNER_ERROR,
                    message=(
                        "the child reported a suspension the engine did not "
                        "mint; refusing the resume token"
                    ),
                ),
                usage_snapshot,
            )
        return WorkflowEnvelope(
            ok=False,
            status=STATUS_NEEDS_APPROVAL,
            # The public resume handle is the record id, resolved via
            # POST /workflows/approvals/{id}/resolve; the internal token
            # never leaves the engine.
            resume_token=approval.record_id,
            output={
                "record_id": approval.record_id,
                "prompt": approval.prompt,
                "expires_at": approval.expires_at,
            },
            budget=usage_snapshot,
        )
    error = finish.get("error") if isinstance(finish.get("error"), dict) else {}
    kind = str(error.get("kind") or KIND_RUNNER_ERROR)
    if kind not in ERROR_KINDS:
        kind = KIND_RUNNER_ERROR
    step = error.get("step")
    return error_envelope(
        WorkflowError(
            kind=kind,
            message=str(error.get("message") or "workflow failed"),
            step=int(step) if isinstance(step, int) else None,
            verb=str(error.get("verb") or "") or None,
            traceback=str(error.get("traceback") or "") or None,
        ),
        usage_snapshot,
    )


async def execute_workflow(
    *,
    source: str,
    entrypoint: str = "run",
    params: Optional[dict] = None,
    user_id: str,
    thread_id: str,
    workflow_id: str = "adhoc",
    budget: Optional[WorkflowBudget] = None,
    depth: int = 0,
    run_id: Optional[str] = None,
    persist_record: bool = True,
    approval: Optional[ApprovalRuntime] = None,
) -> WorkflowRunResult:
    """Run one workflow source end to end and return envelope plus trace.

    Never raises for anything the workflow did (author bugs, verb failures,
    caps): those are envelope statuses. It re-raises ``CancelledError`` after
    killing the child (a turn abort must keep cancelling upward), and lets
    genuine engine bugs propagate. ``approval`` (saved workflows only)
    enables ``nym.approve``; without it the verb refuses.
    """
    load_builtin_verbs()
    budget = budget or WorkflowBudget()
    run_id = run_id or uuid.uuid4().hex[:12]
    usage = BudgetUsage()
    ctx = VerbContext(
        user_id=user_id,
        thread_id=thread_id,
        run_id=run_id,
        workflow_id=workflow_id,
        depth=depth,
        budget=budget,
        usage=usage,
        approval=approval,
    )
    trace = StepTrace(run_id=run_id, workflow_id=workflow_id)
    token = secrets.token_urlsafe(32)
    pump = VerbPump(ctx=ctx, trace=trace, token=token)

    rt_dir = Path(tempfile.mkdtemp(prefix="nym-wf-"))
    server = None
    proc = None
    pgid: Optional[int] = None
    stdout_task = None
    stderr_task = None
    timed_out = False
    stdout_text = ""
    stderr_text = ""
    cap = budget.log_cap_chars
    try:
        server = await _start_server(pump, rt_dir)
        server, socket_spec = server
        bootstrap = {
            "socket": socket_spec,
            "token": token,
            "source": source,
            "entrypoint": entrypoint,
            "params": params or {},
            "run_id": run_id,
            "io_timeout_seconds": budget.wall_clock_seconds + _CHILD_IO_SLACK_SECONDS,
        }
        from ...oom import oom_score_preexec

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(RUNNER_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # The child needs no project files and runs untrusted author code;
            # keep its cwd in the ephemeral 0700 run dir, never data_dir (the
            # secret-dense store).
            cwd=str(rt_dir),
            env=_scrubbed_env(run_id, user_id),
            start_new_session=True,
            preexec_fn=oom_score_preexec(),
        )
        try:
            pgid = os.getpgid(proc.pid)
        except (OSError, AttributeError):
            pgid = None

        # Capture logs on their own bounded tasks; completion is the child's
        # EXIT (proc.wait), independent of whether the log pipes reach EOF, so
        # a backgrounded grandchild holding fd 1/2 cannot pin the run.
        stdout_task = asyncio.ensure_future(_capture_stream(proc.stdout, cap))
        stderr_task = asyncio.ensure_future(_capture_stream(proc.stderr, cap))
        await _write_stdin(proc, json.dumps(bootstrap, default=str).encode("utf-8"))

        # Completion is the FIRST of: the pump receiving the child's finish
        # frame (or the connection dropping), or the process exiting. We race
        # both because proc.wait() alone can block on pipe EOF a backgrounded
        # grandchild holds open (pump.wait_terminal covers that), while
        # wait_terminal alone never fires if the child dies before connecting
        # (proc.wait covers that). The wall clock bounds both.
        proc_wait = asyncio.ensure_future(proc.wait())
        terminal = asyncio.ensure_future(pump.wait_terminal())
        try:
            await asyncio.wait_for(
                asyncio.wait(
                    {proc_wait, terminal},
                    return_when=asyncio.FIRST_COMPLETED,
                ),
                timeout=budget.wall_clock_seconds,
            )
        except asyncio.TimeoutError:
            timed_out = True
            _kill_process_group(proc, pgid)
        finally:
            for pending in (proc_wait, terminal):
                if not pending.done():
                    pending.cancel()
    except asyncio.CancelledError:
        # Turn abort / operator stop: kill the whole group, then keep
        # cancelling upward. The finally below also runs, belt and braces.
        _kill_process_group(proc, pgid)
        _discard_unfinished_suspension(approval)
        raise
    except Exception as exc:  # noqa: BLE001 - spawn/listener failure is runner_error
        logger.warning("workflow run %s failed to launch", run_id, exc_info=True)
        usage.finish()
        envelope = error_envelope(
            WorkflowError(
                kind=KIND_RUNNER_ERROR,
                message=f"workflow engine failed to launch the run: {exc}",
            ),
            usage.snapshot(),
        )
        return WorkflowRunResult(envelope=envelope, trace=trace)
    finally:
        # Kill the whole group on every path (not just timeout): the main child
        # may have exited cleanly while leaving a backgrounded grandchild. This
        # runs before any await, so the child is dead even if a fresh
        # cancellation interrupts the cleanup awaits below.
        _kill_process_group(proc, pgid)
        # The nested finally guarantees the temp dir (holding the socket file)
        # is removed even if a cleanup await is cancelled; CancelledError still
        # propagates afterward.
        try:
            # A verb still in flight has no consumer once the child is dead, and
            # on an abort the cancellation cascade must reach it. Cancelling the
            # pump's handler tasks also lets wait_closed() return promptly.
            pump.abort()
            stdout_text = await _collect(stdout_task, stdout_text)
            stderr_text = await _collect(stderr_task, stderr_text)
            if proc is not None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except Exception:  # noqa: BLE001
                    pass
                # Close the subprocess transport explicitly so a grandchild that
                # held the pipes open cannot leave it to be reaped at GC (which
                # warns "Event loop is closed" if the loop is already gone).
                transport = getattr(proc, "_transport", None)
                if transport is not None:
                    try:
                        transport.close()
                    except Exception:  # noqa: BLE001
                        pass
            if server is not None:
                try:
                    server.close()
                    await asyncio.wait_for(server.wait_closed(), timeout=5.0)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            shutil.rmtree(rt_dir, ignore_errors=True)

    usage.finish()
    trace.stdout = stdout_text[:cap]
    trace.stderr = stderr_text[:cap]

    if timed_out:
        envelope = timeout_envelope(
            f"workflow exceeded its wall-clock cap ({budget.wall_clock_seconds:.0f}s)",
            usage.snapshot(),
        )
    else:
        envelope = _finish_to_envelope(
            pump.finish, usage.snapshot(), proc.returncode if proc else None,
            stderr_text[-1000:], approval,
        )

    # A run that minted a pending record but did not end suspended (author
    # swallowed the suspend signal, timeout, error) must not leave a
    # resumable orphan behind.
    if envelope.status != STATUS_NEEDS_APPROVAL:
        _discard_unfinished_suspension(approval)

    if persist_record:
        try:
            await asyncio.to_thread(
                persist_run_record,
                trace,
                envelope.to_dict(),
                user_id=user_id,
                thread_id=thread_id,
            )
        except Exception:  # noqa: BLE001 - observability must not fail the run
            logger.warning("workflow run-record persistence failed", exc_info=True)

    if envelope.status not in (STATUS_OK,):
        logger.info(
            "workflow run %s (%s) finished status=%s kind=%s",
            run_id,
            workflow_id,
            envelope.status,
            envelope.error.kind if envelope.error else "",
        )
    return WorkflowRunResult(envelope=envelope, trace=trace)
