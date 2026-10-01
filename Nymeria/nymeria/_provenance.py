"""Which code this process booted from, and whether newer code is on disk.

An editable or source install (the slim shape) and every bind-mounted Docker
container import the package at process start and hold those modules for the
life of the process. A fix can land, pass its tests, and sit unused because
nothing restarted the process; nothing said so until a fixed bug came back
(backlog #101 entry 23b). This module answers "what is this process running"
and "is there newer code on disk" for the startup log line and ``/status``,
and gives deploy automation the booted identity on ``/status/turns`` (#423).

Deliberately light (stdlib only): ``run.py::main()`` captures the record
before the subcommand imports the rest of the package, so the start time
below is the process's own. An in-place ``os.execve`` restart keeps the pid
but starts a fresh interpreter, which re-imports this module, so these clocks
are right where the process create-time is not.

The commit is read from ``.git`` directly rather than by running ``git``: no
child process to scrub or confine, it works in images that ship no git, and it
is cheap enough to repeat on every ``/status``. Anything unreadable yields
None; a provenance label never blocks startup or a command.

Staleness is decided by a stat fingerprint (count, summed mtimes, summed
sizes) of every file in the package, not only ``*.py``: prompts such as
``config/soul.md`` and data files are read once at boot too; plus the
``run.py`` entry point beside it (bots, worker and slim logic live there, and
containers bind-mount it). Not by git: it catches uncommitted edits,
bind-mounted source with no ``.git`` (a manually updated container), and an
in-place wheel upgrade, and it ignores a HEAD that moved without touching
the code. uv links files from its cache, so an upgrade can move mtimes
BACKWARDS; hence sums compared for equality, never a newest-mtime test.

Known limits: the commit is HEAD at boot, so uncommitted edits the process
loaded are not in its label; a self-modify write into the package followed
by ``reload_all`` still reads as drift (the reload covers only the tool
modules, and a restart clears it); a reftable-format repository reports no
commit.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__
from ._runtime_paths import is_installed_location

# Captured at import; ``run.py`` imports this module at startup. The wall
# clock is what an operator reads; uptime runs on the monotonic clock so an
# NTP step cannot bend it.
STARTED_AT: float = time.time()
STARTED_MONO: float = time.monotonic()

_PACKAGE_DIR = Path(__file__).resolve().parent
_DIST_NAME = "nymeriaos"
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# The repo layout is ``<root>/Nymeria/nymeria``: the package dir, its parent
# and its grandparent are the only places this checkout's ``.git`` can be.
# Bounded so a tree that is not a checkout never borrows the commit of some
# unrelated repository further up (a dotfiles repo in the home dir, say).
_GIT_SEARCH_DEPTH = 3


@dataclass(frozen=True)
class Fingerprint:
    """Stat-only summary of the package's files and its ``run.py``."""

    files: int
    mtime_ns_sum: int
    size_sum: int


@dataclass(frozen=True)
class BootRecord:
    version: str
    started_at: float
    commit: Optional[str]
    installed: bool
    dist_version: Optional[str]
    fingerprint: Optional[Fingerprint]
    started_mono: float = 0.0


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _git_dir(package_dir: Path) -> Optional[Path]:
    """The git dir of the checkout holding ``package_dir``, or None."""
    for directory in [package_dir, *package_dir.parents][:_GIT_SEARCH_DEPTH]:
        dotgit = directory / ".git"
        if dotgit.is_dir():
            return dotgit
        if dotgit.is_file():
            # A linked worktree or submodule: ``gitdir: <path>``.
            text = _read_text(dotgit) or ""
            if not text.startswith("gitdir:"):
                return None
            target = Path(text[len("gitdir:"):].strip())
            return target if target.is_absolute() else (directory / target).resolve()
    return None


def _resolve_ref(git_dir: Path, ref: str) -> Optional[str]:
    common = git_dir
    commondir = _read_text(git_dir / "commondir")
    if commondir:
        common = (git_dir / commondir).resolve()
    for base in (git_dir, common):
        value = _read_text(base / ref)
        if value and _SHA.fullmatch(value):
            return value
    packed = _read_text(common / "packed-refs")
    for line in (packed or "").splitlines():
        sha, _, name = line.partition(" ")
        if name == ref and _SHA.fullmatch(sha):
            return sha
    return None


def checkout_commit(package_dir: Path = _PACKAGE_DIR) -> Optional[str]:
    """HEAD of the source checkout this package runs from, or None (an
    installed wheel, a tree with no ``.git``, or anything unreadable)."""
    try:
        if is_installed_location(package_dir):
            return None
        git_dir = _git_dir(package_dir)
        if git_dir is None:
            return None
        head = _read_text(git_dir / "HEAD")
        if not head:
            return None
        if _SHA.fullmatch(head):
            return head  # detached
        if head.startswith("ref:"):
            return _resolve_ref(git_dir, head[len("ref:"):].strip())
    except (OSError, ValueError):
        return None
    return None


def _stat_paths(package_dir: Path):
    for root, dirs, names in os.walk(package_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in names:
            yield os.path.join(root, name)
    # The entry point beside the package: a checkout's ``Nymeria/run.py``,
    # or the wheel's top-level ``run.py`` in site-packages.
    yield str(package_dir.parent / "run.py")


def source_fingerprint(package_dir: Path = _PACKAGE_DIR) -> Optional[Fingerprint]:
    """None when the package has no files (gone from disk) or cannot be read."""
    files = mtime_sum = size_sum = 0
    try:
        for path in _stat_paths(package_dir):
            try:
                st = os.stat(path)
            except OSError:
                continue
            files += 1
            mtime_sum += st.st_mtime_ns
            size_sum += st.st_size
    except OSError:
        return None
    return Fingerprint(files, mtime_sum, size_sum) if files else None


def fingerprint_digest(fingerprint: Fingerprint) -> str:
    """A short, stable label for a fingerprint, for comparing across hosts.

    ``GET /status/turns`` reports the BOOT fingerprint's digest to admin
    callers, and ``scripts/deploy_sync.py`` computes the same digest over the
    host checkout to confirm a restarted process loaded exactly the files on
    disk (#423; a bind mount preserves stat, so this holds for containers
    too). The script is stdlib-only and cannot import this package, so it
    carries a copy of this walk and digest; a parity test pins the two.
    """
    raw = f"{fingerprint.files}:{fingerprint.mtime_ns_sum}:{fingerprint.size_sum}"
    return hashlib.sha256(raw.encode("ascii")).hexdigest()[:16]


def installed_dist_version() -> Optional[str]:
    """The ``nymeriaos`` version the metadata on disk names now."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - stdlib since 3.8
        return None
    try:
        return version(_DIST_NAME)
    except PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001 - a malformed dist-info is not our failure.
        return None


def capture(package_dir: Path = _PACKAGE_DIR) -> BootRecord:
    installed = is_installed_location(package_dir)
    return BootRecord(
        version=__version__,
        started_at=STARTED_AT,
        commit=checkout_commit(package_dir),
        installed=installed,
        dist_version=installed_dist_version() if installed else None,
        fingerprint=source_fingerprint(package_dir),
        started_mono=STARTED_MONO,
    )


_BOOT: Optional[BootRecord] = None


def boot_record() -> BootRecord:
    """The record taken at startup (``run.py`` calls this first thing). A
    process that never called it at startup captures on first use, which
    is late but still the best answer available."""
    global _BOOT
    if _BOOT is None:
        _BOOT = capture()
    return _BOOT


def short(commit: str) -> str:
    return commit[:8]


def describe_origin(record: BootRecord) -> str:
    if record.commit:
        return f"checkout commit {short(record.commit)}"
    if record.installed:
        return "an installed package"
    return "a source tree with no git metadata"


def boot_log_line(record: BootRecord) -> str:
    return f"Booted NymeriaOS {record.version} from {describe_origin(record)}"


def drift(record: BootRecord, package_dir: Path = _PACKAGE_DIR) -> Optional[str]:
    """Why the code on disk differs from what this process loaded, or None.

    The source fingerprint decides WHETHER anything changed; the commit and
    the installed version only describe it. A moved HEAD alone is not newer
    code: edit, restart, then commit is the ordinary loop, and a pull that
    touches only docs changes nothing this process runs."""
    try:
        if record.fingerprint is not None:
            now_print = source_fingerprint(package_dir)
            if now_print is None:
                # Uninstalled or moved: the next start will not find it.
                return "the package's files are gone from disk since this process started"
            if now_print == record.fingerprint:
                return None
        if record.commit:
            now = checkout_commit(package_dir)
            if now and now != record.commit:
                return f"the checkout moved to {short(now)} since this process started"
        if record.installed and record.dist_version:
            now_version = installed_dist_version()
            if now_version and now_version != record.dist_version:
                return (
                    f"NymeriaOS {now_version} is installed; this process "
                    f"runs {record.dist_version}"
                )
        if record.fingerprint is not None:
            return "source files changed on disk since this process started"
    except Exception:  # noqa: BLE001 - a status readout never fails on this.
        return None
    return None


def format_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return "under a minute"
    minutes, _ = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def status_lines(
    record: BootRecord,
    *,
    now_mono: Optional[float] = None,
    drift_reason: Optional[str] = None,
) -> list[str]:
    """The ``/status`` Code block body (unindented)."""
    now_mono = time.monotonic() if now_mono is None else now_mono
    started = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(record.started_at))
    head = record.version
    if record.commit:
        head = f"{head} @ {short(record.commit)}"
    uptime = format_uptime(now_mono - record.started_mono)
    lines = [f"{head} | started {started} (up {uptime})"]
    if drift_reason:
        lines.append(f"restart to load newer code: {drift_reason}")
    return lines
