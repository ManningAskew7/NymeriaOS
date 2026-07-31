"""Every child process must be launched with a deliberate environment.

The API process holds the vault master key, the service token, the database and
Redis credentials, and every provider API key. A spawn with no ``env=`` hands
all of it to the child. That is not theoretical: the inventory behind this gate
found fifteen such sites, including one that fed the whole environment to
``uv``/``pip``, which then execute build scripts from a package index.

Point fixes do not hold this. A new ``subprocess.run`` is one line and reads as
completely ordinary, so the leak returns the next time somebody shells out.
Hence a gate rather than a set of per-site regressions: a new spawn either
passes an environment or carries a marker comment with a reason.

Two assertions, because they catch different mistakes:

1. Every spawn passes a real ``env=``. Catches the bare inherit, and ``env=None``,
   which means the same thing to ``subprocess``.
2. No spawn builds its environment from ``os.environ``. Catches the denylist
   shape, which looks careful and is not: it protects against the names its
   author thought of on the day, and every variable added later leaks by
   default.

Implemented over the AST rather than by matching lines, deliberately. Several
correct call sites build their kwargs dict earlier and unpack it, so a
line-oriented check reports them as violations while missing the real ones.

Both assertions track local variables, which is the difference between a gate
and a formality. Almost nobody writes the leaky shape inline; the real code is
``env = os.environ.copy()`` three lines above the spawn, and the real
``**kwargs`` is a dict literal assembled just above the call. Checking only the
call's own arguments misses every genuine instance of both.

Follows the tree-walk idiom of ``test_exception_logging.py``, with the ``ast``
technique of ``test_import_hygiene.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

NYMERIA_ROOT = Path(__file__).resolve().parent.parent / "nymeria"

# Callables that start a child process, keyed by module. Per-module rather than
# one flat set of names: ``asyncio.run`` is not a spawn but shares a name with
# ``subprocess.run``, and a flat set reports every ``asyncio.run(...)`` in the
# tree as a leaking subprocess. ``os.exec*``/``os.spawn*`` are absent on
# purpose: exec replaces the current image and takes no env argument, so the
# only such site (exec_sandbox.py) needs no gate entry.
SPAWN_ATTRS_BY_MODULE: dict[str, frozenset[str]] = {
    "subprocess": frozenset({
        "run", "Popen", "call", "check_call", "check_output",
    }),
    "asyncio": frozenset({
        "create_subprocess_exec", "create_subprocess_shell",
    }),
}

# What this gate CANNOT see, stated so nobody reads a green run as a clean
# tree. It matches calls syntactically, so:
#  - A spawn dispatched through an indirection (service_install.py routes
#    systemctl/launchctl through a self._runner attribute) is invisible.
#  - A library that shells out internally is invisible: pydub spawns
#    ffmpeg/ffprobe from core/voice.py, playwright launches a node driver and
#    Chromium from tools/browser.py (and its launch() accepts an env= that the
#    call site does not pass), and webbrowser.open() spawns a browser at three
#    setup sites.
#  - Variable tracking is per module. An environment or kwargs dict built in a
#    HELPER and returned is opaque here (bash.py's _background_popen_kwargs is
#    the deliberate example); those helpers are reviewed by hand instead.
# Passing this gate means "no NEW bare spawn was introduced", not "no process
# inherits secrets".

# Exemptions live at the call site as a marker comment, not in a table here.
#
#     # env-gate: inherit - <reason>
#     subprocess.run([...])
#
#     # env-gate: full-copy - <reason>
#     subprocess.Popen([...], env=child_env)
#
# The marker goes in the comment block immediately above the spawn (blank lines
# between are fine). Adding one is a security decision: write the reason for
# the next reader, not for yourself today.
#
# This started as a path:line table and moved here after the keying broke twice
# in a single pass, both times because an unrelated import was added above a
# spawn. A key that a two-line edit can invalidate trains people to re-point
# entries mechanically, which is the opposite of what an exemption is for. At
# the call site the reason is also where the reader already is, and it cannot
# go stale: delete the call and the marker goes with it.
INHERIT_MARKER = "env-gate: inherit"
FULL_COPY_MARKER = "env-gate: full-copy"


def _spawn_name(node: ast.Call) -> str | None:
    """Return ``module.attr`` if this call starts a process, else None."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    value = func.value
    # subprocess.run(...) / asyncio.create_subprocess_exec(...)
    if isinstance(value, ast.Name):
        module = value.id
    # asyncio.subprocess.* shows up as a nested attribute
    elif isinstance(value, ast.Attribute):
        module = value.attr
    else:
        return None
    if func.attr in SPAWN_ATTRS_BY_MODULE.get(module, frozenset()):
        return f"{module}.{func.attr}"
    return None


def _env_argument(node: ast.Call) -> ast.expr | None:
    """The expression passed as ``env=``, or None if the call passes none.

    ``env=None`` counts as passing none, because that is exactly what it means
    to ``subprocess``: the child inherits. Reading it as "an environment was
    supplied" would let a spawn opt out of the gate by writing the default.
    """
    for kw in node.keywords:
        if kw.arg == "env":
            if isinstance(kw.value, ast.Constant) and kw.value.value is None:
                return None
            return kw.value
    return None


def _locally_built_dicts_without_env(tree: ast.AST) -> set[str]:
    """Names in this module provably bound to a dict that has no ``env`` key.

    Narrows the ``**kwargs`` escape below. The escape has to exist, because a
    kwargs dict assembled in a helper and returned (``bash.py``'s
    ``_background_popen_kwargs``) is genuinely opaque here. But the local shape

        kwargs = {}
        kwargs["start_new_session"] = True
        await asyncio.create_subprocess_shell(cmd, **kwargs)

    is not opaque at all, and it was passing the gate while demonstrably
    supplying no environment.

    "Provably" is the operative word, and this errs hard toward NOT proving it:
    a name is dropped from the result the moment anything about it is unclear,
    including an assignment from a call, a ``{**other}`` splat, a subscript
    write with a non-literal key, an ``.update()``, or being handed to a
    function that could mutate it (``with_tool_oom_score(kwargs)`` does exactly
    that). A false positive here would be a maintainer arguing with a test
    about code that is already correct, which is how gates lose their
    credibility.
    """
    bound: set[str] = set()
    unclear: set[str] = set()

    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value

        for target in targets:
            if isinstance(target, ast.Name):
                bound.add(target.id)
                if not isinstance(value, ast.Dict):
                    unclear.add(target.id)  # assigned from something opaque
                elif any(
                    key is None
                    or not isinstance(key, ast.Constant)
                    or key.value == "env"
                    for key in value.keys
                ):
                    unclear.add(target.id)  # splat, computed key, or env itself
            elif isinstance(target, ast.Subscript) and isinstance(
                target.value, ast.Name
            ):
                literal_other_key = (
                    isinstance(target.slice, ast.Constant)
                    and target.slice.value != "env"
                )
                if not literal_other_key:
                    unclear.add(target.value.id)

        if isinstance(node, ast.Call):
            # Mutating method on the name, or the name handed to a function
            # that may add to it.
            if isinstance(node.func, ast.Attribute) and isinstance(
                node.func.value, ast.Name
            ):
                if node.func.attr not in {"get", "keys", "values", "items"}:
                    unclear.add(node.func.value.id)
            unclear.update(arg.id for arg in node.args if isinstance(arg, ast.Name))

    return bound - unclear


def _passes_env(node: ast.Call, dicts_without_env: set[str] = frozenset()) -> bool:
    """True if the call supplies a real env=, including via **kwargs unpacking.

    ``**kwargs`` counts as passing unless the dict was built in this module and
    provably never gains an ``env`` key. The escape exists because a kwargs
    dict assembled in a helper is opaque here, and treating those as violations
    would train people to ignore the gate.
    """
    for kw in node.keywords:
        if kw.arg is not None:
            continue
        if isinstance(kw.value, ast.Name) and kw.value.id in dicts_without_env:
            continue
        return True
    return _env_argument(node) is not None


def _is_environ_copy(node: ast.expr) -> bool:
    """True for os.environ.copy(), dict(os.environ), {**os.environ}."""
    if isinstance(node, ast.Dict):
        return any(
            key is None and isinstance(value, ast.Attribute) and value.attr == "environ"
            for key, value in zip(node.keys, node.values)
        )
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "copy"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "environ"
    ):
        return True
    if isinstance(func, ast.Name) and func.id == "dict":
        return any(
            isinstance(arg, ast.Attribute) and arg.attr == "environ" for arg in node.args
        )
    return False


def _environ_copy_names(tree: ast.AST) -> set[str]:
    """Local names in this module bound to a full copy of the environment.

    The reason this exists: almost nobody writes the leaky shape inline. The
    real code is

        env = os.environ.copy()
        env["EXTRA"] = "..."
        subprocess.run(..., env=env)

    which an at-the-call-site check reads as clean. Every genuine instance in
    this tree is that shape, so before this the second assertion could not fire
    and its exemption table was decoration.

    Module-scoped and order-insensitive on purpose. A gate should err toward
    flagging: a false positive costs one exemption line with a reason, a false
    negative costs a silent leak.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_environ_copy(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if _is_environ_copy(node.value) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _builds_env_from_full_copy(node: ast.Call, copy_names: set[str]) -> bool:
    env = _env_argument(node)
    if env is None:
        return False
    if _is_environ_copy(env):
        return True
    return isinstance(env, ast.Name) and env.id in copy_names


def _markers_above(lines: list[str], lineno: int) -> str:
    """Text of the contiguous comment block immediately above ``lineno``.

    Scans upward from the line before the call, through comments and blank
    lines, and stops at the first line of code. That covers the marker sitting
    directly above the call and the marker heading a longer explanatory
    comment, which is the shape these reasons usually want.
    """
    collected: list[str] = []
    index = lineno - 2  # 0-based, one line above the call
    while index >= 0:
        stripped = lines[index].strip()
        if stripped.startswith("#"):
            collected.append(stripped)
        elif stripped:
            break
        index -= 1
    return "\n".join(collected)


def _walk_spawns():
    """Yield a record per process-spawning call under ``nymeria/``."""
    for path in sorted(NYMERIA_ROOT.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        rel = path.relative_to(NYMERIA_ROOT).as_posix()
        lines = source.splitlines()
        copy_names = _environ_copy_names(tree)
        empty_kwargs = _locally_built_dicts_without_env(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _spawn_name(node):
                yield (
                    f"{rel}:{node.lineno}",
                    node,
                    copy_names,
                    _markers_above(lines, node.lineno),
                    empty_kwargs,
                )


def test_every_spawn_supplies_an_environment():
    violations = [
        key for key, node, _, markers, empty_kwargs in _walk_spawns()
        if not _passes_env(node, empty_kwargs) and INHERIT_MARKER not in markers
    ]
    assert not violations, (
        "These spawns inherit the parent environment, which includes the vault "
        "master key, the service token, and the database credentials:\n  "
        + "\n  ".join(violations)
        + "\n\nPass env=scrubbed_subprocess_env(...) from nymeria.subprocess_env, "
        "adding only the non-secret names the child genuinely needs. If the "
        f"child must inherit (it is Nymeria itself, re-executing), put a "
        f"'# {INHERIT_MARKER} - <reason>' comment above the call."
    )


def test_no_spawn_builds_its_environment_from_a_full_copy():
    violations = [
        key for key, node, copy_names, markers, _ in _walk_spawns()
        if _builds_env_from_full_copy(node, copy_names)
        and FULL_COPY_MARKER not in markers
    ]
    assert not violations, (
        "These spawns build a child environment from os.environ.copy() or "
        "dict(os.environ):\n  " + "\n  ".join(violations)
        + "\n\nThat is a denylist. It protects against the variable names its "
        "author thought of, and every name added to the deployment afterwards "
        "leaks by default. Start from scrubbed_subprocess_env() and add what "
        f"the child needs, or justify it with '# {FULL_COPY_MARKER} - <reason>'."
    )


def test_every_exemption_marker_carries_a_reason():
    """A bare marker is an opt-out with nothing to review.

    The point of moving exemptions to the call site was that the next reader
    finds the justification there. A marker with no text after it is the same
    silent pass the gate exists to prevent, just spelled differently.
    """
    unexplained = []
    for key, _node, _copy_names, markers, _ in _walk_spawns():
        for line in markers.splitlines():
            for marker in (INHERIT_MARKER, FULL_COPY_MARKER):
                if marker not in line:
                    continue
                reason = line.split(marker, 1)[1].lstrip("#- ").strip()
                if len(reason) < 20:
                    unexplained.append(f"{key} ({marker})")
    assert not unexplained, (
        "These exemption markers have no usable reason:\n  "
        + "\n  ".join(unexplained)
    )
