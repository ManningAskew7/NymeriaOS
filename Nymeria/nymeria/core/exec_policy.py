"""What a spawned child may REACH on disk: the twin of ``subprocess_env.py``.

``subprocess_env.py`` is the single source of truth for what a child process
INHERITS. This module is the single source of truth for what it may OPEN, and
it is the caller side of ``nymeria/exec_sandbox.py``: the exec surfaces build
their policy here rather than assembling roots at each call site, so "what can
a shell read" has one answer instead of one per surface.

The claim is narrow, and stating it exactly is the point (C1-01/C1-02). A
sandboxed child keeps the reach it had before, MINUS:

* ``/proc``, because ``/proc/1/environ`` is the deployment's whole environment
  (the credential vault's master key, every provider key, the service token)
  and PID 1 holds a copy of it that no in-container hardening can close: with
  ``init: true`` PID 1 is tini, and no process can make another undumpable.
  A short list of ``/proc`` files that carry no per-process state is granted
  back individually, which is why ``free`` still works and ``ps`` does not.
* the credential and account stores in the data dir
  (``core/resource_map.SECRET_AT_REST_CHILDREN``), WHERE THAT IS AFFORDABLE.
  Landlock cannot deny a path without making every ancestor of it read-opaque
  to files created later, so denying inside a working directory would cost more
  than it buys; ``_creation_roots`` is where that line is drawn, and it is why
  the Docker deployment (data dir outside the project root) gets these denied
  and a source checkout does not.

What it does NOT claim, and none of it is new: the child still reaches every
other user's profile and transcript data (``SECURITY.md`` 2.2 concedes that
these are not tenants), and it can still delete or replace a denied file even
though it cannot read one, because the directory holding it stays writable.
Reading is what the finding is about.

The one residual worth naming here rather than only in ``SECURITY.md``: a
secret file that lives INSIDE the working tree stays readable, on every
deployment. That includes the deployment's own dotenv file, which is
``project_root/.env`` by construction, and any credential JSON an operator has
left in the project root. Denying those is the thing the rule above rules out,
so the sandbox closes the process environment and leaves the file it was loaded
from. See ``denied_paths``.

Why this lives in ``core/`` and not beside ``subprocess_env.py`` at the package
root, where the rest of the child-process controls sit: those four are LEAVES,
importing nothing from the package (``oom`` takes one sibling constant), which
is what lets ``run.py::main()`` harden the process before anything heavy
loads. This module cannot be a leaf. Its answer is per deployment, so it reads
settings and the resource register, and a root module that imports ``core``
would invert that layer. The mechanism half stayed at the root
(``exec_sandbox.py``); this is the policy half.
"""

from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache
from typing import Any, MutableMapping, Sequence

from ..config import get_settings
from ..exec_sandbox import (
    DEFAULT_DEVICE_NODES,
    DEFAULT_SYSTEM_ROOTS,
    SandboxError,
    SandboxPolicy,
    carved_policy,
    sandbox_available,
    sandbox_env_overlay,
    wrap_argv,
)
from .resource_map import secret_at_rest_paths

logger = logging.getLogger(__name__)

# Individual ``/proc`` files granted back on top of the ``/proc`` denial. Each
# one is machine-wide and carries no per-process state, so none of them leaks
# another process's environment, memory map, or file descriptors. They are what
# ``free``, ``uptime`` and ``lscpu`` read, and what several language runtimes
# consult for CPU and memory sizing.
#
# Anything under ``/proc/<pid>/`` (including ``/proc/self``, which resolves in
# the PARENT and would pin the wrong process) is deliberately absent, so tools
# that walk the process table (``ps``, ``top``, ``pgrep``) do not work in a
# sandboxed shell. That is the measured cost of the denial, not an oversight.
PROC_READABLE_FILES: tuple[str, ...] = (
    "/proc/meminfo",
    "/proc/cpuinfo",
    "/proc/stat",
    "/proc/loadavg",
    "/proc/uptime",
    "/proc/version",
)

_unavailable_warned = False
_dropped_logged: set[tuple[str, ...]] = set()
_expensive_logged: set[tuple[str, ...]] = set()


@lru_cache(maxsize=1)
def _interpreter_roots() -> tuple[str, ...]:
    """Roots a child needs to run this deployment's Python.

    The shim is itself a Python process, and a sandboxed child that spawns
    Python (a workflow runner, a custom tool) needs the same. Covered already
    when the policy starts from ``/``, and named anyway so a narrower policy
    stays correct rather than failing closed at exec time.
    """
    return tuple(dict.fromkeys((
        os.path.dirname(os.path.realpath(sys.executable)),
        os.path.realpath(sys.prefix),
        os.path.realpath(sys.base_prefix),
    )))


def sandbox_enabled() -> bool:
    """True when spawns should be wrapped in the Landlock sandbox.

    Degrades rather than refuses when the kernel cannot enforce it (non-Linux,
    or a kernel without Landlock), with one warning per process. Refusing there
    would take Windows and macOS deployments offline for a control they cannot
    run; the module header's "multi-tenant must refuse" arm needs a
    multi-tenant notion this codebase does not have yet.
    """
    global _unavailable_warned
    # No try/except around get_settings(). It used to swallow everything and
    # return False, which is a SILENT fail-open: the one state where the
    # sandbox is skipped without a word in the log. If settings cannot be read
    # the caller is already broken (every spawn site resolves its cwd from
    # them), so letting it raise fails the tool call with an error instead,
    # which is what the rest of this module promises.
    if not get_settings().exec_sandbox_enabled:
        return False
    if not sandbox_available():
        if not _unavailable_warned:
            _unavailable_warned = True
            logger.warning(
                "Landlock is unavailable on this kernel, so spawned commands "
                "run without the filesystem sandbox: a child can read the "
                "process environment and the credential stores. Set "
                "EXEC_SANDBOX_ENABLED=false to silence this."
            )
        return False
    return True


def _creation_roots(
    cwd: str | os.PathLike[str] | None,
    override: Sequence[str | os.PathLike[str]] | None = None,
) -> tuple[str, ...]:
    """Directories where a command is expected to create files and read them back.

    This is the limit on what can be denied, and it comes from Landlock rather
    than from policy. Denying a path makes every ANCESTOR of it read-opaque:
    the ancestor cannot grant READ_FILE (rights union UP the tree, so granting
    it there would re-open the denied file), and Landlock has no wildcard, so
    only the entries that existed when the policy was built get their own rule.
    An entry created after that is writable and unreadable. Inside a working
    directory that turns ordinary shell work into permission errors:
    ``cmd > out.txt && grep x out.txt``, ``mkdir build && ...``, ``npm install``.

    So a denied path inside one of these is dropped rather than carved. The
    stores are worth a great deal; they are not worth a shell that cannot read
    what it just wrote, and the operator who wants them denied anyway has a
    supported way to get it: put the data dir outside the working tree, which
    is how the Docker deployment is already laid out (project root ``/app``,
    data dir ``/data``, so nothing is dropped there).

    Two roots, and the pair is the decision:

    * The launch's own ``cwd``, which is the directory the command will
      actually write in. Passing it is what stops a command run with an
      explicit ``working_directory`` from having that very directory carved.
    * ``project_root``, which is both the default cwd and the tree a command
      is most likely to ``cd`` into partway through.

    ``$HOME`` was here and was dropped: it is not a working directory for
    agent-run commands (the default is project root, and an explicit one now
    arrives as ``cwd``), so keeping it only widened the drop on the one layout
    where it changes anything, a data dir under home but outside the project.
    The residual is a command that ``cd``s somewhere new and reads back a file
    it created there in the same invocation; across invocations the policy is
    rebuilt, so the file is readable next call.

    ``override`` replaces both roots for a surface that knows its child's whole
    write area, and it is a TIGHTENING, not a convenience. The pair above is
    sized for ``bash_execute``, whose child is a general-purpose shell working
    in the project tree. A runner that hands its child an ephemeral directory
    and nothing else inherits that concession for free, and on the layout where
    the data dir sits INSIDE the project root (a source checkout, so the slim
    shape) the concession is the whole control: every store is inside a
    creation root, so the deny set collapses to ``/proc`` alone. Naming the run
    directory instead gets the stores denied there too.

    Only pass it where the claim is actually true. The cost of being wrong is
    the measured one: an entry created directly inside a carved directory after
    the policy is built is writable and unreadable for the rest of that launch.
    """
    if override is not None:
        return tuple(dict.fromkeys(str(root) for root in override))
    roots = [str(get_settings().project_root)]
    if cwd:
        roots.append(str(cwd))
    return tuple(dict.fromkeys(roots))


# Entry budget for the WHOLE store deny set, counted over the union of the
# ancestor directories the carve has to replace with per-entry rules. A data
# dir with a handful of top-level children costs tens; this only bites a store
# parked under a directory holding hundreds of unrelated entries (``/tmp``, a
# spool dir), where carving would blow the policy past what execve will carry.
# Dropping the denial there beats failing every command with an opaque error.
#
# Whole set rather than per path, and the difference is not cosmetic. Per path
# it was possible for seven denials to pass individually and still sum past the
# mechanism's own ceiling, which raises: a data-dir layout change would have
# taken bash_execute out entirely with a message about carve roots. Counting
# the union also stops the shared ancestors (they all live under one data dir)
# from being charged seven times.
#
# The number is chosen against ``exec_sandbox._MAX_CARVED_ROOTS`` (1500) with
# room for the rest of the carve: expanding ``/`` for the ``/proc`` denial and
# the base system roots come to a few dozen. The two budgets stay separate
# because they answer different questions: that one is a MECHANISM invariant
# (this policy cannot be transported, so raise), this one a POLICY judgement
# (this denial is not worth what it costs, so drop it and say so). Merging them
# would put the decision to abandon a security control inside the module that
# knows nothing about which control it is.
_MAX_CARVE_ENTRIES = 900


def _carve_cost(paths: Sequence[str]) -> int:
    """Roughly how many rules carving ``paths`` out will produce.

    Every ancestor directory of a denied path is replaced by one rule per entry
    it holds, so the cost is the entry count of the union of those ancestors.
    An unlistable directory costs nothing: the carve drops that subtree whole.
    """
    counted: set[str] = set()
    total = 0
    for path in paths:
        current = os.path.dirname(os.path.realpath(path))
        while True:
            if current not in counted:
                counted.add(current)
                try:
                    with os.scandir(current) as entries:
                        total += sum(1 for _ in entries)
                except OSError:
                    # Unlistable, so it costs nothing: the carve cannot
                    # enumerate it either and drops the whole subtree, which is
                    # one rule rather than many. Nothing to log.
                    pass
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return total


def _inside(path: str, root: str) -> bool:
    real_root = os.path.realpath(root)
    prefix = real_root if real_root.endswith(os.sep) else real_root + os.sep
    return os.path.realpath(path).startswith(prefix)


def _log_once(seen: set[tuple[str, ...]], key: tuple[str, ...]) -> bool:
    """True the first time ``key`` is seen, with a cap so ``seen`` cannot grow.

    The policy is rebuilt per launch, so an unguarded message here is one line
    per command. Keyed on the paths rather than on the call, because what an
    operator needs to see is each distinct outcome once.
    """
    if key in seen or len(seen) >= 32:
        return False
    seen.add(key)
    return True


def denied_paths(
    cwd: str | os.PathLike[str] | None = None,
    *,
    creation_roots: Sequence[str | os.PathLike[str]] | None = None,
) -> tuple[str, ...]:
    """Paths no sandboxed child may read, resolved for this deployment.

    ``/proc`` is always denied: its only ancestor is ``/``, which is not a
    place commands create files. The on-disk stores are denied only where that
    does not cost a working directory (see ``_creation_roots``), which is a
    per-deployment answer rather than a per-install setting, and which
    ``creation_roots`` lets a surface narrow when it knows its child's whole
    write area.
    """
    settings = get_settings()
    candidates: list[str] = [str(path) for path in secret_at_rest_paths(settings.data_dir)]
    # The dotenv files are NOT candidates, and the reason is worth stating
    # because they hold the same secrets as the environment this control is
    # about. ``get_env_file_paths`` is ``project_root / <name>`` by
    # construction, and project_root is always a creation root, so a dotenv
    # denial could never survive the rule below: it would be dead code that
    # read as coverage. The measured breakage that forbids carving a working
    # directory is what forbids it here. This is the sandbox's largest
    # residual on a source checkout and on the reference Docker deployment,
    # where the repo is bind-mounted at /app and /app/.env comes with it: a
    # spawned command cannot read the environment but can still read the file
    # the environment was loaded from. Filed, with the file-tool half, as its
    # own item (backlog: "is .env an admin-gated store").
    working = _creation_roots(cwd, creation_roots)
    kept: list[str] = ["/proc"]
    dropped: list[str] = []
    for path in dict.fromkeys(candidates):
        if any(_inside(path, root) for root in working):
            dropped.append(path)
        else:
            kept.append(path)

    # All or nothing on the store arm: they share a data dir, so they share the
    # ancestors that cost anything, and a deny set that covers the token cache
    # but not the vault beside it is not a control worth the surprise.
    expensive: list[str] = []
    if len(kept) > 1 and _carve_cost(kept[1:]) > _MAX_CARVE_ENTRIES:
        expensive = kept[1:]
        kept = ["/proc"]

    if dropped and _log_once(_dropped_logged, tuple(dropped)):
        logger.info(
            "Exec sandbox: denying /proc. The credential stores under %s stay "
            "readable by spawned commands because they sit inside the working "
            "tree (%s), and denying a path there would stop commands reading "
            "files they had just written. Set a data dir outside the working "
            "tree to have them denied too.",
            settings.data_dir,
            ", ".join(working),
        )
    if expensive and _log_once(_expensive_logged, tuple(expensive)):
        logger.warning(
            "Exec sandbox: leaving the credential stores (%s) reachable by "
            "spawned commands. Denying them means enumerating every entry of "
            "their parent directories, which is too large here to fit in one "
            "launch. Move the data dir somewhere with fewer sibling entries to "
            "have them denied. /proc is still denied.",
            ", ".join(expensive),
        )
    return tuple(kept)


def tool_sandbox_policy(
    cwd: str | os.PathLike[str] | None = None,
    *,
    creation_roots: Sequence[str | os.PathLike[str]] | None = None,
) -> SandboxPolicy:
    """The policy for an agent-invoked command launched in ``cwd``.

    Starts from the whole filesystem and subtracts, rather than listing what a
    command may touch: the tools this policy governs are general-purpose by
    design, and an allowlist of useful roots would be a guess that breaks a
    different command every week. Everything the child could reach before it
    can still reach, minus ``denied_paths(cwd)``.
    """
    return carved_policy(
        read_only=(*DEFAULT_SYSTEM_ROOTS, *_interpreter_roots(), *PROC_READABLE_FILES),
        read_write=("/", *DEFAULT_DEVICE_NODES),
        denied=denied_paths(cwd, creation_roots=creation_roots),
    )


def _sandbox_launch(
    argv: Sequence[str],
    popen_kwargs: MutableMapping[str, Any],
    caller: str,
    creation_roots: Sequence[str | os.PathLike[str]] | None = None,
) -> list[str]:
    """Overlay the policy onto ``popen_kwargs`` and wrap ``argv`` in the shim.

    Raises rather than degrading if the policy cannot be built: a launch that
    was meant to be sandboxed must not quietly become one that is not. The
    caller must have already set ``env``, for the same reason: falling back to
    ``os.environ`` here would hand a child the API process's whole environment
    through a helper, which is precisely the shape
    ``tests/test_subprocess_env_gate.py`` exists to refuse and precisely the
    shape it cannot see (its tracking is per module, so an env dict built in a
    helper is opaque to it). Every spawn site already scrubs.
    """
    base_env = popen_kwargs.get("env")
    if base_env is None:
        raise SandboxError(
            f"{caller} requires an explicit env: pass "
            "subprocess_env.scrubbed_subprocess_env(...) (or the surface's own "
            "scrub) in the spawn kwargs rather than inheriting this process's "
            "environment."
        )
    policy = tool_sandbox_policy(
        popen_kwargs.get("cwd"), creation_roots=creation_roots
    )
    popen_kwargs["env"] = {**base_env, **sandbox_env_overlay(policy)}
    return wrap_argv(list(argv))


def sandbox_argv_launch(
    argv: Sequence[str],
    popen_kwargs: MutableMapping[str, Any],
    *,
    creation_roots: Sequence[str | os.PathLike[str]] | None = None,
) -> list[str]:
    """Sandbox an explicit-argv launch in place; return the argv to spawn.

    For the surfaces that already build a real argv (the Python custom-tool
    runner, the workflow runner) rather than handing a string to a shell. On an
    unsandboxed deployment the argv comes back unchanged and ``popen_kwargs`` is
    not modified.

    Works for ``asyncio.create_subprocess_exec`` too, which takes the program
    and its arguments positionally: build the kwargs as a dict, pass it here,
    then splat both (``*launch, **kwargs``).

    Pass ``creation_roots`` when the child's whole write area is known and is
    NARROWER than "the project tree plus cwd", which is what a surface gets by
    default. See ``_creation_roots``: on a source checkout that default drops
    every store denial, so a runner working entirely in a scratch directory
    should say so and keep them.
    """
    if not sandbox_enabled():
        return list(argv)
    return _sandbox_launch(
        argv, popen_kwargs, "sandbox_argv_launch", creation_roots=creation_roots
    )


def sandbox_shell_launch(
    command: str, popen_kwargs: MutableMapping[str, Any]
) -> str | list[str]:
    """Sandbox a ``shell=True`` launch in place; return what to pass to Popen.

    On an unsandboxed deployment the command comes back untouched and
    ``popen_kwargs`` is not modified. Otherwise ``shell`` is turned off and the
    equivalent explicit argv is wrapped in the shim, because the shim needs a
    real argv and ``shell=True`` with a list means something else entirely
    (the tail becomes ``$0``, ``$1``, ... rather than the command).
    """
    if not sandbox_enabled():
        return command

    # What subprocess itself runs for shell=True on POSIX, so the shell, its
    # quoting and its builtins are unchanged.
    launch = _sandbox_launch(
        ["/bin/sh", "-c", command], popen_kwargs, "sandbox_shell_launch"
    )
    # After the wrap, not before: _sandbox_launch raises on a caller that did
    # not scrub its env, and a raise that had already flipped shell off would
    # leave the caller's kwargs describing a launch it never asked for.
    popen_kwargs["shell"] = False
    return launch
