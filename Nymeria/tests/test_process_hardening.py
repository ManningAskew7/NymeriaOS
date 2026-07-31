"""The agent runtime's environment is not readable from a same-user process.

Scrubbing what children inherit is only half the job: on Linux a same-UID
process can read ``/proc/<pid>/environ`` of the parent and recover every secret
the scrub withheld. These tests check the flag that closes that.

The load-bearing test spawns a real child and has it attempt the read, because
the alternative (assert the function returned True) would pass just as happily
against a kernel or sandbox that accepted the prctl and ignored it.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import textwrap

import pytest

from nymeria.process_hardening import DISABLE_ENV_VAR, restrict_proc_access

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="prctl is Linux-only"
)

# The child reports whether it could read the parent's environ. Run in a
# subprocess so the hardening applies to a throwaway process, never to the
# pytest process itself (which would break later ptrace-based tooling and, more
# practically, cannot be undone within the run).
# No .format() on this: the root path arrives via the environment instead, so
# there is exactly one templating layer and the child's own braces survive.
_PROBE = textwrap.dedent(
    """
    import os, subprocess, sys
    sys.path.insert(0, os.environ["NYMERIA_TEST_ROOT"])
    # NB the canary is placed in this process's env by the PARENT, at spawn
    # time. Setting it here would not work: /proc/<pid>/environ is the kernel's
    # snapshot of the initial stack, so a later putenv is invisible there. That
    # is also why the real leak is worth closing, since the secrets the API
    # holds are present from exec.
    from nymeria.process_hardening import restrict_proc_access
    applied = restrict_proc_access()
    # A child of THIS process, same UID, trying the back door.
    reader = (
        "import sys\\n"
        "try:\\n"
        "    sys.stdout.write(open('/proc/%d/environ','rb')"
        ".read().decode('utf-8','replace'))\\n"
        "except Exception as e:\\n"
        "    sys.stdout.write('DENIED:' + type(e).__name__)\\n"
    ) % os.getpid()
    out = subprocess.run(
        [sys.executable, "-c", reader], capture_output=True, text=True
    )
    print("APPLIED", applied)
    print("LEAKED", "sentinel-value-do-not-leak" in out.stdout)
    """
)


def _run_probe(env_extra: dict[str, str] | None = None) -> dict[str, str]:
    root = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["NYMERIA_TEST_ROOT"] = root
    env["NYMERIA_TEST_CANARY"] = "sentinel-value-do-not-leak"
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return dict(
        line.split(" ", 1) for line in proc.stdout.strip().splitlines() if " " in line
    )


@linux_only
def test_a_child_cannot_read_the_parents_environment():
    """The actual property, proven by a real child doing the real read."""
    result = _run_probe()
    assert result["APPLIED"] == "True"
    assert result["LEAKED"] == "False", (
        "a same-user child read the parent's environ despite PR_SET_DUMPABLE"
    )


@linux_only
def test_without_hardening_the_child_can_read_it():
    """The control. Without this, the test above proves nothing.

    If the probe could never read the parent's environ (wrong path, wrong
    quoting, a kernel that hides it anyway), the primary test would pass while
    testing nothing at all. This asserts the leak is real when the flag is off,
    which is what makes its absence meaningful.
    """
    result = _run_probe({DISABLE_ENV_VAR: "1"})
    assert result["APPLIED"] == "False"
    assert result["LEAKED"] == "True", (
        "the probe could not read the parent's environ even with hardening "
        "disabled, so it is not exercising the channel it claims to"
    )


@linux_only
def test_the_flag_is_actually_set_not_merely_reported():
    """Read the flag back, since a stubbed prctl can return success silently."""
    code = textwrap.dedent(
        f"""
        import ctypes, sys
        sys.path.insert(0, {str(__import__("pathlib").Path(__file__).resolve().parent.parent)!r})
        from nymeria.process_hardening import restrict_proc_access
        restrict_proc_access()
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        print(libc.prctl(3, 0, 0, 0, 0))  # PR_GET_DUMPABLE
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "0"


def test_it_never_raises_on_an_unsupported_platform(monkeypatch):
    """Startup must not fail because a hardening step was unavailable."""
    monkeypatch.setattr(
        ctypes, "CDLL", lambda *a, **k: (_ for _ in ()).throw(OSError("no libc"))
    )
    if sys.platform.startswith("linux"):
        assert restrict_proc_access() is False


def test_the_escape_hatch_is_honoured(monkeypatch):
    """Debugging hosts need ptrace back, and that must not require a code edit."""
    monkeypatch.setenv(DISABLE_ENV_VAR, "1")
    assert restrict_proc_access() is False
