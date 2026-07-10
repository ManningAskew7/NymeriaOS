"""Tests for the Landlock exec sandbox (Track B, stage 1).

Two tiers:

* Pure-unit tests (policy serialization, argv wrapping, the fail-closed contract)
  run everywhere.
* Enforcement tests spawn a real child through the shim and assert the boundary
  actually holds; they skip with a loud reason on a kernel without Landlock, so
  CI on a supported kernel proves containment and CI elsewhere does not pretend
  to. Nothing here claims "contained" without a passing enforcement spawn.
"""

import json
import os
import subprocess
import sys

import pytest

from nymeria.exec_sandbox import (
    DEFAULT_SYSTEM_ROOTS,
    SandboxPolicy,
    landlock_abi_version,
    sandbox_available,
    sandbox_env_overlay,
    wrap_argv,
)

_SANDBOX_POLICY_ENV = "NYMERIA_SANDBOX_POLICY"

# Child program: try to read/write each path, report outcomes as JSON.
# argv: [ws_file, secret_file]
_CHILD_SRC = r"""
import json, sys
ws_file, secret = sys.argv[1], sys.argv[2]
res = {}
for label, path in (("workspace", ws_file), ("secret", secret), ("proc", "/proc/self/environ")):
    try:
        with open(path, "rb") as f:
            f.read(16)
        res[label] = "OK"
    except OSError as e:
        res[label] = "DENIED:%d" % (e.errno,)
try:
    with open(ws_file + ".w", "wb") as f:
        f.write(b"x")
    res["write"] = "OK"
except OSError as e:
    res["write"] = "DENIED:%d" % (e.errno,)
print(json.dumps(res))
"""

requires_landlock = pytest.mark.skipif(
    not sandbox_available(),
    reason=f"Landlock unavailable (ABI {landlock_abi_version()}); enforcement not testable here",
)

# The sandboxed interpreter must be able to load itself and its stdlib. Mirrors
# what the real wiring adds to the policy; keeps these tests robust when pytest
# runs under a venv/non-/usr Python.
_PY_ROOTS = tuple({
    os.path.dirname(os.path.realpath(sys.executable)),
    os.path.realpath(sys.prefix),
    os.path.realpath(sys.base_prefix),
})


# --- pure-unit tier -----------------------------------------------------------

def test_abi_and_availability_types():
    assert isinstance(landlock_abi_version(), int)
    assert isinstance(sandbox_available(), bool)


def test_policy_env_roundtrip():
    policy = SandboxPolicy(read_only=("/usr", "/etc"), read_write=("/tmp/ws",))
    restored = SandboxPolicy.from_env_value(policy.to_env_value())
    assert restored.read_only == ("/usr", "/etc")
    assert restored.read_write == ("/tmp/ws",)


def test_policy_defaults_seed_system_roots():
    assert SandboxPolicy().read_only == DEFAULT_SYSTEM_ROOTS
    assert SandboxPolicy().read_write == ()


def test_policy_rejects_non_dict_json():
    for bad in ("[1, 2]", "42", "true", '"str"'):
        with pytest.raises(ValueError):
            SandboxPolicy.from_env_value(bad)


def test_with_roots_appends_and_dedupes():
    policy = SandboxPolicy(read_only=("/usr",), read_write=("/tmp/a",)).with_roots(
        read_only=("/usr", "/lib"), read_write=("/tmp/a", "/tmp/b")
    )
    assert policy.read_only == ("/usr", "/lib")
    assert policy.read_write == ("/tmp/a", "/tmp/b")


def test_wrap_argv_shape():
    argv = wrap_argv(["cat", "/etc/hostname"], python_executable="/usr/bin/python3")
    assert argv[0] == "/usr/bin/python3"
    assert argv[1].endswith("exec_sandbox.py")
    assert argv[2] == "--"
    assert argv[3:] == ["cat", "/etc/hostname"]


def test_sandbox_env_overlay_carries_policy():
    overlay = sandbox_env_overlay(SandboxPolicy(read_write=("/tmp/ws",)))
    assert _SANDBOX_POLICY_ENV in overlay
    assert "/tmp/ws" in overlay[_SANDBOX_POLICY_ENV]


def test_shim_fails_closed_without_policy():
    """The shim must refuse (nonzero, no exec) when no policy is set, so a
    sandboxed launch can never silently degrade to an unsandboxed run."""
    env = {k: v for k, v in os.environ.items() if k != _SANDBOX_POLICY_ENV}
    proc = subprocess.run(
        wrap_argv(["echo", "SHOULD-NOT-RUN"]),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "SHOULD-NOT-RUN" not in proc.stdout


def test_shim_fails_closed_on_bad_policy():
    env = dict(os.environ)
    env[_SANDBOX_POLICY_ENV] = "{not valid json"
    proc = subprocess.run(
        wrap_argv(["echo", "SHOULD-NOT-RUN"]),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "SHOULD-NOT-RUN" not in proc.stdout


# --- enforcement tier ---------------------------------------------------------

def _run_child(policy: SandboxPolicy, ws_file: str, secret: str):
    env = dict(os.environ)
    env.update(sandbox_env_overlay(policy))
    argv = wrap_argv([sys.executable, "-c", _CHILD_SRC, ws_file, secret])
    proc = subprocess.run(argv, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, f"child failed: {proc.stderr}"
    return json.loads(proc.stdout.strip())


@requires_landlock
def test_enforcement_denies_outside_allowlist(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ws_file = ws / "allowed.txt"
    ws_file.write_text("ALLOWED")
    # Secret lives in the tmp_path PARENT, not under the allowlisted workspace.
    secret = tmp_path / "secret.txt"
    secret.write_text("MASTER-KEY")

    policy = SandboxPolicy().with_roots(read_only=_PY_ROOTS, read_write=(str(ws),))
    res = _run_child(policy, str(ws_file), str(secret))

    assert res["workspace"] == "OK"
    assert res["write"] == "OK"
    assert res["secret"].startswith("DENIED"), res["secret"]
    assert res["proc"].startswith("DENIED"), res["proc"]


@requires_landlock
def test_enforcement_allows_system_exec(tmp_path):
    """A sandboxed interpreter still loads its libs from the system roots."""
    ws = tmp_path / "ws"
    ws.mkdir()
    policy = SandboxPolicy().with_roots(read_only=_PY_ROOTS, read_write=(str(ws),))
    env = dict(os.environ)
    env.update(sandbox_env_overlay(policy))
    proc = subprocess.run(
        wrap_argv([sys.executable, "-c", "print(6 * 7)"]),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "42"


@requires_landlock
def test_enforcement_rejects_symlinked_writable_root(tmp_path):
    """A writable root that is a symlink is rejected (fail closed), so it cannot
    silently expand the boundary to the symlink target."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "ws_link"
    link.symlink_to("/")  # a workspace symlinked to the whole filesystem
    policy = SandboxPolicy().with_roots(read_only=_PY_ROOTS, read_write=(str(link),))
    env = dict(os.environ)
    env.update(sandbox_env_overlay(policy))
    proc = subprocess.run(
        wrap_argv(["echo", "SHOULD-NOT-RUN"]),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "SHOULD-NOT-RUN" not in proc.stdout


@requires_landlock
def test_enforcement_cross_subdir_rename_allowed(tmp_path):
    """os.replace across subdirectories of a writable root works (FS_REFER),
    so atomic writes and file moves inside the workspace are not broken."""
    ws = tmp_path / "ws"
    (ws / "a").mkdir(parents=True)
    (ws / "b").mkdir(parents=True)
    src = r"""
import os, sys
ws = sys.argv[1]
open(os.path.join(ws, "a", "f.txt"), "w").write("x")
os.replace(os.path.join(ws, "a", "f.txt"), os.path.join(ws, "b", "f.txt"))
print("RENAMED" if os.path.exists(os.path.join(ws, "b", "f.txt")) else "MISSING")
"""
    policy = SandboxPolicy().with_roots(read_only=_PY_ROOTS, read_write=(str(ws),))
    env = dict(os.environ)
    env.update(sandbox_env_overlay(policy))
    proc = subprocess.run(
        wrap_argv([sys.executable, "-c", src, str(ws)]),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "RENAMED"
