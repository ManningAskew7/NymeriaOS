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
"""

from __future__ import annotations

import importlib.util
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


def local_rag_importable() -> bool:
    """True when ``sentence-transformers`` is already installed (extra present)."""
    return importlib.util.find_spec("sentence_transformers") is not None


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


def _uv_receipt_install_target(prefix: Path) -> Optional[list[str]]:
    """Reconstruct the uv tool install target (with the extra appended, plus any
    custom index from the receipt) so a reinstall preserves how it was originally
    installed: an editable dev checkout stays editable; a published wheel stays
    the named package; a private-index install keeps its index.

    Returns the argv tail that follows ``uv tool install --force``, or ``None``
    when the receipt is unreadable or does not describe the nymeriaos tool.
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
        editable = req.get("editable")
        target = (
            ["--editable", f"{editable}[{LOCAL_RAG_EXTRA}]"]
            if editable
            else [f"{PROJECT_NAME}[{LOCAL_RAG_EXTRA}]"]
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
        # is dropped by a later `uv tool upgrade`; recording it as an extra on the
        # install keeps it across upgrades, and this matches the supported path
        # the voice-local extra hint already points users to.
        if shutil.which("uv") is None:
            return None
        target = _uv_receipt_install_target(prefix_path)
        if target is None:
            return None
        return ["uv", "tool", "install", "--force", *target]
    if _pip_available():
        return [python, "-m", "pip", "install", SENTENCE_TRANSFORMERS_REQUIREMENT]
    # No pip (uv-managed venv): let uv install into that interpreter's env.
    if shutil.which("uv") is not None:
        return ["uv", "pip", "install", "--python", python, SENTENCE_TRANSFORMERS_REQUIREMENT]
    return None


def manual_install_hint(
    command: Optional[list[str]], *, prefix: Optional[str] = None
) -> str:
    """A copy-pasteable install string.

    Prefers the resolved ``command`` (shell-quoted so the ``[extra]`` brackets
    survive). When the command could not be resolved, falls back to a hint shaped
    for the install kind: ``uv tool install`` for a uv tool environment (which
    cannot be pip-installed into), else a portable ``pip install`` of the extra.
    """
    if command:
        return shlex.join(command)
    if _is_uv_tool_prefix(Path(prefix or sys.prefix)):
        return f'uv tool install --force "{PROJECT_NAME}[{LOCAL_RAG_EXTRA}]"'
    return f'pip install "{PROJECT_NAME}[{LOCAL_RAG_EXTRA}]"'


__all__ = [
    "LOCAL_RAG_EXTRA",
    "PROJECT_NAME",
    "SENTENCE_TRANSFORMERS_REQUIREMENT",
    "requires_local_rag",
    "local_rag_importable",
    "build_install_command",
    "manual_install_hint",
]
