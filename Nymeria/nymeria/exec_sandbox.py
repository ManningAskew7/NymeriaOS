"""Landlock filesystem self-sandbox for spawned exec surfaces (Track B, stage 1).

The credential vault protects the LANGUAGE channel; the env scrub
(``subprocess_env.py``) closes the environment-INHERITANCE leak. Neither stops a
spawned child from reading ``/proc/1/environ`` (the master key) or another
tenant's data straight off disk. This module is the EXECUTION-channel boundary:
an unprivileged, in-process Landlock LSM ruleset that confines a child to an
explicit allowlist of paths and denies everything else (the credential/token
stores, other tenants' subtrees, and ``/proc``).

Landlock only ever *removes* the child's own reach, so it grants no capability
and adds no kernel attack surface. Verified live under the production posture
(Docker, uid 999, ``cap_drop: ALL``, ``no-new-privileges``, default seccomp):
ABI 4 enforces filesystem denial with no container or kernel-surface change.

Dual purpose, one file:

* **Library** (imported as ``nymeria.exec_sandbox``): ``sandbox_available()`` for
  startup fail-closed detection, the ``SandboxPolicy`` dataclass,
  ``carved_policy`` for the allow-everything-except shape Landlock cannot
  express directly, and ``wrap_argv``/``sandbox_env_overlay`` for a parent to
  build a sandboxed launch. What each Nymeria exec surface actually passes is
  ``core/exec_policy.py``'s business, not this module's.
* **Shim** (launched by file path, ``python3 exec_sandbox.py -- <cmd> ...``):
  runs in a FRESH, single-threaded interpreter, reads the policy from the
  ``NYMERIA_SANDBOX_POLICY`` env var, applies Landlock, then ``execvp``s the real
  command. Running the syscalls in the fresh shim (not a ``preexec_fn`` after a
  fork of the heavily-threaded API process) avoids the fork+threads deadlock
  hazard. The shim is stdlib-only and imports nothing from ``nymeria`` so a
  file-path launch never drags in the package.

Fail-closed: a shim invoked WITH a policy that cannot be enforced exits nonzero
and never ``exec``s the target. A sandboxed launch never degrades to an
unsandboxed run.

Linux-only. On any non-Linux/older kernel ``sandbox_available()`` is False and
multi-tenant mode must refuse to start (handled by the caller, not here).

Caller contract (Landlock is a filesystem-path boundary, not a total jail):

* **Inherited fds bypass it.** Landlock only mediates ``open`` after
  ``restrict_self``; a descriptor already open in the child is fully usable.
  Launch the shim with ``close_fds=True`` (the ``subprocess`` default) and never
  ``pass_fds`` a sensitive descriptor into a sandboxed child.
* **The interpreter and its libraries must be reachable.** The real command's
  binary/interpreter and shared libraries (e.g. a venv Python and its
  ``site-packages`` under ``/opt`` or a virtualenv) must be inside the policy's
  ``read_only`` roots, or the sandboxed command fails to exec (fail-closed).
* **Writable roots must be trusted directories, not attacker-controlled
  symlinks.** This module rejects a writable root that is itself a symlink and
  canonicalizes every root, but a caller that places a tenant workspace under a
  parent directory an untrusted party can turn into a symlink defeats the
  boundary. The layer that provisions per-tenant workspaces owns that.
* **Egress is out of scope here.** ``handled_access_net`` is 0; network
  confinement is a separate stage.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import signal
import stat
import struct
import sys
from dataclasses import dataclass, field

# --- Landlock syscall numbers (shared generic table: x86_64 + aarch64) --------
# On an arch/kernel without these, the create_ruleset probe returns -1/ENOSYS and
# sandbox_available() reports False, so a wrong number fails closed, never open.
_NR_landlock_create_ruleset = 444
_NR_landlock_add_rule = 445
_NR_landlock_restrict_self = 446

_PR_SET_NO_NEW_PRIVS = 38

_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1

# Filesystem access-right bits (uapi/linux/landlock.h).
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13  # ABI >= 2
_FS_TRUNCATE = 1 << 14  # ABI >= 3
_FS_IOCTL_DEV = 1 << 15  # ABI >= 5

# Access granted on a read-only allow root (read + traverse + exec-load libs).
_ACCESS_RO = _FS_READ_FILE | _FS_READ_DIR | _FS_EXECUTE
# Access granted on a writable allow root (RO plus create/modify/remove).
# _FS_REFER lets rename/link move entries between subdirectories of a writable
# root (needed by atomic os.replace across subdirs, ``mv``, git, build tools).
# REFER only ever permits a refer whose BOTH ends are inside the ruleset's
# allowed roots, so it does not widen the boundary. Masked to the kernel ABI at
# apply time, so it is simply dropped on ABI < 2.
_ACCESS_RW = (
    _ACCESS_RO
    | _FS_WRITE_FILE
    | _FS_MAKE_REG
    | _FS_MAKE_DIR
    | _FS_MAKE_SYM
    | _FS_MAKE_FIFO
    | _FS_MAKE_SOCK
    | _FS_REMOVE_FILE
    | _FS_REMOVE_DIR
    | _FS_TRUNCATE
    | _FS_REFER
)

# The subset of each mask that is meaningful on a non-directory. Landlock
# rejects landlock_add_rule with EINVAL when a directory-only right (MAKE_*,
# REMOVE_*, READ_DIR, REFER) is requested on a file, and the failure surfaces as
# an opaque "errno 22" that fails the whole launch closed. So an allow root that
# happens to be a file needs its own mask; see _add_rules. This is what makes it
# possible to allow individual device nodes rather than all of /dev, and single
# ``/proc`` files while the rest of ``/proc`` stays denied.
_ACCESS_RW_FILE = _FS_READ_FILE | _FS_WRITE_FILE | _FS_EXECUTE | _FS_TRUNCATE
_ACCESS_RO_FILE = _FS_READ_FILE | _FS_EXECUTE

# Access granted on a CONTAINER: a directory that exists only to hold allow
# roots further down (see carved_policy). It gets every right except the two
# that would read file CONTENT out of it, so listing it and creating, writing,
# removing and renaming entries in it all keep working. Content reads come from
# the per-entry rules instead, which is what lets a denied entry sit inside a
# reachable directory.
#
# _FS_READ_FILE is the exclusion the control is made of: a container that
# granted it would re-open every denied file beneath it, because Landlock
# unions rights UP the tree (an ancestor's rule grants, and a deeper rule can
# never take away; measured on ABI 4, not assumed). _FS_EXECUTE goes with it
# because an executable that IS reachable has its own rule, so granting exec on
# the whole container only ever covers the denied entries.
#
# Everything else stays, and each was measured rather than reasoned about:
# without _FS_WRITE_FILE a shell cannot create a file at all (``echo x > new``
# fails, and so does every temp-file-then-rename tool); without _FS_TRUNCATE it
# cannot overwrite one it created during the same command; without _FS_REFER it
# cannot move a file into or out of the directory. None of the three weakens the
# denial. _FS_REFER in particular cannot be used to move a denied file
# somewhere readable: the kernel additionally requires the destination's rights
# to be a SUBSET of the source's, so a refer out of a container into a
# fully-granted directory is EXDEV (verified with a direct rename(2), because
# ``mv`` masks it by falling back to copy, which the read denial then stops).
#
# What that guard does NOT cover is a link or rename WITHIN one directory,
# which Landlock governs with _FS_MAKE_REG rather than _FS_REFER. So a
# sandboxed command can run ``ln <container>/secret <container>/alias``, and
# withholding REFER would not stop it (measured: the link still succeeds) while
# costing every ``mv`` into the data dir. The alias is unreadable in the
# creating launch (it has no rule; the carve is a snapshot) and unreadable in
# every later one because ``carved_policy`` skips an entry whose inode matches
# a denied path. That inode check, not this mask, is what closes it.
_ACCESS_CONTAINER = _ACCESS_RW & ~(_FS_READ_FILE | _FS_EXECUTE)

_SANDBOX_POLICY_ENV = "NYMERIA_SANDBOX_POLICY"

# Default read-only system roots every child needs to load an interpreter and
# shared libraries. Callers add their own tenant/workspace roots on top.
DEFAULT_SYSTEM_ROOTS: tuple[str, ...] = (
    "/usr",
    "/lib",
    "/lib64",
    "/bin",
    "/sbin",
    "/etc",
)

# Device nodes every sandboxed child needs, granted individually rather than by
# allowing all of /dev. These are WRITABLE because the common uses are writes:
# `> /dev/null`, `2> /dev/null`. Measured cost of omitting them, which is why
# they are a default rather than something each caller remembers: `git` fails
# outright ("fatal: could not open '/dev/null'"), and every shell redirection to
# /dev/null becomes a hard error, so ordinary commands fail for a reason with no
# connection to what the sandbox is for.
#
# Allowing them grants nothing: Landlock only ever REMOVES reach, so a node the
# calling user could not open before (a raw block device, root-owned) stays shut
# by ordinary permissions. Listing them one by one rather than allowing /dev
# keeps a future user-writable node under it out of reach by default.
DEFAULT_DEVICE_NODES: tuple[str, ...] = (
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
)


class SandboxError(RuntimeError):
    """Landlock could not be applied; the caller must fail closed."""


def _libc() -> ctypes.CDLL:
    lib = ctypes.CDLL(None, use_errno=True)
    lib.syscall.restype = ctypes.c_long
    return lib


_abi_cache: int | None = None


def landlock_abi_version() -> int:
    """Return the kernel's Landlock ABI version, or <= 0 if unavailable.

    Cached per process. Safe to call in the already-running parent: it is a
    single read-only syscall that grants nothing.
    """
    global _abi_cache
    if _abi_cache is not None:
        return _abi_cache
    if not sys.platform.startswith("linux"):
        _abi_cache = -1
        return _abi_cache
    try:
        lib = _libc()
        res = lib.syscall(
            ctypes.c_long(_NR_landlock_create_ruleset),
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
        )
        _abi_cache = int(res)
    except (OSError, AttributeError, ValueError):
        _abi_cache = -1
    return _abi_cache


def sandbox_available() -> bool:
    """True when Landlock filesystem enforcement can be applied on this kernel."""
    return landlock_abi_version() >= 1


def _handled_fs_mask(abi: int) -> int:
    mask = (
        _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_READ_DIR
        | _FS_REMOVE_DIR | _FS_REMOVE_FILE | _FS_MAKE_CHAR | _FS_MAKE_DIR
        | _FS_MAKE_REG | _FS_MAKE_SOCK | _FS_MAKE_FIFO | _FS_MAKE_BLOCK
        | _FS_MAKE_SYM
    )
    if abi >= 2:
        mask |= _FS_REFER
    if abi >= 3:
        mask |= _FS_TRUNCATE
    if abi >= 5:
        mask |= _FS_IOCTL_DEV
    return mask


@dataclass(frozen=True)
class SandboxPolicy:
    """Deny-by-default allowlist for one sandboxed exec.

    Only ``read_only`` and ``read_write`` roots (and everything beneath them)
    are reachable; every other path, including ``/proc`` and the credential
    stores, is denied. ``read_only`` is seeded with the system roots needed to
    launch a binary, and ``read_write`` with the standard device nodes, because
    a child that cannot open ``/dev/null`` fails in ways unrelated to what the
    sandbox is for.

    A root may be a directory (the whole subtree is reachable) or a single
    file, which is how the device nodes are granted without opening all of
    ``/dev``.

    ``containers`` is the third kind, and it exists because Landlock has no
    deny-under-an-allow: see ``carved_policy``, which is the only thing that
    should be populating it.
    """

    read_only: tuple[str, ...] = field(default=DEFAULT_SYSTEM_ROOTS)
    read_write: tuple[str, ...] = field(default=DEFAULT_DEVICE_NODES)
    containers: tuple[str, ...] = field(default=())

    def to_env_value(self) -> str:
        return json.dumps({
            "ro": list(self.read_only),
            "rw": list(self.read_write),
            "cont": list(self.containers),
        })

    @classmethod
    def from_env_value(cls, raw: str) -> "SandboxPolicy":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("sandbox policy must be a JSON object")
        return cls(
            read_only=tuple(data.get("ro") or ()),
            read_write=tuple(data.get("rw") or ()),
            containers=tuple(data.get("cont") or ()),
        )

    def with_roots(
        self,
        *,
        read_only: tuple[str, ...] = (),
        read_write: tuple[str, ...] = (),
        containers: tuple[str, ...] = (),
    ) -> "SandboxPolicy":
        return SandboxPolicy(
            read_only=tuple(dict.fromkeys((*self.read_only, *read_only))),
            read_write=tuple(dict.fromkeys((*self.read_write, *read_write))),
            containers=tuple(dict.fromkeys((*self.containers, *containers))),
        )


# Ceiling on how many allow roots a carve may produce. Two reasons for a number
# this low rather than an arbitrary large one: the policy travels to the shim as
# a single environment string, and execve rejects one over MAX_ARG_STRLEN
# (128 KiB), so a carve of a few thousand paths would fail the spawn with an
# opaque E2BIG rather than with anything a reader could act on. Real policies
# produce a few dozen. Hitting this raises rather than truncating, because a
# truncated carve silently denies whatever fell off the end; callers that would
# rather drop a costly denial than fail should not offer it in the first place
# (``core/exec_policy.denied_paths`` does exactly that: it prices the whole
# store deny set with ``_carve_cost`` against its own ``_MAX_CARVE_ENTRIES``
# budget and drops the set rather than letting it reach this ceiling).
_MAX_CARVED_ROOTS = 1500


def carved_policy(
    *,
    read_only: tuple[str, ...] = (),
    read_write: tuple[str, ...] = (),
    denied: tuple[str, ...] = (),
) -> SandboxPolicy:
    """Build a policy that reaches ``read_only``/``read_write`` EXCEPT ``denied``.

    Landlock has no deny rule and no deny-under-an-allow: rights union UP the
    tree, so a rule on an ancestor grants and no deeper rule can take away
    (verified on ABI 4, not assumed). Excluding a path from inside an allowed
    root is therefore done by NOT naming it: each ancestor of a denied path is
    replaced by rules on its other entries, one per entry, and the ancestor
    itself becomes a ``container`` (see ``_ACCESS_CONTAINER``) so that listing
    it and creating/removing entries in it keep working.

    Consequences worth knowing before choosing a ``denied`` set:

    * The carve is a SNAPSHOT taken here, in the parent, at policy-build time.
      An entry created in a container after this returns is unreachable to the
      child. That is the fail-closed direction, and the window is one spawn
      because the policy is rebuilt per launch.
    * A directory that cannot be listed is dropped entirely rather than allowed
      whole, so an unreadable ancestor denies its subtree.
    * Symlinked entries are skipped, not resolved: Landlock matches the resolved
      inode, so a symlink grants nothing its target is not already granted, and
      resolving here would silently widen the carve to wherever it points.
    * ``denied`` need not exist. A missing path still carves its ancestors, so a
      store that has not been created yet cannot be reached by creating it.
    * A denial is by INODE, not by name, and the carve enforces that: an entry
      sharing ``(st_dev, st_ino)`` with a denied path is skipped even under a
      different name. Landlock itself keys on the inode, so without this a
      single ``ln secret alias`` inside a container would hand the next launch
      a carve rule on the secret's own inode, re-opening it under BOTH names
      permanently. (``_ACCESS_CONTAINER`` withholds ``REFER`` so the hardlink
      cannot be made in the first place; this is the second half, and it also
      covers a link that predates the sandbox.)
    """
    denied_real = {os.path.realpath(path) for path in denied}
    denied_inodes = set()
    for path in denied_real:
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            continue  # missing or unreadable: the name carve above still holds
        denied_inodes.add((info.st_dev, info.st_ino))
    containers: list[str] = []
    produced = 0

    def _is_denied_inode(real: str) -> bool:
        """True for another name for a denied file (a hardlink to it)."""
        if not denied_inodes:
            return False
        try:
            info = os.stat(real, follow_symlinks=False)
        except OSError:
            return False
        return (info.st_dev, info.st_ino) in denied_inodes

    def _expand(roots) -> tuple[str, ...]:
        out: list[str] = []

        def _visit(path: str) -> None:
            nonlocal produced
            produced += 1
            if produced > _MAX_CARVED_ROOTS:
                raise SandboxError(
                    f"sandbox carve exceeded {_MAX_CARVED_ROOTS} roots; "
                    "a denied path is too deep under a large directory"
                )
            real = os.path.realpath(path)
            if real in denied_real or _is_denied_inode(real):
                return
            prefix = real if real.endswith(os.sep) else real + os.sep
            if not any(d.startswith(prefix) for d in denied_real):
                out.append(real)
                return
            try:
                with os.scandir(real) as entries:
                    children = sorted(
                        entry.path for entry in entries if not entry.is_symlink()
                    )
            except OSError:
                # Cannot enumerate, so cannot carve: deny the whole subtree.
                return
            containers.append(real)
            for child in children:
                _visit(child)

        for root in roots:
            _visit(root)
        return tuple(dict.fromkeys(out))

    # Order matters only for readability of the resulting policy: rights union,
    # so a path reached from both lists ends up with both grants.
    ro = _expand(read_only)
    rw = _expand(read_write)
    return SandboxPolicy(
        read_only=ro,
        read_write=rw,
        containers=tuple(dict.fromkeys(containers)),
    )


def apply_landlock_policy(policy: SandboxPolicy) -> None:
    """Restrict the CURRENT process (and its future children) to ``policy``.

    Call only in a process you intend to confine (the shim, or a test child):
    the restriction is irreversible for the process's lifetime. Raises
    ``SandboxError`` if Landlock is unavailable or any syscall fails, so the
    caller can fail closed.
    """
    abi = landlock_abi_version()
    if abi < 1:
        raise SandboxError("Landlock unavailable on this kernel")

    lib = _libc()

    # no_new_privs is required to install an unprivileged Landlock restriction.
    if lib.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SandboxError("prctl(PR_SET_NO_NEW_PRIVS) failed")

    handled_fs = _handled_fs_mask(abi)
    # struct landlock_ruleset_attr { u64 handled_access_fs; u64 handled_access_net; }
    # net=0: we do not restrict the network here (Landlock net does not enforce
    # on this kernel; egress is a separate stage). 16 bytes is accepted on every
    # ABI >= 1 because the trailing bytes are zero.
    attr = struct.pack("=QQ", handled_fs, 0)
    attr_buf = ctypes.create_string_buffer(attr, len(attr))
    ruleset_fd = lib.syscall(
        ctypes.c_long(_NR_landlock_create_ruleset),
        attr_buf,
        ctypes.c_size_t(len(attr)),
        ctypes.c_uint32(0),
    )
    if ruleset_fd < 0:
        raise SandboxError(f"landlock_create_ruleset failed (errno {ctypes.get_errno()})")

    try:
        # Read-only system roots may legitimately be symlinks (/bin -> /usr/bin);
        # canonicalize and register the target. Writable (tenant) roots must be
        # real directories: a symlinked writable root is rejected so it cannot
        # silently expand the boundary to the symlink's target (see caller
        # contract).
        _add_rules(lib, ruleset_fd, policy.read_only, _ACCESS_RO & handled_fs,
                   allow_symlink=True,
                   file_access=_ACCESS_RO_FILE & handled_fs)
        _add_rules(lib, ruleset_fd, policy.read_write, _ACCESS_RW & handled_fs,
                   allow_symlink=False,
                   file_access=_ACCESS_RW_FILE & handled_fs)
        # Containers are directories by construction (carved_policy only names
        # one it has successfully listed), so they need no file mask.
        _add_rules(lib, ruleset_fd, policy.containers, _ACCESS_CONTAINER & handled_fs,
                   allow_symlink=False)
        if lib.syscall(
            ctypes.c_long(_NR_landlock_restrict_self),
            ctypes.c_int(ruleset_fd),
            ctypes.c_uint32(0),
        ) != 0:
            raise SandboxError(
                f"landlock_restrict_self failed (errno {ctypes.get_errno()})"
            )
    finally:
        os.close(ruleset_fd)


def _open_root(path: str, *, allow_symlink: bool) -> int:
    """Open an allow root as an O_PATH fd, canonicalizing symlinks safely.

    Rejects a symlinked root when ``allow_symlink`` is False (writable roots), so
    a tenant workspace pointed at a broader target cannot silently expand the
    boundary. Raises ``OSError`` if the path is missing (caller skips it).
    """
    if not allow_symlink and os.path.islink(path):
        raise SandboxError(f"writable sandbox root {path!r} must not be a symlink")
    real = os.path.realpath(path)
    # realpath has resolved every component; O_NOFOLLOW guards a last-moment
    # swap of the (now non-symlink) final component. With O_PATH this yields an
    # fd to the resolved directory.
    return os.open(real, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)


def _add_rules(
    lib: ctypes.CDLL, ruleset_fd: int, paths, access: int, *, allow_symlink: bool,
    file_access: int | None = None,
) -> None:
    for path in paths:
        try:
            fd = _open_root(path, allow_symlink=allow_symlink)
        except OSError:
            # A missing allow root is not fatal: it simply stays unreachable.
            # (A symlink-rejected writable root raises SandboxError, not OSError,
            # so it propagates and fails the whole launch closed.)
            continue
        try:
            # Directory-only rights on a file are EINVAL, so narrow the mask for
            # a non-directory root. Decided from the OPEN fd, not the path, so a
            # swap between the two cannot change which mask is applied, and held
            # in a per-iteration name: assigning `access` here would leak the
            # narrowed mask to every root after the first file one.
            this_access = access
            if file_access is not None and not stat.S_ISDIR(os.fstat(fd).st_mode):
                this_access = file_access
            # struct landlock_path_beneath_attr { u64 allowed_access; s32 parent_fd; } packed
            pb = struct.pack("=Qi", this_access, fd)
            pb_buf = ctypes.create_string_buffer(pb, len(pb))
            if lib.syscall(
                ctypes.c_long(_NR_landlock_add_rule),
                ctypes.c_int(ruleset_fd),
                ctypes.c_uint32(_LANDLOCK_RULE_PATH_BENEATH),
                pb_buf,
                ctypes.c_uint32(0),
            ) != 0:
                raise SandboxError(
                    f"landlock_add_rule({path!r}) failed (errno {ctypes.get_errno()})"
                )
        finally:
            os.close(fd)


def _shim_path() -> str:
    return os.path.abspath(__file__)


def wrap_argv(argv, *, python_executable: str | None = None) -> list[str]:
    """Prefix ``argv`` with the sandbox shim launch.

    The returned argv runs the real command inside the Landlock sandbox. Pair
    with ``sandbox_env_overlay`` on the child env.

    ``-I -S`` is LOAD-BEARING, not tidiness, and removing it is a complete
    bypass of everything below. The shim is an interpreter, and an interpreter
    executes a great deal of code before it reaches this module's ``main``:
    ``site`` imports ``sitecustomize`` and ``usercustomize`` and runs every
    ``.pth`` file it finds. Those live in directories the agent's own uid can
    write (in the reference container, ``$HOME/.local/lib/python3.13/
    site-packages`` is mode 700 and owned by the running user), so without the
    flags a command could write ``usercustomize.py`` on one call and have it
    execute UNSANDBOXED inside the shim on the next, before
    ``apply_landlock_policy`` runs. That is the C1-01 finding restored in full,
    two tool calls wide.

    ``-S`` skips ``site`` entirely (no ``.pth``, no ``sitecustomize``,
    no ``usercustomize``); ``-I`` additionally ignores ``PYTHON*`` environment
    variables (``PYTHONSTARTUP`` is interactive-only, but ``PYTHONPATH`` is not)
    and drops the user site directory. Nothing is lost: the shim imports only
    stdlib, which ``-S`` leaves fully importable, and it ``execvp``s the real
    command as a fresh image with its own flags.
    """
    python = python_executable or sys.executable
    return [python, "-I", "-S", _shim_path(), "--", *argv]


def sandbox_env_overlay(policy: SandboxPolicy) -> dict[str, str]:
    """Env additions carrying the policy to the shim (merge into the child env)."""
    return {_SANDBOX_POLICY_ENV: policy.to_env_value()}


def _restore_default_signals() -> None:
    """Undo the interpreter's own signal dispositions before handing over.

    ``subprocess`` resets SIGPIPE and SIGXFSZ to SIG_DFL in the forked child
    (``restore_signals=True``), and then this shim starts a Python interpreter,
    which sets SIGPIPE back to SIG_IGN so writes raise BrokenPipeError instead
    of killing the process. SIG_IGN, unlike a handler, SURVIVES exec, so
    without this the sandboxed command inherits an ignored SIGPIPE and every
    ``producer | head`` prints "standard output: Broken pipe" instead of ending
    quietly. Restoring the same signals ``subprocess`` does keeps the shim
    invisible to the command it launches.
    """
    for name in ("SIGPIPE", "SIGXFSZ"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, signal.SIG_DFL)
            except (OSError, ValueError):
                pass  # not settable here; the command simply keeps what it got


def _shim_main(raw_argv: list[str]) -> int:
    # Launched by file path: sys.path[0] is this file's dir (the nymeria package
    # dir), which could shadow a stdlib module. Drop it; the shim is stdlib-only.
    if sys.path and sys.path[0] == os.path.dirname(_shim_path()):
        sys.path.pop(0)

    try:
        sep = raw_argv.index("--")
    except ValueError:
        sys.stderr.write("exec_sandbox shim: missing '--' command separator\n")
        return 2
    command = raw_argv[sep + 1:]
    if not command:
        sys.stderr.write("exec_sandbox shim: empty command\n")
        return 2

    raw_policy = os.environ.get(_SANDBOX_POLICY_ENV)
    if not raw_policy:
        # Fail closed: the shim is only launched for a sandboxed run.
        sys.stderr.write("exec_sandbox shim: no policy set; refusing to run unsandboxed\n")
        return 2
    try:
        policy = SandboxPolicy.from_env_value(raw_policy)
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"exec_sandbox shim: bad policy: {exc}\n")
        return 2

    try:
        apply_landlock_policy(policy)
    except SandboxError as exc:
        sys.stderr.write(f"exec_sandbox shim: sandbox not applied ({exc}); failing closed\n")
        return 2

    # Do not leak the policy var into the sandboxed program's environment.
    os.environ.pop(_SANDBOX_POLICY_ENV, None)
    _restore_default_signals()
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        sys.stderr.write(f"exec_sandbox shim: exec {command[0]!r} failed: {exc}\n")
        return errno.ENOEXEC


if __name__ == "__main__":
    raise SystemExit(_shim_main(sys.argv[1:]))
