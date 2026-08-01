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
    DEFAULT_DEVICE_NODES,
    DEFAULT_SYSTEM_ROOTS,
    SandboxPolicy,
    carved_policy,
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
    # Writable, not read-only: the common uses are writes (`> /dev/null`,
    # `2> /dev/null`). A default rather than something each caller remembers,
    # because omitting them fails ordinary commands for reasons unrelated to
    # the sandbox (measured: `git` cannot start at all).
    assert SandboxPolicy().read_write == DEFAULT_DEVICE_NODES
    assert "/dev/null" in DEFAULT_DEVICE_NODES
    # Granted one node at a time, so a future user-writable node under /dev is
    # not reachable by default.
    assert "/dev" not in DEFAULT_DEVICE_NODES


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
    # -I -S is a control, not a style choice: without them the shim runs
    # site/usercustomize/.pth from a user-writable directory BEFORE it applies
    # the policy. See test_the_shim_does_not_run_user_site_startup_code.
    assert argv[1:3] == ["-I", "-S"]
    assert argv[3].endswith("exec_sandbox.py")
    assert argv[4] == "--"
    assert argv[5:] == ["cat", "/etc/hostname"]


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
def test_enforcement_grants_a_single_file_root_without_opening_its_directory(
    tmp_path,
):
    """A writable root may be a FILE, which is how device nodes are granted.

    Landlock rejects landlock_add_rule with EINVAL when a directory-only right
    (MAKE_*, REMOVE_*, READ_DIR, REFER) is asked for on a file, and the failure
    surfaces as an opaque errno that fails the whole launch closed. So a file
    root needs its own narrower mask. Without it, DEFAULT_DEVICE_NODES would
    make every sandboxed launch in the tree fail to start.

    The second half is the point of granting nodes individually: allowing the
    file must not make its DIRECTORY readable.
    """
    allowed = tmp_path / "allowed.txt"
    allowed.write_text("visible", encoding="utf-8")
    sibling = tmp_path / "sibling.txt"
    sibling.write_text("hidden", encoding="utf-8")

    script = (
        "import sys\n"
        "def probe(fn):\n"
        "    try:\n"
        "        return fn()\n"
        "    except OSError as exc:\n"
        "        return 'DENIED:' + exc.__class__.__name__\n"
        f"print(probe(lambda: open({str(allowed)!r}).read()))\n"
        f"print(probe(lambda: open({str(allowed)!r}, 'a').write('!') and 'W'))\n"
        f"print(probe(lambda: open({str(sibling)!r}).read()))\n"
        f"import os; print(probe(lambda: str(sorted(os.listdir({str(tmp_path)!r})))))\n"
    )
    policy = SandboxPolicy(
        read_only=(*DEFAULT_SYSTEM_ROOTS, *_PY_ROOTS),
        read_write=(str(allowed),),
    )
    proc = subprocess.run(
        wrap_argv([sys.executable, "-c", script]),
        env={**os.environ, **sandbox_env_overlay(policy)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    read, wrote, sibling_read, listing = proc.stdout.split("\n")[:4]
    assert read == "visible", proc.stdout
    assert not wrote.startswith("DENIED"), proc.stdout
    assert sibling_read.startswith("DENIED"), proc.stdout
    assert listing.startswith("DENIED"), proc.stdout


@requires_landlock
def test_enforcement_file_root_mask_does_not_leak_to_later_roots(tmp_path):
    """The narrowed mask is per root, not sticky.

    A file root and a directory root in the same policy, file FIRST, which is
    the real ordering: DEFAULT_DEVICE_NODES are files and sit ahead of every
    caller-added workspace. Assigning the narrowed mask to the loop variable
    instead of a per-iteration name silently strips create/delete from every
    directory root after the first file one, and a plain write still succeeds,
    so only a create/remove probe catches it.
    """
    node = tmp_path / "node.txt"
    node.write_text("n", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()

    script = (
        "import os, sys\n"
        "def probe(fn):\n"
        "    try:\n"
        "        return fn() or 'OK'\n"
        "    except OSError as exc:\n"
        "        return 'DENIED:' + exc.__class__.__name__\n"
        f"print(probe(lambda: open({str(workspace / 'new.txt')!r}, 'w').write('x') and None))\n"
        f"print(probe(lambda: os.mkdir({str(workspace / 'sub')!r})))\n"
        f"print(probe(lambda: os.remove({str(workspace / 'new.txt')!r})))\n"
    )
    policy = SandboxPolicy(
        read_only=(*DEFAULT_SYSTEM_ROOTS, *_PY_ROOTS),
        read_write=(str(node), str(workspace)),
    )
    proc = subprocess.run(
        wrap_argv([sys.executable, "-c", script]),
        env={**os.environ, **sandbox_env_overlay(policy)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    for line in proc.stdout.split("\n")[:3]:
        assert line == "OK", f"directory root lost a right after a file root: {proc.stdout}"


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


# --- carve tier (allow-everything-except) -------------------------------------


def _tree(root):
    """A small fixture tree: two files and two subdirs, one of each denied."""
    (root / "keep.txt").write_text("KEEP", encoding="utf-8")
    (root / "secret.txt").write_text("SECRET", encoding="utf-8")
    (root / "keepdir").mkdir()
    (root / "keepdir" / "inner.txt").write_text("INNER", encoding="utf-8")
    (root / "secretdir").mkdir()
    (root / "secretdir" / "key.txt").write_text("KEY", encoding="utf-8")


def test_carve_leaves_a_root_whole_when_nothing_under_it_is_denied(tmp_path):
    _tree(tmp_path)
    policy = carved_policy(read_write=(str(tmp_path),), denied=("/nowhere",))
    assert policy.read_write == (str(tmp_path.resolve()),)
    assert policy.containers == ()


def test_carve_replaces_an_ancestor_with_its_other_entries(tmp_path):
    _tree(tmp_path)
    policy = carved_policy(
        read_write=(str(tmp_path),),
        denied=(str(tmp_path / "secret.txt"), str(tmp_path / "secretdir")),
    )
    assert set(policy.read_write) == {
        str((tmp_path / name).resolve()) for name in ("keep.txt", "keepdir")
    }
    # The ancestor becomes a container rather than disappearing, or the child
    # could not even list the directory it is standing in.
    assert policy.containers == (str(tmp_path.resolve()),)


def test_carve_drops_a_root_that_is_itself_denied(tmp_path):
    _tree(tmp_path)
    policy = carved_policy(read_write=(str(tmp_path),), denied=(str(tmp_path),))
    assert policy.read_write == ()
    assert policy.containers == ()


def test_carve_denies_a_path_that_does_not_exist_yet(tmp_path):
    """A store that has not been created must not be reachable by creating it."""
    _tree(tmp_path)
    policy = carved_policy(
        read_write=(str(tmp_path),), denied=(str(tmp_path / "not-yet.db"),)
    )
    assert str((tmp_path / "not-yet.db").resolve()) not in policy.read_write
    assert str((tmp_path / "keep.txt").resolve()) in policy.read_write
    assert policy.containers == (str(tmp_path.resolve()),)


def test_carve_skips_symlinked_entries(tmp_path):
    _tree(tmp_path)
    (tmp_path / "link").symlink_to(tmp_path / "keepdir")
    policy = carved_policy(
        read_write=(str(tmp_path),), denied=(str(tmp_path / "secret.txt"),)
    )
    # Resolving it instead of skipping would widen the carve to wherever it
    # points; Landlock matches the resolved inode, so it grants nothing anyway.
    assert str(tmp_path / "link") not in policy.read_write
    assert str((tmp_path / "keepdir").resolve()) in policy.read_write


def test_carve_drops_a_directory_it_cannot_list(tmp_path):
    """Cannot enumerate means cannot carve, so the whole subtree is denied."""
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "child").mkdir()
    (parent / "child" / "secret.txt").write_text("S", encoding="utf-8")
    parent.chmod(0o000)
    try:
        policy = carved_policy(
            read_write=(str(parent),), denied=(str(parent / "child" / "secret.txt"),)
        )
    finally:
        parent.chmod(0o755)
    assert policy.read_write == ()
    assert policy.containers == ()


def test_carve_still_grants_an_explicit_root_under_a_denied_path(tmp_path):
    """Denial is the absence of a rule, so an explicit deeper allow still wins.

    Deliberate: it is how a couple of readable ``/proc`` files are granted back
    while the rest of ``/proc`` stays denied. It also means ``denied`` is a
    carve instruction for the roots it is applied to, not a global assertion.
    """
    _tree(tmp_path)
    policy = carved_policy(
        read_only=(str(tmp_path / "secretdir" / "key.txt"),),
        read_write=(str(tmp_path),),
        denied=(str(tmp_path / "secretdir"),),
    )
    assert policy.read_only == (str((tmp_path / "secretdir" / "key.txt").resolve()),)


@requires_landlock
def test_enforcement_carved_directory_denies_content_and_stays_usable(tmp_path):
    """The whole carve contract, in one sandboxed child.

    A denied file cannot be read, its neighbours can, and the directory holding
    it is still listable and writable. The last two are what make the carve
    shippable: a container that could not create or list would break every
    ordinary command that runs in one.
    """
    _tree(tmp_path)
    denied = tmp_path / "secret.txt"
    script = (
        "import os, sys\n"
        "def probe(fn):\n"
        "    try:\n"
        "        return str(fn())\n"
        "    except OSError as exc:\n"
        "        return 'DENIED:' + exc.__class__.__name__\n"
        f"os.chdir({str(tmp_path)!r})\n"
        "print(probe(lambda: open('secret.txt').read()))\n"
        "print(probe(lambda: open('keep.txt').read()))\n"
        "print(probe(lambda: open('keepdir/inner.txt').read()))\n"
        "print(probe(lambda: sorted(os.listdir('.'))[0]))\n"
        "print(probe(lambda: open('made.txt', 'w').write('m')))\n"
        "print(probe(lambda: os.rename('secret.txt', 'keepdir/leaked.txt')))\n"
        "print(probe(lambda: os.remove('made.txt')))\n"
    )
    policy = carved_policy(
        read_only=(*DEFAULT_SYSTEM_ROOTS, *_PY_ROOTS),
        read_write=(str(tmp_path), *DEFAULT_DEVICE_NODES),
        denied=(str(denied),),
    )
    proc = subprocess.run(
        wrap_argv([sys.executable, "-c", script]),
        env={**os.environ, **sandbox_env_overlay(policy)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.split("\n")
    assert lines[0].startswith("DENIED"), proc.stdout        # the denied file
    assert lines[1] == "KEEP", proc.stdout                   # its neighbour
    assert lines[2] == "INNER", proc.stdout                  # a subdirectory
    assert lines[3] == "keep.txt", proc.stdout               # listing works
    assert lines[4] == "1", proc.stdout                      # creating works
    # Moving the denied file somewhere readable must not launder it. The kernel
    # refuses a refer whose destination grants more than the source, so this is
    # denied even though the container itself holds REFER.
    assert lines[5].startswith("DENIED"), proc.stdout
    assert lines[6] == "None", proc.stdout                   # removing works
    assert denied.read_text(encoding="utf-8") == "SECRET"


@requires_landlock
def test_the_shim_does_not_run_user_site_startup_code(tmp_path):
    """The shim is an interpreter, and an interpreter runs code on startup.

    ``site`` imports ``usercustomize`` from ``$HOME/.local/lib/pythonX.Y/
    site-packages`` and executes every ``.pth`` it finds, all BEFORE this
    module's ``main`` reaches ``apply_landlock_policy``. That directory belongs
    to the uid the agent's commands run as, so without ``-I -S`` on the shim
    launch a command could plant ``usercustomize.py`` on one call and have it
    run unsandboxed on the next: a complete bypass, two tool calls wide.

    The marker is written to a directory the policy grants, so a failure here
    means the code ran, not that it was denied.
    """
    home = tmp_path / "home"
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_dir = home / ".local" / "lib" / version / "site-packages"
    site_dir.mkdir(parents=True)
    marker = tmp_path / "startup-code-ran.txt"
    (site_dir / "usercustomize.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8"
    )

    policy = carved_policy(
        read_only=(*DEFAULT_SYSTEM_ROOTS, *_PY_ROOTS),
        read_write=(str(tmp_path), *DEFAULT_DEVICE_NODES),
    )
    proc = subprocess.run(
        wrap_argv(["/bin/sh", "-c", "echo ok"]),
        env={
            "HOME": str(home),
            "PATH": "/usr/bin:/bin",
            **sandbox_env_overlay(policy),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok", proc.stdout
    assert not marker.exists(), (
        "usercustomize.py ran inside the shim, which happens before the "
        "Landlock policy is applied: the sandbox can be bypassed by writing "
        "to the user site directory."
    )


def test_carve_skips_another_name_for_a_denied_file(tmp_path):
    """A denial is by inode, so a hardlink to it is not a way around it.

    Landlock keys its rules on the inode, and the carve enumerates by name, so
    without this check one ``ln secret alias`` would earn the secret's own
    inode a READ_FILE rule on the next launch and re-open it under both names.
    """
    (tmp_path / "secret.txt").write_text("SECRET", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("KEEP", encoding="utf-8")
    os.link(tmp_path / "secret.txt", tmp_path / "alias.txt")

    policy = carved_policy(
        read_write=(str(tmp_path),), denied=(str(tmp_path / "secret.txt"),)
    )

    assert str(tmp_path / "keep.txt") in policy.read_write
    assert str(tmp_path / "alias.txt") not in policy.read_write
    assert str(tmp_path / "secret.txt") not in policy.read_write


@requires_landlock
def test_enforcement_a_hardlink_cannot_launder_a_denied_file(tmp_path):
    """The two-call escape, driven end to end across two policies.

    A container has to keep ``MAKE_REG`` (a shell that cannot create a file is
    useless), and a link within one directory needs exactly that, not
    ``REFER``: the kernel's "destination may not grant more than the source"
    guard is about reparenting and does not fire here. So the link CAN be made.
    What must not happen is the alias becoming readable, in this launch (it has
    no rule, because the carve is a snapshot) or in the next one, where the
    carve would otherwise hand a READ_FILE rule to the secret's own inode and
    re-open it under both names, permanently.
    """
    _tree(tmp_path)
    denied = tmp_path / "secret.txt"
    os.link(denied, tmp_path / "preexisting-alias.txt")

    def _run(script: str) -> list[str]:
        policy = carved_policy(  # rebuilt per launch, exactly as the tool does
            read_only=(*DEFAULT_SYSTEM_ROOTS, *_PY_ROOTS),
            read_write=(str(tmp_path), *DEFAULT_DEVICE_NODES),
            denied=(str(denied),),
        )
        proc = subprocess.run(
            wrap_argv([sys.executable, "-c", script]),
            env={**os.environ, **sandbox_env_overlay(policy)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.split("\n")

    probe = (
        "import os\n"
        "def probe(fn):\n"
        "    try:\n"
        "        return str(fn())\n"
        "    except OSError as exc:\n"
        "        return 'DENIED:' + exc.__class__.__name__\n"
        f"os.chdir({str(tmp_path)!r})\n"
    )

    first = _run(
        probe
        + "print(probe(lambda: open('preexisting-alias.txt').read()))\n"
        + "print(probe(lambda: os.link('secret.txt', 'fresh-alias.txt')))\n"
        + "print(probe(lambda: open('fresh-alias.txt').read()))\n"
    )
    assert first[0].startswith("DENIED"), first  # carve skipped the inode
    assert first[1] == "None", first             # the link itself is allowed
    assert first[2].startswith("DENIED"), first  # created after the snapshot

    second = _run(
        probe
        + "print(probe(lambda: open('fresh-alias.txt').read()))\n"
        + "print(probe(lambda: open('secret.txt').read()))\n"
        + "print(probe(lambda: open('keep.txt').read()))\n"
    )
    assert second[0].startswith("DENIED"), second  # the escape, on the next call
    assert second[1].startswith("DENIED"), second  # and the original still shut
    assert second[2] == "KEEP", second             # everything else unaffected


@requires_landlock
def test_shim_leaves_sigpipe_at_its_default(tmp_path):
    """The shim must be invisible to the command it launches.

    ``subprocess`` restores SIGPIPE to SIG_DFL in the forked child, and then
    the shim starts a Python interpreter, which sets it back to SIG_IGN.
    SIG_IGN survives exec (a handler would not), so without the restore every
    ``producer | head`` in a sandboxed shell ends with "standard output:
    Broken pipe" on stderr instead of ending quietly.
    """
    policy = SandboxPolicy(read_only=(*DEFAULT_SYSTEM_ROOTS, *_PY_ROOTS))
    proc = subprocess.run(
        wrap_argv(["/bin/sh", "-c", "yes | head -c 1000 > /dev/null"]),
        env={**os.environ, **sandbox_env_overlay(policy)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == "", proc.stderr
