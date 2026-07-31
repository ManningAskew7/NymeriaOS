"""Every child process must be launched with a deliberate environment.

The API process holds the vault master key, the service token, the database and
Redis credentials, and every provider API key. A spawn with no ``env=`` hands
all of it to the child. That is not theoretical: the inventory behind this gate
found fifteen such sites, including one that fed the whole environment to
``uv``/``pip``, which then execute build scripts from a package index.

Point fixes do not hold this. A new ``subprocess.run`` is one line and reads as
completely ordinary, so the leak returns the next time somebody shells out.
Hence a gate rather than a set of per-site regressions: a new spawn either
passes an environment or is named in the exemption table below with a reason.

Two assertions, because they catch different mistakes:

1. Every spawn passes ``env=``. Catches the bare inherit.
2. No spawn passes ``os.environ.copy()`` or ``dict(os.environ)``. Catches the
   denylist shape, which looks careful and is not: it protects against the
   names its author thought of on the day, and every variable added later
   leaks by default. Four sites were in this shape when the gate was written.

Implemented over the AST rather than by matching lines, deliberately. Several
correct call sites build their kwargs dict earlier and unpack it, so a
line-oriented check reports them as violations while missing the real ones.

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
#  - The copy check only sees an inline os.environ.copy() at the call site. An
#    environment built in a helper and passed in reads as clean here; the
#    helpers are reviewed by hand instead.
# Passing this gate means "no NEW bare spawn was introduced", not "no process
# inherits secrets".

# path:line -> why this spawn may inherit the parent environment.
# Adding an entry is a security decision. Write the reason for the next reader,
# not for yourself today.
EXEMPT_BARE_ENV: dict[str, str] = {
    "triggers/cli/script_segments.py:166":
        "User-authored statusline script from the local ~/.nymeria/cli.json. "
        "Runs on the user's own machine, under their own account, where the "
        ".env is already readable by them, so scrubbing buys little and would "
        "break scripts that legitimately read the environment.",
    # Remaining known gaps from the 2026-07-31 inventory. Listed rather than
    # silently passing, so the count is visible and shrinking. All are in the
    # wizard/CLI process, which run.py loads the full deployment .env into, so
    # they are real, just lower value than the API-process sites already fixed.
    "setup/environment.py:221": "docker info probe. Not yet scrubbed.",
    "setup/environment.py:237": "docker compose version probe. Not yet scrubbed.",
    "setup/environment.py:253": "docker ps probe. Not yet scrubbed.",
    "setup/environment.py:286": "ss -tlnp port probe. Not yet scrubbed.",
    "setup/environment.py:307": "lsof port probe. Not yet scrubbed.",
    "setup/external_access.py:137": "tailscale status. Not yet scrubbed.",
    "setup/external_access.py:187": "tailscale up. Not yet scrubbed.",
    "setup/external_access.py:275": "tailscale serve/funnel. Not yet scrubbed.",
    "setup/cliproxy_deploy.py:207": "docker compose up. Not yet scrubbed.",
}

# path:line -> why this spawn may build its environment from a full copy.
# Separate table from the one above: these sites DO pass env=, they just build
# it the leaky way, so they fail a different assertion and deserve their own
# reasons.
EXEMPT_ENVIRON_COPY: dict[str, str] = {
    "api/routers/system.py:167":
        "Re-exec of the API process itself during a self-restart. The child IS "
        "this service and must come up with identical configuration; the code "
        "re-merges dotenv over the copy precisely so a restart picks up config "
        "edits. A scrubbed env here is a broken backend, not a hardened one.",
}


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


def _passes_env(node: ast.Call) -> bool:
    """True if the call supplies env=, including via **kwargs unpacking.

    ``**kwargs`` counts as passing. The gate cannot see inside the dict, and
    the sites that build kwargs earlier are the ones already doing this right;
    treating them as violations would train people to ignore the gate.
    """
    return any(kw.arg == "env" or kw.arg is None for kw in node.keywords)


def _env_copy_source(node: ast.Call) -> bool:
    """True if any argument is os.environ.copy() or dict(os.environ)."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "copy"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
        ):
            return True
        if isinstance(func, ast.Name) and func.id == "dict":
            for arg in sub.args:
                if isinstance(arg, ast.Attribute) and arg.attr == "environ":
                    return True
    return False


def _walk_spawns():
    """Yield (key, node) for every process-spawning call under nymeria/."""
    for path in sorted(NYMERIA_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        rel = path.relative_to(NYMERIA_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _spawn_name(node):
                yield f"{rel}:{node.lineno}", node


def test_every_spawn_supplies_an_environment():
    violations = [
        key for key, node in _walk_spawns()
        if not _passes_env(node) and key not in EXEMPT_BARE_ENV
    ]
    assert not violations, (
        "These spawns inherit the parent environment, which includes the vault "
        "master key, the service token, and the database credentials:\n  "
        + "\n  ".join(violations)
        + "\n\nPass env=scrubbed_subprocess_env(...) from nymeria.subprocess_env, "
        "adding only the non-secret names the child genuinely needs. If the "
        "child must inherit (it is Nymeria itself, re-executing), add it to "
        "EXEMPT_BARE_ENV in this file with a reason."
    )


def test_no_spawn_builds_its_environment_from_a_full_copy():
    violations = [
        key for key, node in _walk_spawns()
        if _env_copy_source(node) and key not in EXEMPT_ENVIRON_COPY
    ]
    assert not violations, (
        "These spawns build a child environment from os.environ.copy() or "
        "dict(os.environ):\n  " + "\n  ".join(violations)
        + "\n\nThat is a denylist. It protects against the variable names its "
        "author thought of, and every name added to the deployment afterwards "
        "leaks by default. Start from scrubbed_subprocess_env() and add what "
        "the child needs."
    )


def test_the_exemption_table_has_no_stale_entries():
    """An exemption for a line that no longer spawns is a lie in a security file.

    Without this, refactors leave entries behind that read as reviewed
    decisions about code that has moved or gone, and the next reader trusts
    them.
    """
    live = {key for key, _ in _walk_spawns()}
    stale = sorted((set(EXEMPT_BARE_ENV) | set(EXEMPT_ENVIRON_COPY)) - live)
    assert not stale, (
        "These exemptions no longer point at a spawn (the line moved or the "
        "call was deleted):\n  " + "\n  ".join(stale)
        + "\n\nRe-point them at the current line or delete them."
    )
