"""Canonical filesystem path-segment sanitization for per-identifier storage.

Several core storage layers derive a JSON or SQLite file path from a caller
supplied identifier (a user id or thread id): ``TodoManager``,
``TriggerManager``, ``GoalManager``, ``UserProfileManager``,
``ThreadMetadataManager``, ``ActivityLog``, the notification store, the ticker's
TODO-completion indexer, and the per-user memory-index path in
``agent_prompt``. Each historically inlined the same path-traversal guard: keep
only ``[A-Za-z0-9-_]`` characters and fall back to a default when nothing
survives. The copies had already begun to drift (some inline ``or "default"``,
others a separate ``if not safe:`` block).

This module is the single home for that rule, so a change to the allowed
character set or the fallback happens in one place instead of ten. It imports
nothing from the rest of the package (stdlib only), so any storage module can
depend on it without a cycle.

It is also the home of ``quarantine_corrupt_file``, the shared corrupt-store
quarantine used by the file-backed resource stores (resource-filesystem-layout
plan, slice 3), and of the store-fingerprint sidecar helpers used by the
external-edit audit (slice 4), for the same no-cycle reason.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

# Characters permitted in a path segment in addition to alphanumerics. Path
# separators and "." are intentionally excluded, which is what blocks traversal.
_ALLOWED_EXTRA = "-_"


def safe_path_segment(value: str, *, default: str = "default") -> str:
    """Reduce ``value`` to a filesystem-safe path segment.

    Keeps only alphanumeric characters plus ``-`` and ``_``, dropping everything
    else (including ``/``, ``\\`` and ``.``). Using the result as a filename or
    directory component therefore cannot escape its parent directory. When no
    allowed character survives (an empty or all-punctuation input), ``default``
    is returned instead.

    Args:
        value: The raw identifier (user id, thread id, ...) to sanitize.
        default: Value returned when sanitization leaves an empty string. Pass
            ``""`` to keep the legacy "return possibly-empty" contract that a
            few non-core call sites still rely on.

    Returns:
        The sanitized segment, or ``default`` when it would otherwise be empty.
    """
    return "".join(c for c in value if c.isalnum() or c in _ALLOWED_EXTRA) or default


def quarantine_corrupt_file(path: Path) -> Optional[Path]:
    """Move an unparseable store file into a ``quarantine/`` sibling directory.

    The uniform fail-safe for raw edits to the file-backed resource stores:
    without it a corrupt file is either silently skipped or, worse for the
    whole-store-per-file layouts (hooks, triggers), overwritten with an empty
    store by the next save. The bytes survive under
    ``<store dir>/quarantine/<stem>.corrupt-<utc stamp>-<nonce><suffix>``; the
    subdirectory keeps the quarantined file out of the loaders' ``*.json`` /
    directory globs so it is not re-parsed (and re-quarantined) every sweep.

    Returns the quarantine path, or ``None`` when the move failed, in which
    case the caller should leave the file in place and degrade to its old
    skip-or-replace behavior. Never raises.
    """
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target_dir = path.parent / "quarantine"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / (
            f"{path.stem}.corrupt-{stamp}-{uuid.uuid4().hex[:6]}{path.suffix}"
        )
        path.rename(target)
        return target
    except OSError:
        return None


def store_fingerprint_path(path: Path) -> Path:
    """The ``<name>.sig`` sidecar that records a store file's fingerprint."""
    return path.with_name(path.name + ".sig")


def record_store_fingerprint(path: Path) -> Optional[Tuple[int, int]]:
    """Persist ``path``'s ``(st_mtime_ns, st_size)`` to its ``.sig`` sidecar.

    The sidecar is the shared "last acknowledged state" of a store file,
    visible across manager instances and processes (the stores that need it,
    hooks and triggers, are written and read by different instances: hook
    authoring goes through the tools/REST managers while the engine reads via
    its own, and trigger fire-state saves happen in the ticker, a separate
    process in the Docker shape). Managers call this after every save; loaders
    that detect and audit a raw on-disk edit call it again to acknowledge the
    edit, so it is audited once rather than once per reader.

    The write is atomic (temp file + rename). When ``path`` itself is missing
    (deleted or quarantined), the stale sidecar is removed instead. Never
    raises; returns the recorded fingerprint, or ``None`` when nothing was
    recorded.
    """
    sidecar = store_fingerprint_path(path)
    try:
        st = path.stat()
    except OSError:
        try:
            sidecar.unlink(missing_ok=True)
        except OSError:
            pass  # best-effort stale-sidecar cleanup; helper never raises
        return None
    sig = (st.st_mtime_ns, st.st_size)
    try:
        temp = sidecar.with_name(sidecar.name + ".tmp")
        temp.write_text(f"{sig[0]}:{sig[1]}\n", encoding="utf-8")
        temp.replace(sidecar)
    except OSError:
        return None
    return sig


def read_store_fingerprint(path: Path) -> Optional[Tuple[int, int]]:
    """Read the fingerprint recorded by ``record_store_fingerprint``.

    Returns ``None`` when the sidecar is absent or unreadable (including a
    corrupt sidecar), which callers treat as "no manager write on record":
    the external-edit audit fails safe rather than false-positiving. Never
    raises.
    """
    try:
        raw = store_fingerprint_path(path).read_text(encoding="utf-8").strip()
        mtime_ns, size = raw.split(":", 1)
        return (int(mtime_ns), int(size))
    except (OSError, ValueError):
        return None
