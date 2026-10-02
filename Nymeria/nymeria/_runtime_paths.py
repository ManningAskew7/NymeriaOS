"""Runtime path bootstrap helpers for package and source launches."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional, Tuple


# The NYMERIA_PROJECT_ROOT this process was LAUNCHED with, captured at import:
# ``configure_project_root`` sets the variable itself from discovery, after
# which an exported root and a discovered one look the same. Both launchers
# import this module before anything can set it (#101 entry 10).
_LAUNCH_PROJECT_ROOT_ENV: Optional[str] = os.environ.get("NYMERIA_PROJECT_ROOT")
# A root named by a ``--root`` flag, selected by ``run.py`` before it loads any
# env file (``select_project_root``). Outranks the export, like the flag does.
_SELECTED_PROJECT_ROOT: Optional[Path] = None


def explicit_project_root() -> Optional[Path]:
    """The root the user named at launch, if any: a ``--root`` flag, else the export."""
    if _SELECTED_PROJECT_ROOT is not None:
        return _SELECTED_PROJECT_ROOT
    raw = (_LAUNCH_PROJECT_ROOT_ENV or "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def select_project_root(root: Path | str) -> Path:
    """Make ``root`` this process's project root, exactly as an export would.

    ``run.py`` calls this for a ``--root`` flag BEFORE it loads any env file
    (#101 entry 6, #451): the launch root's files used to load first, so
    ``nymeria init --root B`` ran with another install's environment and
    copied its vault key into B. Setting the variable here is what every later
    resolver reads (``config.settings`` computes its root at import, which
    happens after this), and recording the selection keeps
    ``explicit_project_root`` honest about a root the user named.
    """
    global _SELECTED_PROJECT_ROOT
    resolved = Path(root).expanduser().resolve()
    _SELECTED_PROJECT_ROOT = resolved
    os.environ["NYMERIA_PROJECT_ROOT"] = str(resolved)
    return resolved


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


def discover_project_root(start: Path | None = None) -> Path:
    """The root a launch resolves when nothing names one. Reads and writes no env.

    A frozen build or an installed wheel uses ``~/.nymeria``; a source checkout
    or an editable install uses the checkout's backend root.
    """
    if getattr(sys, "frozen", False):
        return default_user_project_root()

    base = (start or Path(__file__)).resolve()

    # Installed wheel (pip/pipx/uv tool): the shipped top-level run.py would
    # make the source-checkout markers match inside site-packages. Use the
    # writable per-user root so config/data survive upgrades.
    if is_installed_location(base):
        return default_user_project_root()

    return find_project_root(base) or default_user_project_root()


def bare_launch_root() -> Path:
    """The root a bare ``nymeria`` command resolves in the shell that launched this one.

    The launch-time export when there was one, else discovery. A ``--root``
    flag does not count: it names a root for this command only, which is why
    a command printed for a later shell has to repeat it.
    """
    raw = (_LAUNCH_PROJECT_ROOT_ENV or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return discover_project_root()


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

    runtime_root = discover_project_root(start)
    os.environ["NYMERIA_PROJECT_ROOT"] = str(runtime_root)
    return runtime_root
