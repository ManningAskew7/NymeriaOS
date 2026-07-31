"""Stop same-user processes reading the agent runtime's environment.

The API process holds the vault master key, the service token, and the
database, Redis and provider credentials in its environment. Scrubbing what
child processes *inherit* (see :mod:`nymeria.subprocess_env`) closes the direct
handoff, but not the back door: on Linux, ``/proc/<pid>/environ`` of a process
with the same real UID is readable by default, so a shell started by
``bash_execute`` can simply read the parent's environment out of ``/proc`` and
recover everything the scrub withheld.

``prctl(PR_SET_DUMPABLE, 0)`` closes that. It reassigns the ownership of the
process's ``/proc/<pid>`` entries to root, which makes ``environ``, ``mem``,
``maps`` and friends unreadable to a same-UID process, and blocks ptrace attach
from one. This is a cheap, shape-independent layer that does not depend on
containers, LSMs, or a sandbox being wired.

**What it does not do.** An attacker who is root, holds ``CAP_SYS_PTRACE``, or
can read the encrypted stores off disk is unaffected: the key material is still
on the same host as the process using it. This narrows one channel; it is not
isolation.

**Cost, stated plainly.** The flag's original purpose is suppressing core
dumps, so this process will not produce one on a crash. Profilers and debuggers
that attach via ptrace (py-spy, gdb) stop working from an unprivileged same-UID
shell. They keep working as root or with ``CAP_SYS_PTRACE``, which is how the
documented container workflow already invokes them
(``docker exec --privileged ... py-spy dump --pid 1``). If you need the
unprivileged path back on a debugging host, set
``NYMERIA_DISABLE_PROCESS_HARDENING=1``.

Linux-only, and best-effort everywhere: this must never prevent the API from
starting. A platform without ``prctl``, a missing libc symbol, or a refusing
kernel all fall through to a debug log.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

logger = logging.getLogger(__name__)

# From <linux/prctl.h>. Stable kernel ABI constants, safe to inline; the two
# in exec_sandbox.py are inlined the same way.
_PR_SET_DUMPABLE = 4
_PR_GET_DUMPABLE = 3

DISABLE_ENV_VAR = "NYMERIA_DISABLE_PROCESS_HARDENING"


def _disabled() -> bool:
    return os.environ.get(DISABLE_ENV_VAR, "").strip().lower() in {"1", "true", "yes"}


def restrict_proc_access() -> bool:
    """Make this process's ``/proc`` entries unreadable to same-UID processes.

    Returns True if the restriction is in place, False if it was skipped or
    could not be applied. Never raises.

    Call this ONCE at startup, before spawning anything. Order matters for a
    reason that is easy to miss: the flag protects the *parent's* ``/proc``
    entry, so a child spawned beforehand has already had its window. It is
    reset to 1 across ``execve`` for the child itself, which is fine and
    expected, since what needs protecting is this process, not the child.
    """
    if not sys.platform.startswith("linux"):
        return False
    if _disabled():
        logger.info(
            "Process hardening disabled via %s: /proc/<pid>/environ of this "
            "process is readable by same-user processes, including tool shells",
            DISABLE_ENV_VAR,
        )
        return False
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            logger.debug(
                "prctl(PR_SET_DUMPABLE, 0) failed: errno %d", ctypes.get_errno()
            )
            return False
        # Read it back rather than trusting the return code. A sandbox or seccomp
        # filter can stub prctl to succeed without acting, and a hardening step
        # that silently did nothing is worse than one that logs a failure.
        if libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
            logger.warning(
                "prctl(PR_SET_DUMPABLE, 0) reported success but the flag did "
                "not change; /proc access is NOT restricted"
            )
            return False
    except (OSError, AttributeError) as exc:
        logger.debug("Process hardening unavailable: %s", exc)
        return False
    logger.debug("Process hardening applied: /proc entries restricted to root")
    return True


__all__ = ["DISABLE_ENV_VAR", "restrict_proc_access"]
