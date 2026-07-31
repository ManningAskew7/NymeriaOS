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
from .process_hardening import PR_SET_DUMPABLE

# 0..1000. Large enough that a modest tool subprocess outranks the much larger
# API server in the kernel's per-process OOM scoring, without pinning it to the
# absolute maximum (1000, "always kill first") so genuinely runaway children
# still rank above well-behaved ones.
TOOL_SUBPROCESS_OOM_SCORE_ADJ = 700

# Bytes path: os.open skips fsencode on a bytes path, so the post-fork child
# does no Python-level allocation for the path.
_OOM_SCORE_PATH = b"/proc/self/oom_score_adj"


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

    # Resolve libc HERE, in the parent, not inside the child below.
    #
    # ``import ctypes`` and ``CDLL`` both run real Python and a ``dlopen``,
    # which takes the import lock and glibc's loader lock. ``preexec_fn`` runs
    # post-fork in a process that has only the calling thread, so a lock any
    # OTHER thread held at fork time is held forever in the child: the textbook
    # post-fork deadlock, and it would hang a tool call rather than fail it.
    # The parent-side call also warms the loader so the bound ``prctl`` below
    # is a plain function pointer by the time the child touches it.
    try:
        import ctypes

        _prctl = ctypes.CDLL("libc.so.6", use_errno=True).prctl
    except (OSError, AttributeError):  # pragma: no cover - no glibc
        _prctl = None

    def _preexec() -> None:
        # Restore dumpability in the CHILD before touching /proc/self.
        #
        # The parent (API/slim) sets PR_SET_DUMPABLE(0) at startup so a
        # same-user process cannot read its environment. That flag is inherited
        # across fork, and it reassigns the child's own /proc/self entries to
        # root, so this write fails with EACCES and the OOM biasing silently
        # stops working: measured, the child ends up at 0 instead of 700, and
        # the kernel goes back to evicting the largest-RSS process, which is
        # the API itself. Exactly what this module exists to prevent.
        #
        # What the window between here and execve exposes is the forked ADDRESS
        # SPACE, which is still a copy of the parent's and holds everything the
        # parent held, master key included. It is not closed by the caller's
        # environment scrubbing, which governs what execve hands over. It is
        # acceptable because it lasts microseconds, needs a same-uid attacker
        # already racing this exact fork, and execve resets dumpable to 1 for
        # an ordinary binary immediately afterwards anyway: the flag was never
        # going to survive into the process anyone could actually attach to.
        if _prctl is not None:
            _prctl(PR_SET_DUMPABLE, 1, 0, 0, 0)
        try:
            fd = os.open(_OOM_SCORE_PATH, os.O_WRONLY)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        except OSError:
            # Best effort only: a cgroup without the knob, or a race on exec,
            # must not stop the tool from running.
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
