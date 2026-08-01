"""Every agent-reachable spawn is sandboxed, or says at the call site why not.

The twin of ``test_subprocess_env_gate.py``: the same walk over the same calls,
asking the other half of the question. That gate asks what a child INHERITS;
this one asks what it may OPEN. Both exist because point fixes do not hold a
property that a single new line of ordinary-looking code can break.

C1-02 wired ``bash_execute``'s three spawns to the Landlock policy and left the
rest for a follow-up pass. Without a gate that is an invisible state: the
remaining sites look exactly like the wired ones, a twelfth site added next
month reads as ordinary, and the sandbox quietly covers a shrinking fraction of
the exec surface. So the unsandboxed sites are made to declare themselves, and
the list can only shrink.

SCOPE, and it is narrower than the env gate's on purpose. Only ``nymeria/core/``
and ``nymeria/tools/`` are gated: those are where a turn can reach a spawn.
``setup/``, ``cliproxy/``, ``service_install.py`` and ``api/routers/system.py``
are operator-invoked or Nymeria re-executing itself, and sandboxing them would
be either meaningless or actively wrong (the API re-exec must not be confined by
a policy built for a tool call). Passing this gate means "no new AGENT-REACHABLE
spawn was introduced unsandboxed and unexplained", not "every child is
confined".

The exemption marker lives at the call site rather than in a table here, for the
reason the env gate records: a path:line table breaks on any edit above the
spawn, which trains people to re-point entries mechanically.

    # sandbox-gate: unsandboxed - <reason>
    subprocess.run([...])
"""

from __future__ import annotations

import ast
from pathlib import Path

from test_subprocess_env_gate import _markers_above, _spawn_name

NYMERIA_ROOT = Path(__file__).resolve().parent.parent / "nymeria"

# Directories whose spawns a turn can reach. See SCOPE in the module docstring.
GATED_DIRS = ("core/", "tools/")

# The wrappers that put a child under the policy: the two policy-layer entry
# points (shell-shaped and argv-shaped) plus the mechanism-layer one underneath
# them, which a surface needing its own policy would call directly.
SANDBOX_WRAPPERS = frozenset({
    "sandbox_shell_launch", "sandbox_argv_launch", "wrap_argv",
})

UNSANDBOXED_MARKER = "sandbox-gate: unsandboxed"


def _call_name(node: ast.expr) -> str | None:
    """Bare function name of a call, whether called plainly or via a module."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _sandbox_wrapped_names(tree: ast.AST) -> set[str]:
    """Local names in this module bound to the output of a sandbox wrapper.

    The shipped shape is two lines, and an at-the-call-site check reads the
    second one as an ordinary spawn::

        launch = sandbox_shell_launch(command, kwargs)
        proc = subprocess.Popen(launch, **kwargs)

    Module-scoped and order-insensitive, matching the env gate's treatment of
    ``env = os.environ.copy()``. Over-matching here would let an unsandboxed
    spawn pass, so the binding has to come from the wrapper call itself rather
    than from anything that merely mentions it.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None or _call_name(value) not in SANDBOX_WRAPPERS:
            continue
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def _is_sandboxed(node: ast.Call, wrapped: set[str]) -> bool:
    """True if the argv handed to this spawn came through a sandbox wrapper.

    ``ast.Starred`` is unwrapped because ``asyncio.create_subprocess_exec``
    takes the program and its arguments positionally rather than as one list,
    so its sandboxed form is ``create_subprocess_exec(*launch, **kwargs)``.
    Without this the async surfaces could never satisfy the gate.
    """
    for arg in node.args:
        if isinstance(arg, ast.Starred):
            arg = arg.value
        if isinstance(arg, ast.Name) and arg.id in wrapped:
            return True
        if _call_name(arg) in SANDBOX_WRAPPERS:
            return True
    return False


def _walk_gated_spawns():
    """Yield ``(key, sandboxed, markers)`` per spawn in the gated directories."""
    for path in sorted(NYMERIA_ROOT.rglob("*.py")):
        rel = path.relative_to(NYMERIA_ROOT).as_posix()
        if not rel.startswith(GATED_DIRS):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        lines = source.splitlines()
        wrapped = _sandbox_wrapped_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _spawn_name(node):
                yield (
                    f"{rel}:{node.lineno}",
                    _is_sandboxed(node, wrapped),
                    _markers_above(lines, node.lineno),
                )


def test_every_agent_reachable_spawn_is_sandboxed_or_explained():
    violations = [
        key
        for key, sandboxed, markers in _walk_gated_spawns()
        if not sandboxed and UNSANDBOXED_MARKER not in markers
    ]
    assert not violations, (
        "These spawns are reachable from a turn and are not confined by the "
        "Landlock policy, so the command they launch can read the process "
        "environment (the vault master key, the service token, every provider "
        "key) and the credential stores:\n  " + "\n  ".join(violations)
        + "\n\nWrap the launch with nymeria.core.exec_policy.sandbox_shell_launch "
        "(or exec_sandbox.wrap_argv for a non-shell argv). If it genuinely "
        f"cannot be sandboxed yet, put a '# {UNSANDBOXED_MARKER} - <reason>' "
        "comment above the call saying what blocks it."
    )


def test_the_wired_surfaces_are_actually_sandboxed():
    """The gate is only worth having if it can tell wired from unwired.

    Without this, deleting the wiring would leave sites the gate reports as
    unsandboxed, which is only a failure if something asserts the positive case
    too. Counts per file rather than a total, so moving a spawn between these
    files cannot keep the sum right while losing one.
    """
    sandboxed = [key for key, is_sandboxed, _ in _walk_gated_spawns() if is_sandboxed]
    counts = {
        "tools/bash.py": 3,              # foreground, tracked bg, legacy bg
        "core/python_custom_tools.py": 1,  # the Python custom-tool runner
        "core/workflows/executor.py": 1,   # the workflow runner
        "core/hooks/actions.py": 1,        # the run_command hook action
        "core/mcp_runtime.py": 1,          # the npm/pip/git install runner
        "core/mcp_manager.py": 1,          # the MCP stdio server
        "core/validator.py": 1,            # the self-modification import check
        "tools/claude_code_bridge.py": 1,  # the git before/after summary
    }
    for prefix, expected in counts.items():
        found = [k for k in sandboxed if k.startswith(prefix)]
        assert len(found) == expected, (
            f"Expected {expected} sandboxed spawn(s) in {prefix}, found "
            f"{found}. All sandboxed sites: {sorted(sandboxed)}"
        )


def test_every_unsandboxed_marker_carries_a_reason():
    """A bare marker is an opt-out with nothing to review.

    Mirrors the env gate: the point of putting exemptions at the call site is
    that the next reader finds the justification there.
    """
    unexplained = []
    for key, _sandboxed, markers in _walk_gated_spawns():
        for line in markers.splitlines():
            if UNSANDBOXED_MARKER not in line:
                continue
            reason = line.split(UNSANDBOXED_MARKER, 1)[1].lstrip("#- ").strip()
            if len(reason) < 20:
                unexplained.append(key)
    assert not unexplained, (
        "These sandbox exemption markers have no usable reason:\n  "
        + "\n  ".join(unexplained)
    )
