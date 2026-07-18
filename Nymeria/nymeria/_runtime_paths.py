"""Runtime path bootstrap helpers for package and source launches."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple


PROJECT_ROOT_MARKERS: Tuple[Tuple[str, ...], ...] = (
    ("run.py", "nymeria/config/soul.md"),
    ("docker-compose.yml", "nymeria/config/settings.py"),
)


def find_project_root(start: Path) -> Optional[Path]:
    """Find a source-checkout backend root by walking upward from ``start``."""
    current = start.expanduser().resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        for markers in PROJECT_ROOT_MARKERS:
            if all((candidate / marker).exists() for marker in markers):
                return candidate
    return None


def default_user_project_root() -> Path:
    """Return the writable runtime root used by pip/pipx installations."""
    return (Path.home() / ".nymeria").expanduser().resolve()


def is_installed_location(path: Path) -> bool:
    """True when ``path`` lives inside a Python install tree.

    A wheel install (pip/pipx/uv tool/venv) places this package under a
    ``site-packages`` directory (Debian system packages use ``dist-packages``);
    a source checkout or an editable (``pip install -e``) install does not.

    We need this because the wheel ships ``run.py`` as a top-level module
    beside ``nymeria/``, so the source-checkout markers in
    ``PROJECT_ROOT_MARKERS`` also match INSIDE ``site-packages``. Without this
    guard an installed backend is misread as a source checkout rooted at
    ``site-packages`` and strands all config/data there, where an upgrade or
    reinstall deletes it. Detecting the install tree lets us fall through to
    ``default_user_project_root()`` (``~/.nymeria``) instead.
    """
    parts = {part.lower() for part in path.resolve().parts}
    return "site-packages" in parts or "dist-packages" in parts


def configure_project_root(start: Path | None = None) -> Path:
    """
    Ensure ``NYMERIA_PROJECT_ROOT`` exists before settings are imported.

    Source checkouts keep using the backend root. Installed wheels and frozen
    executables without an explicit override use ``~/.nymeria`` for config and
    data.
    """
    env_root = os.environ.get("NYMERIA_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    if getattr(sys, "frozen", False):
        runtime_root = default_user_project_root()
        os.environ["NYMERIA_PROJECT_ROOT"] = str(runtime_root)
        return runtime_root

    base = (start or Path(__file__)).resolve()

    # Installed wheel (pip/pipx/uv tool): the shipped top-level run.py would
    # make the source-checkout markers match inside site-packages. Use the
    # writable per-user root so config/data survive upgrades.
    if is_installed_location(base):
        runtime_root = default_user_project_root()
        os.environ["NYMERIA_PROJECT_ROOT"] = str(runtime_root)
        return runtime_root

    discovered = find_project_root(base)
    if discovered:
        os.environ["NYMERIA_PROJECT_ROOT"] = str(discovered)
        return discovered

    runtime_root = default_user_project_root()
    os.environ["NYMERIA_PROJECT_ROOT"] = str(runtime_root)
    return runtime_root
