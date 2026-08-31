"""Terminate the child processes this API process owns, before it goes away.

Called from two places, for one reason: when the API process ends, whether it
is replaced in place by ``/restart api`` or exits cleanly, the work it spawned
should end with it.

Nothing used to do this, and until #300 nothing had to. Every restart exited
the process, so the OS cleaned up: the container's PID-namespace teardown, or
systemd's ``KillMode=control-group`` kill. Making the restart in-place
(``os.execve``, same pid) removed that, and removed it silently. The new image
inherits the children but not the registries or watcher threads that owned
them, so a background bash job ran to completion into nothing: no completion
prompt, no autonomous turn, output in a temp file nobody would read, and a
zombie against a pid that no longer dies. Backlog #303 chose to restore the old
effective behavior deliberately rather than adopt the orphans.

The clean-exit path has the same hole for the same reason, minus a supervisor
to cover it: children are spawned with ``start_new_session=True``, so a
terminal Ctrl-C never reaches them.

Scope, honestly stated. This kills what a registry can name: tracked background
bash jobs and MCP stdio servers. Claude Code bridge runs, foreground bash and
hook ``run_command`` children have no durable handle to kill, but all three are
pipe-based, so replacing or exiting this process closes their pipes and they
take EPIPE/EOF; the reap sweep then collects them. Giving those a registry is
a feature, not this.
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

__all__ = ["terminate_owned_children"]

# How long the whole teardown may take before it gives up on the rest. Each
# process-group kill costs up to a second of grace, so without a ceiling a
# deployment with many background jobs could hold a restart open indefinitely.
TEARDOWN_BUDGET_SECONDS = 5.0


def terminate_owned_children() -> dict[str, int]:
    """Kill the children this process owns, then reap them. Never raises.

    Returns a small summary for logging. Every step is individually guarded:
    this runs while the process is on its way out, and on the restart path it
    sits after the point of no return, so a failure here must never be able to
    strand a restart that has already stopped the ticker.
    """
    deadline = time.monotonic() + TEARDOWN_BUDGET_SECONDS
    summary = {"bash_jobs": 0, "reaped": 0}
    killed: list[int] = []

    try:
        from ..tools.bash_background import terminate_running_jobs

        killed = terminate_running_jobs(deadline)
        summary["bash_jobs"] = len(killed)
    except Exception:  # noqa: BLE001 - teardown is best-effort by contract
        logger.warning("Background bash teardown failed", exc_info=True)

    try:
        from .mcp_manager import shutdown_mcp_manager

        shutdown_mcp_manager()
    except Exception:  # noqa: BLE001
        logger.warning("MCP server teardown failed", exc_info=True)

    summary["reaped"] = _reap(killed)

    if summary["bash_jobs"] or summary["reaped"]:
        logger.info(
            "Child teardown: terminated %d background job(s), reaped %d",
            summary["bash_jobs"],
            summary["reaped"],
        )
    return summary


def _reap(pids: list[int]) -> int:
    """Collect the processes this teardown killed, so none is left a zombie.

    Targeted, and deliberately NOT a blanket ``waitpid(-1)``. A blanket sweep
    would harvest any child that happens to exit during it, and CPython's
    ``Popen`` substitutes exit status 0 when its own ``wait()`` then gets
    ECHILD, so the owner would record a clean exit for a process it never saw
    finish. On the clean-shutdown path the process keeps running afterwards,
    so that corrupted status is not academic: the victim could be a Claude Code
    bridge run or a foreground command that had nothing to do with the
    teardown.

    Non-blocking: it only harvests what is already dead, since each pid was
    just SIGKILLed and its own watcher usually reaps it first (that race is
    fine, ``ChildProcessError`` simply means someone got there first). What
    this covers is the case where no watcher exists, or where the exec replaces
    the image before one wakes up, leaving a zombie against a pid that (since
    the restart became in-place) no longer dies.
    """
    if not hasattr(os, "waitpid"):
        return 0  # Windows: no POSIX wait semantics, and no zombies either
    reaped = 0
    for pid in pids:
        try:
            collected, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue  # already reaped by its own watcher
        except OSError:
            logger.debug("Could not reap pid %s", pid, exc_info=True)
            continue
        if collected:
            reaped += 1
    return reaped
