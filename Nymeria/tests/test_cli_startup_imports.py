"""Import-graph guards for the thin CLI's launch path.

The thin CLI (``--transport api``, the default) is an HTTP client: it talks to
a backend that owns the agent. It must therefore not import the agent runtime,
the API server, or the tool catalog at launch.

That property is easy to break by accident and invisible when you do, because
a stray module-level convenience import still *works*. It just quietly costs
seconds. These tests measure the import graph rather than the clock, so they
fail deterministically on a slow or loaded box.

Historical context: a single eager ``from .api import create_api_app`` in
``nymeria/triggers/__init__.py`` (plus sibling package inits) made
``from nymeria.triggers.cli import run_cli`` cost ~14.9s and dragged in
langchain, transformers and torch. Launch went from ~16.4s to ~1.7s when the
re-exports became lazy.

Most tests here must stay cheap, because the repo sets a global 30s per-test
timeout (``pyproject.toml``) and resolving a name like ``create_api_app`` for
real costs ~13.5s, which flakes under ``-n``. The one test that genuinely needs
the full import (``test_every_lazy_export_resolves_for_real``) overrides that
timeout explicitly rather than being left to race it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Modules the thin CLI must never pull in at import time. Each entry is here
# because it was measured as a real cost, not on principle, EXCEPT
# `transport.in_process`: it is cheap now that `core/__init__.py` is lazy, but
# it is the seam the fat-client deferral rides on, so it is pinned to keep
# `cli/app.py`'s function-local import from being "tidied" up to module scope.
FORBIDDEN_AT_CLI_IMPORT = (
    "nymeria.core.agent",
    "nymeria.triggers.api",
    "nymeria.tools",
    "nymeria.vendor.react_agent.providers",
    "nymeria.triggers.cli.transport.in_process",
    "langchain_core",
    "langchain_openai",
    "langgraph",
    "transformers",
    "torch",
    "fastapi",
    # ~120ms, measured 2026-08-07 when an eager nymeria/cliproxy/__init__.py
    # rode the header's catalog import onto the launch path. The transport
    # modules that need httpx are themselves imported lazily, so launch
    # never needs it.
    "httpx",
)

# Packages whose public export surface this file pins. Most serve `__all__`
# through a PEP 562 `__getattr__` over a `_LAZY_EXPORTS` table;
# `triggers.cli.transport` has neither and is included because its surface
# shrank in the same pass, so the checks below tolerate the table's absence.
EXPORT_SURFACE_PACKAGES = (
    "nymeria",
    "nymeria.cliproxy",
    "nymeria.core",
    "nymeria.triggers",
    "nymeria.triggers.cli.transport",
    "nymeria.vendor.react_agent",
)


def _run_python(code: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Run ``code`` in a clean interpreter rooted at the repo.

    A subprocess is the point: within one pytest session another test may have
    already imported the agent, so an in-process ``sys.modules`` check would
    pass or fail depending on test ordering.

    Note the default timeout is mostly a backstop, not the governing limit:
    the repo's global 30s pytest-timeout fires first for every test here
    except the one carrying an explicit ``@pytest.mark.timeout``. That is fine
    (worst measured probe is well under a second); it just means this value
    only really applies to the slow test that raises its own ceiling.
    """
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _probe(result: subprocess.CompletedProcess[str], prefix: str) -> str:
    """Extract a ``prefix``-tagged verdict line, failing loudly if absent.

    Guards against a vacuous pass: a probe that dies before printing must not
    look like a clean run.
    """
    assert result.returncode == 0, f"probe failed:\n{result.stderr}"
    lines = [ln for ln in result.stdout.splitlines() if ln.startswith(prefix)]
    assert lines, f"probe produced no {prefix!r} verdict:\n{result.stdout!r}"
    return lines[0][len(prefix) :]


def test_thin_cli_import_does_not_load_agent_stack() -> None:
    """Importing the CLI REPL must not load the agent, API, or tool catalog."""
    result = _run_python(
        "import nymeria._runtime_paths as rp; rp.configure_project_root()\n"
        "from nymeria.triggers.cli.app import CLIApp  # noqa: F401\n"
        "import sys\n"
        f"forbidden = {FORBIDDEN_AT_CLI_IMPORT!r}\n"
        "print('LOADED:' + ','.join(m for m in forbidden if m in sys.modules))\n"
    )

    loaded = [name for name in _probe(result, "LOADED:").split(",") if name]
    assert not loaded, (
        "Thin CLI import pulled in the agent stack: "
        + ", ".join(loaded)
        + ". Something gained a module-level import of a heavy sibling; move it "
        "into the function that needs it, or behind the package's __getattr__."
    )


def test_run_cli_entry_does_not_load_agent_stack() -> None:
    """The packaged entry symbol must stay as cheap as the app module."""
    result = _run_python(
        "import nymeria._runtime_paths as rp; rp.configure_project_root()\n"
        "from nymeria.triggers.cli import run_cli  # noqa: F401\n"
        "import sys\n"
        f"forbidden = {FORBIDDEN_AT_CLI_IMPORT!r}\n"
        "print('LOADED:' + ','.join(m for m in forbidden if m in sys.modules))\n"
    )

    loaded = [name for name in _probe(result, "LOADED:").split(",") if name]
    assert not loaded, (
        "`from nymeria.triggers.cli import run_cli` pulled in: "
        + ", ".join(loaded)
        + ". The CLI package init or one of its parents regained an eager import."
    )


def test_run_module_import_does_not_load_agent_stack() -> None:
    """`run.py` is the real launcher, so its module scope must stay light too.

    Without this, a heavy module-level import added to `run.py` or
    `cli_entry.py` would restore the regression for actual `nymeria cli` users
    while the library-level guards above stayed green.
    """
    result = _run_python(
        "import nymeria._runtime_paths as rp; rp.configure_project_root()\n"
        "import run  # noqa: F401\n"
        "import sys\n"
        f"forbidden = {FORBIDDEN_AT_CLI_IMPORT!r}\n"
        "print('LOADED:' + ','.join(m for m in forbidden if m in sys.modules))\n"
    )

    loaded = [name for name in _probe(result, "LOADED:").split(",") if name]
    assert not loaded, (
        "Importing `run` pulled in the agent stack: "
        + ", ".join(loaded)
        + ". Keep run.py's module scope to stdlib plus lazy helpers; command "
        "runners import their dependencies inside the function."
    )


def test_lazy_export_tables_cover_every_public_name() -> None:
    """Fast drift guard on the two parallel lists each lazy package carries.

    A name in `__all__` but missing from `_LAZY_EXPORTS` (a typo, or a symbol
    added to only one list) would raise AttributeError at runtime and break
    `from pkg import *`. This also checks the table's VALUES name real modules,
    via `find_spec`, which catches a mistyped target without importing it.

    This is the cheap check and it runs in well under a second;
    `test_every_lazy_export_resolves_for_real` is the definitive one.
    """
    result = _run_python(
        "import importlib, importlib.util\n"
        f"packages = {EXPORT_SURFACE_PACKAGES!r}\n"
        "problems = []\n"
        "for pkg in packages:\n"
        "    mod = importlib.import_module(pkg)\n"
        "    lazy = getattr(mod, '_LAZY_EXPORTS', {})\n"
        "    eager = vars(mod)\n"
        "    for name in mod.__all__:\n"
        "        if name not in lazy and name not in eager:\n"
        "            problems.append(pkg + '.' + name + '(unmapped)')\n"
        "    for name in lazy:\n"
        "        if name not in mod.__all__:\n"
        "            problems.append(pkg + '.' + name + '(not in __all__)')\n"
        "    for target in sorted(set(lazy.values())):\n"
        "        if importlib.util.find_spec(target, pkg) is None:\n"
        "            problems.append(pkg + ' -> ' + target + '(no such module)')\n"
        "print('PROBLEMS:' + ','.join(problems))\n"
    )

    problems = [p for p in _probe(result, "PROBLEMS:").split(",") if p]
    assert not problems, (
        "Lazy export tables drifted: "
        + ", ".join(problems)
        + ". Every public name must appear in both `__all__` and either "
        "`_LAZY_EXPORTS` or the module's eager globals, and every "
        "`_LAZY_EXPORTS` target must be a real submodule."
    )


@pytest.mark.timeout(300)
def test_every_lazy_export_resolves_for_real() -> None:
    """Actually resolve all ~51 public names across the lazy packages.

    The cheap guard above validates table membership and that each target
    module EXISTS, but not that the named symbol actually lives there: a
    plausible-but-wrong entry (`"TodoStatus": ".todo_schedule_db"`) passes it
    and fails at runtime. Only real resolution catches that.

    This costs ~14s because it necessarily imports the agent, the API app and
    the tool catalog, so it carries an explicit generous timeout instead of
    racing the repo's global 30s one. That race is exactly what made an earlier
    version of this file flaky.

    Deliberately NOT marked `slow`, though it is the obvious candidate: this is
    the only layer that catches a wrong-home table entry, so putting it behind
    a `-m 'not slow'` lane would silently downgrade drift protection to the
    layer whose own docstring says it is insufficient. 14s in a ~10min suite
    is not worth that.
    """
    result = _run_python(
        "import logging; logging.disable(logging.CRITICAL)\n"
        "import importlib\n"
        f"packages = {EXPORT_SURFACE_PACKAGES!r}\n"
        "broken = []\n"
        "for pkg in packages:\n"
        "    mod = importlib.import_module(pkg)\n"
        "    for name in mod.__all__:\n"
        "        try:\n"
        "            getattr(mod, name)\n"
        "        except Exception as exc:\n"
        "            broken.append(pkg + '.' + name + '(' + type(exc).__name__ + ')')\n"
        "print('BROKEN:' + ','.join(broken))\n",
        timeout=240,
    )

    broken = [b for b in _probe(result, "BROKEN:").split(",") if b]
    assert not broken, (
        "Public names that do not resolve: "
        + ", ".join(broken)
        + ". Check the `_LAZY_EXPORTS` target for each: the symbol must "
        "actually be defined in the module the table points at."
    )


def test_cheap_lazy_reexport_resolves_for_real() -> None:
    """Prove the __getattr__ machinery genuinely works, using a cheap submodule.

    `react_agent.config` costs ~19ms, so this exercises real resolution without
    importing the ~6.4s provider stack the structural test deliberately avoids.
    """
    result = _run_python(
        "from nymeria.vendor.react_agent import LLMConfig, AgentConfig\n"
        "print('OK:' + LLMConfig.__name__ + ',' + AgentConfig.__name__)\n"
    )

    assert _probe(result, "OK:") == "LLMConfig,AgentConfig"


def test_lazy_package_raises_attribute_error_for_unknown_name() -> None:
    """Unknown attributes must still raise AttributeError, not ImportError.

    `hasattr` and `getattr(..., default)` depend on this, as does any code that
    probes for an optional symbol.
    """
    result = _run_python(
        "import nymeria.core as core\n"
        "try:\n"
        "    core.definitely_not_a_real_symbol\n"
        "except AttributeError:\n"
        "    print('RESULT:AttributeError')\n"
        "except Exception as exc:\n"
        "    print('RESULT:' + type(exc).__name__)\n"
    )

    assert _probe(result, "RESULT:") == "AttributeError"


def test_submodule_fallback_does_not_mask_a_real_import_error() -> None:
    """A submodule with a broken dependency must raise ITS error, not AttributeError.

    This pins the subtlest line in the lazy inits:

        if exc.name != f"{__name__}.{name}": raise

    Without it, `__getattr__`'s submodule fallback swallows every
    `ModuleNotFoundError` raised *inside* a submodule and reports the symptom
    as "package has no attribute X". The debugging experience is brutal: a
    missing optional dependency looks like a typo'd attribute name.

    Every other test in this file still passes if that guard is deleted, which
    is exactly why it needs its own.
    """
    probe = REPO_ROOT / "nymeria" / "core" / "_lazy_guard_probe.py"
    probe.write_text(
        "import definitely_missing_third_party_xyz  # noqa: F401\n",
        encoding="utf-8",
    )
    try:
        result = _run_python(
            "import logging; logging.disable(logging.CRITICAL)\n"
            "import nymeria.core\n"
            "try:\n"
            "    nymeria.core._lazy_guard_probe\n"
            "except ModuleNotFoundError as exc:\n"
            "    print('RESULT:ModuleNotFoundError:' + str(exc.name))\n"
            "except AttributeError:\n"
            "    print('RESULT:AttributeError')\n"
            "else:\n"
            "    print('RESULT:no-error')\n"
        )
        verdict = _probe(result, "RESULT:")
    finally:
        probe.unlink(missing_ok=True)

    assert verdict == "ModuleNotFoundError:definitely_missing_third_party_xyz", (
        "The submodule fallback masked a real import error (got "
        f"{verdict!r}). The `exc.name` guard in the package's __getattr__ must "
        "re-raise anything that is not this submodule being genuinely absent."
    )


def test_trigger_sources_autoload_without_eager_package_import() -> None:
    """The sources registry still populates on first access.

    ``nymeria/triggers/__init__.py`` used to import ``.sources`` eagerly to
    "have them ready when the API starts". That import was redundant:
    ``sources/__init__.py`` auto-loads its plugins at module scope and every
    consumer imports it function-locally, so the registry fills on first use.
    """
    result = _run_python(
        "import logging; logging.disable(logging.CRITICAL)\n"
        "from nymeria.triggers.sources import AVAILABLE_SOURCES\n"
        "print('COUNT:' + str(len(AVAILABLE_SOURCES)))\n"
    )

    assert int(_probe(result, "COUNT:")) > 0, (
        "No trigger sources auto-loaded; the sources registry is empty."
    )
