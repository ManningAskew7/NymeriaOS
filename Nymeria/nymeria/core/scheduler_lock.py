"""One scheduler per data directory, enforced across processes (#397).

The schedule (``todo_schedule.db``, the trigger and TODO stores it drives, and
``scheduler_state.json``) must have exactly one ticker. Every shape keeps that
by construction (slim runs one in the API process, Docker runs one in the
worker and disables the API's), but nothing stopped a SECOND process from
starting another on the same data dir, and the fat CLI's local transport did
exactly that beside a running slim instance: its startup recovery deleted the
live ticker's execution markers (a double fire), then both polled the same
schedule and fired the same triggers.

So the Ticker takes this lock before it touches the schedule, and a ticker
that cannot get it stands by instead. The lock is an OS lock on an open file:
``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows. The OS drops it when
the process exits, a crash or a ``kill -9`` included, so a dead owner can never
leave the directory locked (no pid files to go stale). ``flock`` locks belong
to the open file description, not the process, so two tickers in ONE process
contend exactly like two processes, which is what lets tests and embedded
hosts see the same rule.

Two things this module deliberately never does:

- It never deletes the lock file. Unlinking on release is the classic flock
  race: a waiter that opened the old file locks an orphaned inode while a
  newcomer creates and locks a fresh one, and both believe they own it.
- It never blocks. Contention is an answer ("someone else owns it"), not a
  wait; the ticker retries on its own poll cadence.

Any error OTHER than contention (a filesystem without lock support, a data dir
that cannot be written) is reported as ``UNAVAILABLE`` and the ticker fails
OPEN: running without the guard is the pre-#397 behavior, while failing closed
would leave a service with no scheduler at all because of a lock. The ticker
retries the lock each poll while it runs unguarded.

Where the guard does not reach: on NFS, Linux emulates ``flock`` with POSIX
record locks, which belong to the PROCESS (two tickers in one process no
longer contend) and are dropped when the owner closes ANY descriptor on the
file; and a Docker Desktop bind mount of a Windows or macOS host directory
does not share locks between a host process and a container. Named Docker
volumes on one host are fine (one kernel, one inode).
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import socket
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCHEDULER_LOCK_FILENAME = "scheduler.lock"

# Windows locks a byte RANGE, and a locked range cannot be read through any
# other handle. The lock therefore sits well past the holder's note (which
# lives at offset 0 and stays readable); locking beyond end of file is allowed.
_WINDOWS_LOCK_OFFSET = 1 << 20

# Errnos that mean "another holder has it". flock reports EWOULDBLOCK (EAGAIN
# on Linux); msvcrt.locking reports EACCES, or EDEADLOCK after its retries.
_CONTENTION_ERRNOS = frozenset(
    code
    for code in (
        getattr(errno, "EWOULDBLOCK", None),
        getattr(errno, "EAGAIN", None),
        getattr(errno, "EACCES", None),
        getattr(errno, "EDEADLK", None),
        getattr(errno, "EDEADLOCK", None),
    )
    if code is not None
)

_HOLDER_NOTE_MAX_BYTES = 1024

# Readable by every user: a lock file one user created must still be lockable
# by another through a read-only descriptor (flock needs no write access).
_LOCK_FILE_MODE = 0o644

# A subcommand word (``slim``, ``claude-code-runner``), never a path or flag.
_SUBCOMMAND_SHAPE = re.compile(r"[a-z][a-z0-9_-]{0,31}")


class LockOutcome(str, Enum):
    ACQUIRED = "acquired"
    HELD_ELSEWHERE = "held_elsewhere"
    UNAVAILABLE = "unavailable"


class ProbeOutcome(str, Enum):
    """What ``ScheduleLock.probe`` saw: someone holds the lock, nobody does,
    or the lock cannot be tested here."""

    HELD = "held"
    FREE = "free"
    UNKNOWN = "unknown"


def _command_label() -> str:
    """A short, secret-free name for this process: the program plus its
    subcommand (``nymeria slim``, ``run.py worker``), never flags or values.

    Only ``argv[1]`` can be the subcommand: the first bare word after a flag
    is that flag's VALUE (``--token nym_...``), not a subcommand.
    """
    argv = list(sys.argv or [])
    if not argv:
        return "python"
    program = Path(argv[0]).name or "python"
    subcommand = argv[1] if len(argv) > 1 else ""
    if not _SUBCOMMAND_SHAPE.fullmatch(subcommand):
        subcommand = ""
    return f"{program} {subcommand}".strip()[:80]


def _current_process_note() -> dict:
    return {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "command": _command_label(),
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def describe_current_process() -> str:
    """This process, in the words its holder note would use."""
    return describe_holder(_current_process_note())


def describe_holder(note: dict) -> str:
    """Render a holder note as one line: ``pid 123 on host (nymeria slim)``."""
    pid = note.get("pid")
    host = note.get("host")
    command = note.get("command")
    parts = [f"pid {pid}" if pid else "an unknown process"]
    if host:
        parts.append(f"on {host}")
    text = " ".join(parts)
    if command:
        text += f" ({command})"
    # The note is a file anyone can write: no terminal control sequences.
    return "".join(ch for ch in text if ch.isprintable())[:200]


class ScheduleLock:
    """Non-blocking exclusive ownership of one data dir's schedule."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / SCHEDULER_LOCK_FILENAME
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> tuple[LockOutcome, str]:
        """Try once. Returns the outcome and, when it failed, why (the holder
        for contention, the error for an unavailable lock)."""
        if self._fd is not None:
            return LockOutcome.ACQUIRED, ""
        outcome, fd, detail = self._open_and_lock()
        if fd is not None:
            self._fd = fd
        return outcome, detail

    def _open_and_lock(self) -> tuple[LockOutcome, Optional[int], str]:
        binary = getattr(os, "O_BINARY", 0)
        writable = True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # World-readable (the umask permitting) so the fallback below can
            # open a file another user created; the note holds no secret.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | binary, _LOCK_FILE_MODE)
        except PermissionError:
            # A file another user left (``sudo nymeria slim`` once) still
            # locks through a read-only descriptor; only the note is lost.
            try:
                fd = os.open(self.path, os.O_RDONLY | binary)
            except OSError as exc:
                return (
                    LockOutcome.UNAVAILABLE,
                    None,
                    f"cannot open {self.path}: {exc}",
                )
            writable = False
        except OSError as exc:
            return LockOutcome.UNAVAILABLE, None, f"cannot open {self.path}: {exc}"
        try:
            _lock(fd)
        except OSError as exc:
            os.close(fd)
            if exc.errno in _CONTENTION_ERRNOS:
                return LockOutcome.HELD_ELSEWHERE, None, self.holder()
            return LockOutcome.UNAVAILABLE, None, f"cannot lock {self.path}: {exc}"
        if writable:
            self._write_note(fd)
        return LockOutcome.ACQUIRED, fd, ""

    def relock_if_replaced(self) -> Optional[LockOutcome]:
        """None while the path is still the file this lock holds. Otherwise
        (deleted, or replaced by a restore or a hand edit) try to lock what is
        there now: ``ACQUIRED`` swaps to it, anything else is reported."""
        fd = self._fd
        if fd is None:
            return None
        try:
            held = os.fstat(fd)
            current = os.stat(self.path)
            if (held.st_dev, held.st_ino) == (current.st_dev, current.st_ino):
                return None
        except FileNotFoundError:
            pass  # deleted: lock whatever file the path names now
        except OSError:
            return None  # cannot tell; a transient stat error is no alarm
        outcome, new_fd, _detail = self._open_and_lock()
        if new_fd is not None:
            self._fd = new_fd
            try:
                _unlock(fd)
            except OSError:
                pass  # closing the old descriptor below drops its lock anyway
            os.close(fd)
        return outcome

    def release(self) -> None:
        """Drop the lock (idempotent). The file stays: see the module notes."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        except OSError:
            logger.debug("Schedule lock unlock failed; closing releases it")
        finally:
            try:
                os.close(fd)
            except OSError:
                pass  # already closed or invalid: nothing left to release

    def probe(self) -> tuple[ProbeOutcome, str]:
        """Whether a process holds this data dir's schedule NOW, and who.

        ``holder()`` alone cannot answer that: the note outlives its writer
        (the file is never deleted), so it names the last owner, running or
        not. This tries the lock through a read-only descriptor and drops it
        at once. It never creates the file (absent means nobody ever ran a
        scheduler here: FREE) and never writes the note, so the owner's note
        survives a probe byte for byte. For status readers in a process that
        does NOT own the schedule (the Docker API, a standby); ``held``
        already answers for this instance.

        Two traps, both accepted:

        - The probe holds the lock for microseconds. A ticker claiming in that
          instant stands by: a service retries on its next poll, a fat CLI
          (no takeover) stays on standby for its session. Vanishingly rare.
        - Never call it from a process whose own ticker holds the lock on a
          filesystem with PROCESS-owned locks (NFS, see the module notes):
          closing the probe's descriptor would drop the owner's lock. The
          ``scheduler_control`` status path asks the ticker first for that
          reason.
        """
        if self._fd is not None:
            return ProbeOutcome.HELD, self.holder()
        binary = getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(self.path, os.O_RDONLY | binary)
        except FileNotFoundError:
            return ProbeOutcome.FREE, ""
        except OSError as exc:
            return ProbeOutcome.UNKNOWN, f"cannot open {self.path}: {exc}"
        try:
            try:
                _lock(fd)
            except OSError as exc:
                if exc.errno in _CONTENTION_ERRNOS:
                    return ProbeOutcome.HELD, self.holder()
                return ProbeOutcome.UNKNOWN, f"cannot lock {self.path}: {exc}"
            try:
                _unlock(fd)
            except OSError:
                pass  # closing the descriptor below drops it anyway
            return ProbeOutcome.FREE, ""
        finally:
            os.close(fd)

    def holder(self) -> str:
        """Who holds the lock, from the note its holder wrote. Best effort."""
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(_HOLDER_NOTE_MAX_BYTES)
            note = json.loads(raw.decode("utf-8")) if raw.strip() else {}
        except (OSError, ValueError):
            note = {}
        if not isinstance(note, dict) or not note:
            return "another process"
        return describe_holder(note)

    @staticmethod
    def _write_note(fd: int) -> None:
        note = _current_process_note()
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps(note).encode("utf-8"))
        except OSError:
            logger.debug("Could not write the schedule lock note", exc_info=True)


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
