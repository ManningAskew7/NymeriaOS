"""Bias the Linux OOM killer toward tool subprocesses, away from the API.

When the API container's memory cgroup is exhausted, the kernel OOM killer
evicts the process with the highest ``oom_score`` (roughly resident size plus
``oom_score_adj``). The agent runtime is normally the largest resident process
in the cgroup, so without help the kernel kills the *API server itself* rather
than the tool subprocess whose allocation triggered the spike, taking down the
whole agent runtime over one heavy tool call.

Raising a tool subprocess's ``oom_score_adj`` (a positive bump, which needs no
extra capabilities even in a cap-dropped container) flips that ranking: under
pressure the kernel evicts the offending tool first, the API survives, and the
tool call simply returns an error so the turn can continue.

Linux-only: a no-op on other platforms, and best-effort if ``/proc`` is
unavailable (it must never block or fail a spawn).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable, Optional

# Imported rather than redeclared: process_hardening is what CLEARS this flag,
# and the preexec below is what restores it per child. One constant, one
# mechanism.
from .process_hardening import _PR_GET_DUMPABLE, PR_SET_DUMPABLE

# 0..1000. Large enough that a modest tool subprocess outranks the much larger
# API server in the kernel's per-process OOM scoring, without pinning it to the
# absolute maximum (1000, "always kill first") so genuinely runaway children
# still rank above well-behaved ones.
TOOL_SUBPROCESS_OOM_SCORE_ADJ = 700

# Bytes path: os.open skips fsencode on a bytes path, so the post-fork child
# does no Python-level allocation for the path.
_OOM_SCORE_PATH = b"/proc/self/oom_score_adj"


def _resolve_prctl():
    """Bind libc's ``prctl`` at IMPORT time, and warm it.

    Every part of this is about what must not happen after a fork.
    ``preexec_fn`` runs in a single-threaded copy of a ~30-thread process, so
    any lock another thread held at fork time is held forever there. Measured:
    with a sibling thread mid-``import ctypes``, a spawn whose preexec imported
    ctypes never returned at all. Not a lost child, a wedged caller, because
    ``subprocess.run(timeout=)`` bounds ``communicate()`` and not the fork/exec
    handshake. ``exec_sandbox.py`` documents the same hazard as its reason for
    using a shim rather than a preexec.

    So: module scope, not per-call (this used to run on every spawn), and a
    warming call so the first real invocation is not also a lazy PLT resolution
    taking the loader lock in the child. ``PR_GET_DUMPABLE`` is a pure read.
    """
    if sys.platform != "linux":
        return None
    try:
        import ctypes

        prctl = ctypes.CDLL("libc.so.6", use_errno=True).prctl
        prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0)
        return prctl
    except BaseException:  # noqa: BLE001 - see below; must never break import
        # Deliberately BaseException. A Python built without _ctypes raises
        # ImportError, musl has no libc.so.6 (OSError), and a constrained
        # environment can raise things neither class covers. None of that may
        # stop the module importing or a tool from running; the cost of failure
        # here is only that children keep the parent's OOM score.
        return None


_PRCTL = _resolve_prctl()


def oom_score_preexec(
    score: int = TOOL_SUBPROCESS_OOM_SCORE_ADJ,
) -> Optional[Callable[[], None]]:
    """Return a ``preexec_fn`` that raises the child's OOM score.

    Returns ``None`` where it does not apply (non-Linux), which is the default
    for ``subprocess`` ``preexec_fn`` and so is safe to pass through verbatim.

    The returned callable runs in the forked child just before ``exec``. The
    score and the ``/proc`` path are pre-encoded to bytes so the child does no
    Python-level allocation, and it swallows errors so it can never fail the
    spawn. As with any ``preexec_fn`` it runs post-fork in a possibly
    multithreaded parent, so it is kept deliberately minimal; the stdlib offers
    no allocation-free parameter for ``oom_score_adj``, making this the
    pragmatic choice.
    """
    if sys.platform != "linux":
        return None

    payload = str(int(score)).encode("ascii")

    def _preexec() -> None:
        # One try, covering everything, and catching BaseException. Anything
        # that escapes a preexec_fn is re-raised in the PARENT as
        # subprocess.SubprocessError, so a narrow except here does not degrade
        # the OOM bump, it kills the spawn. On a Python built without _ctypes
        # that would be every bash, MCP, workflow and Claude Code call in the
        # process. The docstring's promise that this can never fail a spawn has
        # to be enforced, not just stated.
        try:
            # Restore dumpability in the CHILD before touching /proc/self.
            #
            # The parent (API/slim) sets PR_SET_DUMPABLE(0) at startup so a
            # same-user process cannot read its environment. That flag is
            # inherited across fork and reassigns the child's own /proc/self
            # entries to root, so the write below fails with EACCES and the OOM
            # biasing silently stops working: measured, the child ends up at 0
            # instead of 700, and the kernel goes back to evicting the
            # largest-RSS process, which is the API itself. Exactly what this
            # module exists to prevent.
            #
            # What the window between here and execve exposes is the forked
            # ADDRESS SPACE, still a COW copy of the parent's and holding
            # everything the parent held, master key included. The caller's
            # environment scrub does not cover that; it governs what execve
            # hands over. Acceptable because it lasts microseconds, needs a
            # same-uid attacker already racing this exact fork, and execve
            # resets dumpable to 1 for an ordinary binary immediately
            # afterwards anyway. An attempt to read /proc/<child>/environ and
            # maps during the window was denied on kernel 6.8 even with
            # dumpable restored, with a control proving the read works
            # post-exec.
            if _PRCTL is not None:
                _PRCTL(PR_SET_DUMPABLE, 1, 0, 0, 0)
            fd = os.open(_OOM_SCORE_PATH, os.O_WRONLY)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        except BaseException:  # noqa: BLE001 - see above
            pass

    return _preexec


def with_tool_oom_score(
    kwargs: dict[str, Any],
    score: int = TOOL_SUBPROCESS_OOM_SCORE_ADJ,
) -> dict[str, Any]:
    """Merge a tool-subprocess OOM ``preexec_fn`` into ``subprocess`` kwargs.

    Mutates and returns ``kwargs`` (for chaining). No-op on non-Linux and when
    the caller already set a ``preexec_fn`` (we never clobber an existing one).
    """
    if kwargs.get("preexec_fn") is not None:
        return kwargs
    preexec = oom_score_preexec(score)
    if preexec is not None:
        kwargs["preexec_fn"] = preexec
    return kwargs
