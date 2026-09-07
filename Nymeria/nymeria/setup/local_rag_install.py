"""Install the optional ``local-rag`` extra when first-run setup picks a local
RAG stack.

``EMBEDDING_PROVIDER=local`` (granite) and ``RAG_RERANK_PROVIDER=local`` (Ettin)
run in-process via ``sentence-transformers`` (which pulls torch), shipped as the
optional ``local-rag`` extra so a cloud-embedder install stays lean. A user who
picks the local stack (or skips the RAG screen, which silently equips the same
free local stack via ``rag_catalog.apply_quickstart_rag``) otherwise lands a
working config whose embedder cannot load, spamming "local embedder unavailable"
on every ingest (``core/memory_index.py``). So ``finalize`` installs the extra
for them on the bare-metal shapes.

TUI-free (like ``rag_catalog`` / ``voice_catalog``) so ``finalize`` and the tests
share one source of truth. The install command depends on how Nymeria itself was
installed, recovered from the uv tool receipt (an editable dev checkout vs a
published wheel) with a plain-pip fallback for a venv install. Docker is handled
by the caller, not here: its dependencies live in the image, not a uv/pip env.

The wizard runs INSIDE the environment the uv command modifies, which shapes two
rules here. The in-process command never passes ``--force``: that flag makes uv
delete and recreate the tool environment (the running interpreter's own files),
whereas without it uv syncs a published install's environment in place and
still records the extra in the receipt, so a later ``uv tool upgrade`` keeps it
(an editable checkout may still be reinstalled, since uv implies reinstall for
a local-directory requirement; harmless on POSIX). And on Windows the
in-process install is refused outright (``in_process_install_blocked``): the
running ``nymeria.exe`` launcher and the loaded extension modules are locked,
uv cannot delete the launcher, its overwrite guard then finds the file still
present and removes the environment before bailing, leaving a half-deleted
install (the first Windows beta test, 2026-09-07). The manual command the user
is handed for a fresh shell keeps ``--force``: nothing is locked there, and it
is also what repairs a half-deleted environment. The receipt's existing extras
ride along on every reinstall (``nymeriaos[discord]`` becomes
``nymeriaos[discord,local-rag]``), because uv syncs the environment exactly to
the new requirement and rewrites the receipt: an extra left off is an extra
uninstalled.
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Mapping, Optional

# Matches the extra defined in pyproject's [project.optional-dependencies].
LOCAL_RAG_EXTRA = "local-rag"
# The package that uv installs as the `nymeria` tool.
PROJECT_NAME = "nymeriaos"
# Full Docker stack: the extra has to be IN the image, so finalize writes this
# flag to `.env.docker` and docker-compose.yml passes it to Dockerfile.full as a
# build arg (`x-nymeria-full-image`). The single-container shapes have no such
# hook yet and still get the generic hint.
DOCKER_LOCAL_RAG_ENV = "NYMERIA_LOCAL_RAG"

# Pin mirrors pyproject's local-rag extra; used for the plain-pip path, which adds
# the dependency directly rather than reinstalling nymeriaos (so an editable
# source checkout is not re-resolved from an index).
SENTENCE_TRANSFORMERS_REQUIREMENT = "sentence-transformers>=3.0.0"


def requires_local_rag(env: Mapping[str, str]) -> bool:
    """True when the resolved config drives an in-process (local) embedder or
    reranker, so ``sentence-transformers`` must be importable.

    Keys off the env ``finalize`` is about to write (``rag_env_for_state``), not
    the UI selection, so the Ctrl+S quickstart default (which equips the local
    granite + Ettin stack) is covered alongside an explicit local pick.
    """
    return "local" in (env.get("EMBEDDING_PROVIDER"), env.get("RAG_RERANK_PROVIDER"))


def _module_importable(name: str) -> bool:
    """True when ``name`` can be imported by the running interpreter.

    Invalidates the import caches first: ``FileFinder`` caches each ``sys.path``
    directory listing and rechecks it only when the directory's mtime changes,
    so a package the wizard just installed (written by a child process after
    this interpreter started) can read as missing on a same-second write or a
    coarse-timestamp filesystem until the caches are dropped. That is what
    lets the post-install capability row be a real check, not a config echo.
    """
    importlib.invalidate_caches()
    return importlib.util.find_spec(name) is not None


def local_rag_importable() -> bool:
    """True when ``sentence-transformers`` is already installed (extra present)."""
    return _module_importable("sentence_transformers")


def _is_uv_tool_prefix(prefix: Path) -> bool:
    """True when the running interpreter lives in a uv-managed tool environment.

    uv installs each tool under ``.../uv/tools/<name>``; mirrors the detection in
    ``finalize._print_voice_hints``.
    """
    return "uv/tools" in prefix.as_posix()


def _pip_available() -> bool:
    """True when ``pip`` can be invoked in the current interpreter.

    A uv-managed project venv (``uv venv`` / ``uv sync``, the shape this repo's
    own ``.venv`` uses) ships no pip, so ``python -m pip install`` would fail with
    ``No module named pip``; the caller routes around it via ``uv pip`` instead.
    """
    return importlib.util.find_spec("pip") is not None


def _uv_receipt_index_flags(tool: dict) -> list[str]:
    """Re-emit the package indexes recorded in the receipt's ``[tool.options]`` so
    a reinstall hits the same (possibly private) index the original install used,
    not the default PyPI where a private pre-beta package would not resolve.

    Mirrors ``install.sh``'s published private-index track, which runs
    ``uv tool install nymeriaos --index <url>``; uv records that as
    ``[tool.options] index = [{ url = ..., default = false, ... }]``.
    """
    flags: list[str] = []
    for entry in (tool.get("options") or {}).get("index") or []:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not url:
            continue
        flags += ["--default-index", url] if entry.get("default") else ["--index", url]
    return flags


def _uv_receipt_install_target(
    prefix: Path, extra: str = LOCAL_RAG_EXTRA
) -> Optional[list[str]]:
    """Reconstruct the uv tool install target (with ``extra`` added to the extras
    the receipt already records, plus any custom index from the receipt) so a
    reinstall preserves how it was originally installed: an editable dev
    checkout stays editable; a published wheel stays the named package; a
    private-index install keeps its index; ``nymeriaos[discord]`` keeps discord
    (uv syncs the environment exactly to the new requirement and rewrites the
    receipt, so an extra left off here would be uninstalled).

    Returns the argv tail that follows ``uv tool install``, or ``None`` when
    the receipt is unreadable or does not describe the nymeriaos tool.
    """
    receipt = prefix / "uv-receipt.toml"
    try:
        data = tomllib.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tool = data.get("tool") or {}
    for req in tool.get("requirements") or []:
        if not isinstance(req, dict) or req.get("name") != PROJECT_NAME:
            continue
        extras = [e for e in (req.get("extras") or []) if isinstance(e, str)]
        if extra not in extras:
            extras.append(extra)
        spec = f"[{','.join(extras)}]"
        editable = req.get("editable")
        target = (
            ["--editable", f"{editable}{spec}"] if editable else [f"{PROJECT_NAME}{spec}"]
        )
        return [*_uv_receipt_index_flags(tool), *target]
    return None


def build_install_command(
    *, prefix: Optional[str] = None, executable: Optional[str] = None
) -> Optional[list[str]]:
    """The command that installs the local-rag extra into the *current* runtime,
    or ``None`` when it cannot be determined (the caller then prints a manual
    hint instead of guessing).

    Shapes, in order of preference:

    - uv tool install (how the ``nymeria`` CLI ships): reinstall the tool with the
      extra, preserving editable-vs-published from the receipt. Needs the ``uv``
      binary on PATH.
    - plain venv with pip: add ``sentence-transformers`` to the active interpreter
      directly (not a nymeriaos reinstall, so an editable source checkout is not
      re-resolved from an index).
    - uv-managed venv without pip (``uv venv`` / ``uv sync``): install the same
      dependency into that interpreter with ``uv pip``.

    Returns ``None`` when none of these is drivable (a uv-tool prefix with no
    ``uv`` or an unreadable receipt, or a pip-less venv with no ``uv``), so the
    caller prints a manual hint instead of a command that cannot run.

    ``prefix`` / ``executable`` default to the running interpreter's; they are
    parameters so the resolution is unit-testable without a real install.
    """
    prefix_path = Path(prefix or sys.prefix)
    python = executable or sys.executable
    if _is_uv_tool_prefix(prefix_path):
        # Reinstall the tool with the extra (editable-vs-published recovered from
        # the receipt) rather than `uv pip install` into the tool venv. Though uv
        # does allow pip-installing into a tool venv, a dependency added that way
        # is dropped by a later `uv tool upgrade` (verified); recording it as an
        # extra on the install keeps it across upgrades, and this matches the
        # supported path the voice-local extra hint already points users to.
        # Deliberately no `--force`: this runs from inside the environment being
        # modified, and `--force` makes uv delete and recreate that environment
        # under the running interpreter. Without it uv syncs in place, records
        # the extra all the same, and a repeat run is a no-op.
        if shutil.which("uv") is None:
            return None
        target = _uv_receipt_install_target(prefix_path)
        if target is None:
            return None
        return ["uv", "tool", "install", *target]
    if _pip_available():
        return [python, "-m", "pip", "install", SENTENCE_TRANSFORMERS_REQUIREMENT]
    # No pip (uv-managed venv): let uv install into that interpreter's env.
    if shutil.which("uv") is not None:
        return ["uv", "pip", "install", "--python", python, SENTENCE_TRANSFORMERS_REQUIREMENT]
    return None


def in_process_install_blocked(
    prefix: Optional[str] = None, *, platform: Optional[str] = None
) -> Optional[str]:
    """Why the wizard must not run the install from inside itself, or ``None``.

    Only a uv tool install on Windows is blocked. There the running
    ``nymeria.exe`` launcher (uv's trampoline waits on the interpreter for the
    whole run) and the loaded extension modules are locked. With ``--force``
    uv deletes the environment and stops at the first locked file; without it
    the sync succeeds but uv cannot delete the launcher, and its
    "executable already exists" overwrite guard then removes the environment
    before bailing. Either way the locks leave the install half-deleted. On
    POSIX an in-place sync is safe, and a plain venv install adds a dependency
    without touching nymeriaos at all. The caller prints the reason with the
    manual command for a fresh shell.

    ``prefix`` / ``platform`` default to the running interpreter's, as
    parameters so the gate is unit-testable off-platform.
    """
    platform = sys.platform if platform is None else platform
    if platform != "win32" or not _is_uv_tool_prefix(Path(prefix or sys.prefix)):
        return None
    return (
        "Windows keeps the running nymeria's files locked and uv rebuilds the "
        "tool environment they live in, so the extra cannot be installed from "
        "inside setup."
    )


def _with_force(command: list[str]) -> list[str]:
    """The uv tool command with ``--force`` (once), other commands unchanged.

    The manual command runs from a fresh shell where nothing is locked, so the
    rebuild is safe there, and it is what repairs a half-deleted environment
    after a failed in-process attempt.
    """
    if command[:3] == ["uv", "tool", "install"] and "--force" not in command:
        return [*command[:3], "--force", *command[3:]]
    return list(command)


# Characters an argument may carry unquoted in BOTH cmd.exe and PowerShell.
# Anything else (whitespace, `[extra]` brackets, the `>` of a version pin,
# `&|^;()` and quotes) gets double quotes, which both shells honor;
# ``shlex.join``'s single quotes are literal characters to cmd.exe.
_WINDOWS_BARE_ARG = re.compile(r"^[\w.:/\\=@+,%-]+$")


def _windows_join(argv: list[str]) -> str:
    """Join ``argv`` for a Windows shell (cmd.exe and PowerShell alike)."""
    return " ".join(arg if _WINDOWS_BARE_ARG.match(arg) else f'"{arg}"' for arg in argv)


def _join(argv: list[str], *, windows: bool) -> str:
    return _windows_join(argv) if windows else shlex.join(argv)


def extra_install_hint(
    extra: str, *, prefix: Optional[str] = None, windows: Optional[bool] = None
) -> str:
    """A fresh-shell command that adds ``extra`` to however Nymeria is installed.

    A uv tool environment cannot be pip-installed into durably, so its hint is
    the ``uv tool install --force`` reinstall reconstructed from the receipt
    (editable-vs-published, custom index, existing extras kept); with no
    readable receipt, the plain published shape. Anything else gets a portable
    ``pip install`` of the extra. Shared by the local-rag and voice-local
    hints so both quote for the platform and neither drops the other's extra.
    """
    windows = sys.platform == "win32" if windows is None else windows
    prefix_path = Path(prefix or sys.prefix)
    if _is_uv_tool_prefix(prefix_path):
        target = _uv_receipt_install_target(prefix_path, extra)
        if target is not None:
            return _join(["uv", "tool", "install", "--force", *target], windows=windows)
        return f'uv tool install --force "{PROJECT_NAME}[{extra}]"'
    return f'pip install "{PROJECT_NAME}[{extra}]"'


def manual_install_hint(
    command: Optional[list[str]],
    *,
    prefix: Optional[str] = None,
    windows: Optional[bool] = None,
) -> str:
    """A copy-pasteable install string for a fresh shell.

    Prefers the resolved ``command``, with ``--force`` added to the uv tool
    shape (see ``_with_force``) and quoted for the platform (``_join``). When
    the command could not be resolved, falls back to ``extra_install_hint``,
    shaped for the install kind.
    """
    windows = sys.platform == "win32" if windows is None else windows
    if command:
        return _join(_with_force(command), windows=windows)
    return extra_install_hint(LOCAL_RAG_EXTRA, prefix=prefix, windows=windows)


__all__ = [
    "LOCAL_RAG_EXTRA",
    "PROJECT_NAME",
    "SENTENCE_TRANSFORMERS_REQUIREMENT",
    "requires_local_rag",
    "local_rag_importable",
    "build_install_command",
    "in_process_install_blocked",
    "extra_install_hint",
    "manual_install_hint",
]
