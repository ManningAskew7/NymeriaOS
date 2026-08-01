"""Tests for the exec-surface sandbox policy (C1-01/C1-02).

Three tiers:

* the deny-set rules, which decide what a deployment can afford to deny;
* the launch rewrite, which is what actually connects the policy to a spawn;
* enforcement, which spawns a real sandboxed child and asserts the boundary
  holds. The enforcement tier skips loudly on a kernel without Landlock, so a
  passing run elsewhere never claims containment it did not prove.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.core import exec_policy
from nymeria.exec_sandbox import (
    DEFAULT_DEVICE_NODES,
    DEFAULT_SYSTEM_ROOTS,
    SandboxError,
    carved_policy,
    landlock_abi_version,
    sandbox_available,
    sandbox_env_overlay,
    wrap_argv,
)
from nymeria.tools.bash import bash_execute

requires_landlock = pytest.mark.skipif(
    not sandbox_available(),
    reason=f"Landlock unavailable (ABI {landlock_abi_version()}); enforcement not testable here",
)

# The tests that drive the SHIPPED entry points (bash_execute, the two runners)
# read the ambient setting rather than a monkeypatched one, so an operator with
# EXEC_SANDBOX_ENABLED=false exported would see them fail rather than skip.
# That is a false alarm, not a finding: the code is behaving as configured.
# ``requires_landlock`` guards the kernel; this guards the configuration.
requires_sandbox_on = pytest.mark.skipif(
    not exec_policy.get_settings().exec_sandbox_enabled,
    reason="EXEC_SANDBOX_ENABLED=false here; the shipped surfaces honour it, so "
    "enforcement is correctly absent rather than broken",
)

_PY_ROOTS = tuple({
    os.path.dirname(os.path.realpath(sys.executable)),
    os.path.realpath(sys.prefix),
    os.path.realpath(sys.base_prefix),
})


def _settings(monkeypatch, *, project_root: Path, data_dir: Path, enabled: bool = True):
    fake = SimpleNamespace(
        project_root=project_root,
        data_dir=data_dir,
        exec_sandbox_enabled=enabled,
    )
    monkeypatch.setattr(exec_policy, "get_settings", lambda: fake)
    # No _creation_roots patch: it derives from these settings plus the
    # launch's own cwd, so patching it would hide the rule under test.
    monkeypatch.setattr(exec_policy, "_dropped_logged", set())
    monkeypatch.setattr(exec_policy, "_expensive_logged", set())
    # pytest's tmp_path sits under a /tmp that can hold thousands of unrelated
    # entries, which the carve-cost guard would (correctly) refuse to carve.
    # The guard has its own test; here it must not stand in for the rule under
    # test.
    monkeypatch.setattr(exec_policy, "_MAX_CARVE_ENTRIES", 10**6)
    return fake


def _seed_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "accounts.db").write_text("VAULT", encoding="utf-8")
    (data_dir / "accounts.db-wal").write_text("VAULT-WAL", encoding="utf-8")
    (data_dir / "auth_tokens").mkdir(exist_ok=True)
    (data_dir / "auth_tokens" / "t.json").write_text("TOKEN", encoding="utf-8")
    (data_dir / "todos").mkdir(exist_ok=True)
    (data_dir / "todos" / "a.json").write_text("TODO", encoding="utf-8")


# --- deny set -----------------------------------------------------------------


def test_proc_is_always_denied(tmp_path, monkeypatch):
    _settings(monkeypatch, project_root=tmp_path, data_dir=tmp_path / "data")
    assert "/proc" in exec_policy.denied_paths()


def test_stores_outside_the_working_tree_are_denied(tmp_path, monkeypatch):
    """The Docker layout: project root and data dir are siblings."""
    project = tmp_path / "app"
    data = tmp_path / "data"
    project.mkdir()
    _seed_data_dir(data)
    _settings(monkeypatch, project_root=project, data_dir=data)

    denied = exec_policy.denied_paths()
    assert str(data / "accounts.db") in denied
    assert str(data / "auth_tokens") in denied
    # The SQLite sidecar carries recently written rows, so denying the DB file
    # and not its WAL would be a control with a hole in it.
    assert str(data / "accounts.db-wal") in denied
    assert str(data / "todos") not in denied


def test_stores_inside_the_working_tree_are_dropped(tmp_path, monkeypatch):
    """The source-checkout layout: the data dir lives under the project root.

    Denying there would make the working directory read-opaque for anything
    created after the policy was built, which costs more than it buys. The
    drop is the deliberate answer, so this asserts it rather than tolerating
    it, and the operator's remedy is to move the data dir out.
    """
    project = tmp_path / "project"
    data = project / "data"
    _seed_data_dir(data)
    (project / ".env").write_text("SECRET=1", encoding="utf-8")
    _settings(monkeypatch, project_root=project, data_dir=data)

    assert exec_policy.denied_paths() == ("/proc",)


def test_a_narrower_creation_root_keeps_the_stores_denied_on_a_source_layout(
    tmp_path, monkeypatch
):
    """The workflow runner's override, and the reason the knob exists.

    On a source checkout the data dir lives under the project root, so the
    default roots drop every store denial (the test above pins that, and for
    ``bash_execute`` it is the right answer). A runner whose child writes only
    in an ephemeral scratch dir does not need that concession, and taking it
    anyway would leave ``/proc`` as the entire policy on the slim shape.
    """
    project = tmp_path / "project"
    data = project / "data"
    _seed_data_dir(data)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _settings(monkeypatch, project_root=project, data_dir=data)

    # Default roots: project_root is one, so everything under it is dropped.
    assert exec_policy.denied_paths(cwd=str(run_dir)) == ("/proc",)

    narrowed = exec_policy.denied_paths(
        cwd=str(run_dir), creation_roots=(str(run_dir),)
    )
    assert str(data / "accounts.db") in narrowed
    assert str(data / "accounts.db-wal") in narrowed
    assert str(data / "auth_tokens") in narrowed


def test_argv_launch_threads_the_creation_root_override_to_the_policy(
    tmp_path, monkeypatch
):
    """The override has to survive the launch helper, not just the deny set.

    Wiring it in ``executor.py`` is worthless if the helper drops it on the
    way through, and that would be invisible: the launch still succeeds and the
    child still runs, just without the denials the caller asked for.
    """
    project = tmp_path / "project"
    data = project / "data"
    _seed_data_dir(data)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _settings(monkeypatch, project_root=project, data_dir=data)
    monkeypatch.setattr(exec_policy, "sandbox_available", lambda: True)

    seen: dict = {}
    real = exec_policy.tool_sandbox_policy

    def spy(cwd=None, *, creation_roots=None):
        seen["creation_roots"] = creation_roots
        seen["denied"] = exec_policy.denied_paths(cwd, creation_roots=creation_roots)
        # Build with the DEFAULT roots, whose deny set is just /proc here. The
        # override's real policy would carve this tmp_path's parents, and /tmp
        # on a dev box holds enough entries to blow the mechanism's own root
        # ceiling. What is under test is the threading, and that is `seen`.
        return real(cwd)

    monkeypatch.setattr(exec_policy, "tool_sandbox_policy", spy)
    kwargs = {"env": {"PATH": "/usr/bin"}, "cwd": str(run_dir)}

    exec_policy.sandbox_argv_launch(
        ["/bin/true"], kwargs, creation_roots=(str(run_dir),)
    )

    assert seen["creation_roots"] == (str(run_dir),)
    assert str(data / "accounts.db") in seen["denied"]
    assert kwargs["env"]["NYMERIA_SANDBOX_POLICY"]


def test_the_launch_cwd_is_never_carved(tmp_path, monkeypatch):
    """A command's own working directory is not made read-opaque under it.

    The deny rule is per launch and takes the cwd, so a command run somewhere
    outside the project root does not get that directory carved just because a
    store happens to sit above it. Without this, ``working_directory=<store's
    parent>`` would break exactly the commands the drop rule exists to protect.
    """
    project = tmp_path / "app"
    data = tmp_path / "work" / "data"
    project.mkdir()
    _seed_data_dir(data)

    _settings(monkeypatch, project_root=project, data_dir=data)
    assert str(data / "accounts.db") in exec_policy.denied_paths()
    assert exec_policy.denied_paths(cwd=str(tmp_path / "work")) == ("/proc",)


def test_dotenv_files_are_a_named_residual_not_a_denial(tmp_path, monkeypatch):
    """The dotenv files hold the same secrets as the environment, and stay readable.

    They are ``project_root/<name>`` by construction and project_root is always
    a creation root, so a dotenv denial could only ever be dropped: it would be
    dead code reading as coverage. Pinned as a test so that adding it back
    without first solving the carve problem fails here rather than shipping a
    control that never fires.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / ".env").write_text("SECRET=1", encoding="utf-8")
    _settings(monkeypatch, project_root=project, data_dir=tmp_path / "data")

    denied = exec_policy.denied_paths()
    assert str(project / ".env") not in denied
    assert not any(path.endswith(".env") for path in denied)


def test_policy_grants_readable_proc_files_back(tmp_path, monkeypatch):
    _settings(monkeypatch, project_root=tmp_path, data_dir=tmp_path / "data")
    policy = exec_policy.tool_sandbox_policy()
    assert "/proc" not in policy.read_write
    assert "/proc" not in policy.containers
    for path in exec_policy.PROC_READABLE_FILES:
        assert path in policy.read_only


# --- launch rewrite -----------------------------------------------------------


def test_launch_rewrites_shell_true_into_the_shim(tmp_path, monkeypatch):
    _settings(monkeypatch, project_root=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(exec_policy, "sandbox_available", lambda: True)
    kwargs = {"shell": True, "env": {"PATH": "/usr/bin"}, "cwd": str(tmp_path)}

    launch = exec_policy.sandbox_shell_launch("echo hi", kwargs)

    # shell=True with a LIST means something else entirely (the tail becomes
    # $0, $1, ...), so the rewrite has to turn it off.
    assert kwargs["shell"] is False
    assert launch[-3:] == ["/bin/sh", "-c", "echo hi"]
    assert launch[0] == sys.executable
    assert kwargs["env"]["NYMERIA_SANDBOX_POLICY"]
    assert kwargs["env"]["PATH"] == "/usr/bin"


def test_argv_launch_wraps_without_touching_shell(tmp_path, monkeypatch):
    """The argv-shaped sibling, for surfaces that never involve a shell.

    ``shell`` must be left alone rather than set False: these callers never
    passed it, and ``asyncio.create_subprocess_exec`` does not accept it at all.
    """
    _settings(monkeypatch, project_root=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(exec_policy, "sandbox_available", lambda: True)
    kwargs = {"env": {"PATH": "/usr/bin"}, "cwd": str(tmp_path)}

    launch = exec_policy.sandbox_argv_launch(["/usr/bin/python3", "runner.py"], kwargs)

    assert launch[-2:] == ["/usr/bin/python3", "runner.py"]
    assert launch[0] == sys.executable
    assert "shell" not in kwargs
    assert kwargs["env"]["NYMERIA_SANDBOX_POLICY"]
    assert kwargs["env"]["PATH"] == "/usr/bin"


def test_argv_launch_is_a_noop_when_the_setting_is_off(tmp_path, monkeypatch):
    _settings(monkeypatch, project_root=tmp_path, data_dir=tmp_path / "data", enabled=False)
    kwargs = {"env": {"PATH": "/usr/bin"}}

    assert exec_policy.sandbox_argv_launch(["a", "b"], kwargs) == ["a", "b"]
    assert kwargs == {"env": {"PATH": "/usr/bin"}}


def test_a_launch_without_an_explicit_env_is_refused(tmp_path, monkeypatch):
    """Never fall back to os.environ, in either helper.

    A helper that inherited would hand a child the API process's whole
    environment while reading as compliant to ``test_subprocess_env_gate.py``,
    whose variable tracking is per module and cannot see inside a helper. Every
    caller already scrubs, so the missing env is a bug, not a default.
    """
    _settings(monkeypatch, project_root=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(exec_policy, "sandbox_available", lambda: True)

    with pytest.raises(SandboxError):
        exec_policy.sandbox_argv_launch(["/bin/true"], {})
    with pytest.raises(SandboxError):
        exec_policy.sandbox_shell_launch("true", {})


def test_launch_is_a_noop_when_the_setting_is_off(tmp_path, monkeypatch):
    _settings(monkeypatch, project_root=tmp_path, data_dir=tmp_path / "data", enabled=False)
    kwargs = {"shell": True, "env": {"PATH": "/usr/bin"}}

    assert exec_policy.sandbox_shell_launch("echo hi", kwargs) == "echo hi"
    assert kwargs == {"shell": True, "env": {"PATH": "/usr/bin"}}


def test_launch_is_a_noop_when_the_kernel_cannot_enforce_it(tmp_path, monkeypatch):
    _settings(monkeypatch, project_root=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(exec_policy, "sandbox_available", lambda: False)
    monkeypatch.setattr(exec_policy, "_unavailable_warned", False)
    kwargs = {"shell": True, "env": {"PATH": "/usr/bin"}}

    assert exec_policy.sandbox_shell_launch("echo hi", kwargs) == "echo hi"
    assert "NYMERIA_SANDBOX_POLICY" not in kwargs["env"]


# --- enforcement --------------------------------------------------------------


@requires_landlock
def test_enforcement_tool_policy_hides_the_stores_and_keeps_the_rest(
    tmp_path, monkeypatch
):
    project = tmp_path / "app"
    data = tmp_path / "data"
    project.mkdir()
    _seed_data_dir(data)
    (project / "code.py").write_text("PROJECT", encoding="utf-8")
    _settings(monkeypatch, project_root=project, data_dir=data)

    script = (
        "import os\n"
        "def probe(fn):\n"
        "    try:\n"
        "        return str(fn())\n"
        "    except OSError as exc:\n"
        "        return 'DENIED:' + exc.__class__.__name__\n"
        f"print(probe(lambda: open({str(data / 'accounts.db')!r}).read()))\n"
        f"print(probe(lambda: open({str(data / 'accounts.db-wal')!r}).read()))\n"
        f"print(probe(lambda: open({str(data / 'auth_tokens' / 't.json')!r}).read()))\n"
        f"print(probe(lambda: open({str(data / 'todos' / 'a.json')!r}).read()))\n"
        f"print(probe(lambda: open({str(project / 'code.py')!r}).read()))\n"
        "print(probe(lambda: open('/proc/self/environ', 'rb').read(8)))\n"
        "print(probe(lambda: open('/proc/meminfo').readline()[:8]))\n"
    )
    # Narrower base roots than tool_sandbox_policy's "/" so the carve stays
    # small: pytest's tmp_path lives under a /tmp holding thousands of unrelated
    # entries. The deny set under test is the real one.
    policy = carved_policy(
        read_only=(
            *DEFAULT_SYSTEM_ROOTS,
            *_PY_ROOTS,
            *exec_policy.PROC_READABLE_FILES,
        ),
        read_write=(str(tmp_path), *DEFAULT_DEVICE_NODES),
        denied=exec_policy.denied_paths(),
    )
    proc = subprocess.run(
        wrap_argv([sys.executable, "-c", script]),
        env={**os.environ, **sandbox_env_overlay(policy)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    vault, wal, token, todo, code, environ, meminfo = proc.stdout.split("\n")[:7]
    assert vault.startswith("DENIED"), proc.stdout
    assert wal.startswith("DENIED"), proc.stdout
    assert token.startswith("DENIED"), proc.stdout
    assert todo == "TODO", proc.stdout
    assert code == "PROJECT", proc.stdout
    assert environ.startswith("DENIED"), proc.stdout
    assert meminfo.startswith("Mem"), proc.stdout


@requires_landlock
@requires_sandbox_on
def test_enforcement_bash_execute_cannot_read_the_process_environment():
    """The end of the wiring: the shipped tool, no policy plumbed by hand.

    ``cat /proc/1/environ`` is the exact command C1-01 was filed on, and it is
    where the vault master key, the service token and every provider key live.
    """
    assert exec_policy.sandbox_enabled(), "sandbox should be on by default here"

    for path in ("/proc/1/environ", "/proc/self/environ"):
        result = bash_execute.func(f"cat {path}")
        assert "Permission denied" in result, result


@requires_landlock
@requires_sandbox_on
def test_enforcement_bash_execute_denies_the_stores_through_the_real_policy(
    monkeypatch,
):
    """The shipped policy, end to end: no hand-built roots anywhere.

    The other enforcement test narrows ``read_write`` to keep the carve small,
    which means the configuration that actually ships (start from ``/`` and
    subtract) is the one never exercised. This runs it, through
    ``bash_execute`` itself, with a data dir laid out the way Docker lays it
    out: a sibling of the project root rather than a child.

    ``/var/tmp`` rather than ``tmp_path``, deliberately: pytest's ``/tmp``
    holds thousands of unrelated entries, and the carve budget would (rightly)
    drop the denial there, so the test would pass while proving nothing.
    """
    base = Path(tempfile.mkdtemp(dir="/var/tmp", prefix="nymeria-sandbox-"))
    try:
        project = base / "app"
        data = base / "data"
        project.mkdir()
        _seed_data_dir(data)
        monkeypatch.setattr(
            exec_policy,
            "get_settings",
            lambda: SimpleNamespace(
                project_root=project, data_dir=data, exec_sandbox_enabled=True
            ),
        )

        vault = bash_execute.func(f"cat {data / 'accounts.db'}")
        wal = bash_execute.func(f"cat {data / 'accounts.db-wal'}")
        todo = bash_execute.func(f"cat {data / 'todos' / 'a.json'}")
        system = bash_execute.func("cat /etc/hostname")
        proc = bash_execute.func("cat /proc/self/environ")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    assert "Permission denied" in vault, vault
    assert "Permission denied" in wal, wal
    assert "Permission denied" in proc, proc
    # ...and the whole-filesystem carve leaves everything else alone.
    assert "TODO" in todo, todo
    assert "Permission denied" not in system, system


_ENVIRON_PROBE_BODY = (
    "    try:\n"
    "        with open('/proc/self/environ', 'rb') as fh:\n"
    "            return 'READ:' + repr(fh.read(32))\n"
    "    except OSError as exc:\n"
    "        return 'DENIED:' + type(exc).__name__\n"
)


@requires_landlock
@requires_sandbox_on
def test_enforcement_a_python_custom_tool_cannot_read_the_environment():
    """The second wired surface, driven through the shipped runner.

    A published Python tool is agent-authored code running out of process. The
    env scrub already withholds this process's secrets from it; without the
    sandbox it could read them back out of ``/proc/self/environ`` anyway, since
    the API process is its own parent's sibling in the same container.
    """
    from nymeria.core.python_custom_tools import run_python_tool_subprocess
    from nymeria.tools.definitions.custom_tool_schema import PythonToolConfig

    result = run_python_tool_subprocess(
        tool_id="sandbox-probe",
        config=PythonToolConfig(source_code="def run():\n" + _ENVIRON_PROBE_BODY),
        params={},
        timeout_seconds=60,
    )

    assert result.ok, f"{result.error_type}: {result.error_message} {result.stderr}"
    assert result.result.startswith("DENIED"), result.result


@requires_landlock
def test_enforcement_a_python_custom_tool_reads_it_with_the_sandbox_off(monkeypatch):
    """The falsification half: the denial above is the sandbox, not the runner."""
    from nymeria.core.python_custom_tools import run_python_tool_subprocess
    from nymeria.tools.definitions.custom_tool_schema import PythonToolConfig

    monkeypatch.setattr(exec_policy, "sandbox_enabled", lambda: False)
    result = run_python_tool_subprocess(
        tool_id="sandbox-probe",
        config=PythonToolConfig(source_code="def run():\n" + _ENVIRON_PROBE_BODY),
        params={},
        timeout_seconds=60,
    )

    assert result.ok, f"{result.error_type}: {result.error_message} {result.stderr}"
    assert result.result.startswith("READ"), result.result


@requires_landlock
@requires_sandbox_on
@pytest.mark.asyncio
async def test_enforcement_a_workflow_cannot_read_the_environment():
    """The third wired surface, and the one with the most to lose.

    The workflow child connects back to an RPC socket in an ephemeral run dir
    and is killed by process group, so this asserts the run still SUCCEEDS as
    well as that the read is refused: a sandbox that broke the socket or the
    supervision would pass a denial-only assertion.
    """
    from nymeria.core.workflows.budget import WorkflowBudget
    from nymeria.core.workflows.executor import execute_workflow

    result = await execute_workflow(
        source="def run():\n" + _ENVIRON_PROBE_BODY,
        entrypoint="run",
        params={},
        user_id="tester",
        thread_id="sandbox-probe-thread",
        budget=WorkflowBudget(wall_clock_seconds=60),
        persist_record=False,
    )

    envelope = result.envelope
    assert envelope.ok, envelope.to_dict()
    assert str(envelope.output).startswith("DENIED"), envelope.output


@requires_landlock
@requires_sandbox_on
@pytest.mark.asyncio
async def test_enforcement_a_workflow_cannot_read_the_credential_store():
    """The runner's narrowed creation roots, driven end to end.

    The workflow child works only in its ephemeral run dir, so ``executor.py``
    passes ``creation_roots=(rt_dir,)`` and keeps the store denials that the
    default roots would drop.

    Where this bites is the SOURCE-CHECKOUT layout (data dir under the project
    root, so the slim shape): there the override is the only thing denying the
    vault to workflow code, and deleting it from ``executor.py`` fails this
    test. On a layout with the data dir outside the tree the stores are denied
    either way, so this still passes but proves less. Worth having in both.
    """
    from nymeria.config import get_settings
    from nymeria.core.workflows.budget import WorkflowBudget
    from nymeria.core.workflows.executor import execute_workflow

    vault = Path(get_settings().data_dir) / "accounts.db"
    if not vault.exists():
        pytest.skip(f"no account vault at {vault} to probe on this deployment")

    source = (
        "def run():\n"
        "    try:\n"
        f"        open({str(vault)!r}, 'rb').read(16)\n"
        "        return 'READ'\n"
        "    except OSError as exc:\n"
        "        return 'DENIED:' + type(exc).__name__\n"
    )
    result = await execute_workflow(
        source=source,
        entrypoint="run",
        params={},
        user_id="tester",
        thread_id="sandbox-store-probe-thread",
        budget=WorkflowBudget(wall_clock_seconds=60),
        persist_record=False,
    )

    envelope = result.envelope
    assert envelope.ok, envelope.to_dict()
    assert str(envelope.output).startswith("DENIED"), envelope.output


@requires_landlock
def test_enforcement_bash_execute_reads_the_environment_with_the_sandbox_off(
    monkeypatch,
):
    """The control's own falsification test.

    Asserting a denial proves nothing unless the same read succeeds when the
    sandbox is removed: without this, a command that failed for any other
    reason would read as containment.
    """
    monkeypatch.setattr(exec_policy, "sandbox_enabled", lambda: False)
    result = bash_execute.func("head -c 16 /proc/self/environ | tr '\\0' '\\n'")
    assert "Permission denied" not in result, result


def test_a_store_too_expensive_to_carve_is_left_reachable(tmp_path, monkeypatch):
    """The budget guard, and why it drops a denial instead of failing the spawn.

    Carving a path out means replacing every ancestor directory with one rule
    per entry, and the policy travels to the shim as a single environment
    string that execve will not carry past 128 KiB. A store parked under a
    directory with thousands of unrelated entries would therefore fail every
    command rather than protect anything.
    """
    project = tmp_path / "app"
    data = tmp_path / "crowd" / "data"
    project.mkdir()
    _seed_data_dir(data)
    for index in range(30):
        (data.parent / f"noise-{index}").write_text("x", encoding="utf-8")
    _settings(monkeypatch, project_root=project, data_dir=data)
    monkeypatch.setattr(exec_policy, "_MAX_CARVE_ENTRIES", 5)

    assert exec_policy.denied_paths() == ("/proc",)
