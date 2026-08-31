"""Shared dotenv merge-writer for the env files Nymeria owns.

One algorithm, two callers: the offline setup wizard (`setup/finalize.py`, run
when the backend may be down) and the online `PATCH /settings` handler
(`api/routers/settings.py`, run against a live backend). Both overlay a small set
of changed keys onto an existing env file, preserving untouched lines, comments,
and ordering, and both must write atomically with 0600 perms because the file
holds API keys and the credential-vault Fernet key.

This module is intentionally low-level (stdlib only) and imports nothing from
`settings`/`finalize`, so either side can use it without an import cycle. Callers
own the field->env-var mapping; this module owns value formatting and the
read/overlay/append/atomic-write mechanics.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)


def format_env_value(value: str | bool | None) -> str:
    """Render a Python value as a dotenv RHS string.

    ``None`` and empty become ``""``; booleans become ``true``/``false`` (the
    lowercase form the settings models parse). Otherwise the value is returned
    unquoted when every character is in a safe set (alphanumerics plus
    ``/._:-=``), so base64url values such as a Fernet key ending in ``=`` write
    cleanly and Docker ``env_file`` quote handling (which is version-fragile) is
    never exercised; anything else is double-quoted with backslash escaping.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if text and all(c.isalnum() or c in "/._:-=" for c in text):
        return text
    if not text:
        return ""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_env_value(raw: str) -> str:
    """Inverse of :func:`format_env_value` for a single dotenv RHS.

    Strips a surrounding pair of double quotes and reverses the backslash escaping,
    so a value read back from a written line (e.g. to sync ``os.environ``) matches
    what a dotenv parser would load rather than carrying literal quotes. Unquoted
    values are returned unchanged. The ``\\(.)`` substitution unescapes left to
    right in one pass, so ``\\\\`` -> ``\\`` and ``\\"`` -> ``"`` without
    double-processing.
    """
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return re.sub(r"\\(.)", r"\1", raw[1:-1])
    return raw


def merge_env_lines(
    existing_lines: Sequence[str],
    produced: Sequence[tuple[str, str]],
    *,
    drop: Sequence[str] = (),
) -> list[str]:
    """Overlay ``produced`` ``(KEY, formatted_value)`` pairs onto existing lines.

    Existing comments, blank lines, and untouched keys are kept verbatim and in
    place; a produced key's value is replaced where it FIRST appears and any
    later duplicate lines for that key are REMOVED, so exactly one line for it
    survives; produced keys not already present are appended in order. Values in
    ``produced`` are assumed already formatted (see :func:`format_env_value`);
    this function never formats. The line parse splits on the first ``=`` and
    strips, so leading-whitespace and spaced ``KEY = value`` lines match.

    Collapsing duplicates rather than preserving them is deliberate (#301).
    Every reader of these files takes the LAST occurrence of a key
    (python-dotenv, the pydantic dotenv source, ``run.py::_load_environment``,
    the restart re-merge in ``api/routers/system.py``), so a preserved duplicate
    leaves the writer and every reader disagreeing about which line is live: the
    write reports success, the running process serves the new value, and the
    next restart silently reverts to the old one. Deleting a line from a file
    the user owns is the cost, taken knowingly: the deleted line is the one this
    write supersedes, and each removal is logged with its key (never its value).
    ``drop`` already removed every occurrence, so both paths now agree.

    ``drop`` keys have their existing lines REMOVED instead of preserved. This is
    for keys whose absence from ``produced`` means "retired", not "unchanged"
    (the `NYMERIA_INIT_*` pick carriers, which a reconfigure must be able to
    clear back to defaults). A key in both ``produced`` and ``drop`` is written,
    not dropped, so callers can pass a static drop list.
    """
    produced_map = dict(produced)
    drop_keys = {key for key in drop if key not in produced_map}
    seen: set[str] = set()
    collapsed: dict[str, int] = {}
    out: list[str] = []
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in produced_map:
                if key in seen:
                    collapsed[key] = collapsed.get(key, 0) + 1
                    continue
                out.append(f"{key}={produced_map[key]}")
                seen.add(key)
                continue
            if key in drop_keys:
                continue
        out.append(raw_line)
    for key, value in produced:
        if key not in seen:
            out.append(f"{key}={value}")
            seen.add(key)
    for key, count in collapsed.items():
        logger.warning(
            "Removed %d duplicate line(s) for %s while writing an env file: "
            "dotenv readers take the last occurrence, so keeping them would "
            "revert this write at the next restart (#301)",
            count,
            key,
        )
    return out


def write_env_file(
    path: Path,
    produced: Sequence[tuple[str, str]],
    *,
    merge: bool,
    header: str | None = None,
    drop: Sequence[str] = (),
) -> list[str]:
    """Atomically write ``path`` (0600) and return the final line list.

    ``merge``: overlay ``produced`` onto the file's current lines via
    :func:`merge_env_lines` (a missing/unreadable file is treated as empty, so
    every produced key is appended). Otherwise a fresh file is written from
    ``header`` (optional) plus the produced lines. Values are pre-formatted by
    the caller. The returned lines let callers sync ``os.environ`` afterward.
    ``drop`` keys are removed on merge unless re-produced (see
    :func:`merge_env_lines`); ignored on a fresh write.

    The returned list is the file's FINAL contents, not the lines this call
    changed. No production caller consumes it (only tests do): `PATCH
    /settings` deliberately syncs ``os.environ`` from its own produced pairs
    instead, because syncing from this return exported every mapped key the
    file happened to contain (#299).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if merge:
        try:
            existing = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing = []
        lines = merge_env_lines(existing, produced, drop=drop)
    else:
        lines = [header] if header else []
        lines.extend(f"{key}={value}" for key, value in produced)
    content = "\n".join(lines) + "\n"

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass  # chmod may fail on filesystems without POSIX modes
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass  # best-effort cleanup; re-raise the original error
        raise
    return lines


__all__ = [
    "format_env_value",
    "parse_env_value",
    "merge_env_lines",
    "write_env_file",
]
