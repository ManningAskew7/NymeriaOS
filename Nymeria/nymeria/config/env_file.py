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

The invariant every value obeys (#156): ONE physical line per key, which every
reader of these files decodes back to the exact original. The readers are
python-dotenv (boot and `/settings reload` via `load_env_files_into_environ`,
the restart re-merge, the pydantic dotenv source in doctor, the wizard's
hydrate), Docker Compose (`.env.docker` through `--env-file` interpolation and
service `env_file:`), and hand-rolled one-line readers that see nothing but
one line per key (`service_install._read_env_port`, `server_browser`,
finalize's vault-key reader, `scripts/deploy_sync.py`). A raw line break in a
value was a delayed env-var injection: the one-line readers took a
continuation line as a binding at once, and the next write of the same key
orphaned the rest into real bindings for python-dotenv too. The merge below is
still line-based, so it relies on this invariant; a multi-line value already
in a file (hand-edited, or written before #156) is not repaired by it.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# What an env key this code writes or names looks like: upper-case letters,
# digits and underscores, starting with a letter. The one owner of the shape
# (#434): the clear path refuses anything else before it can reach a file or a
# URL, and the wizard filters the in-container override report through it.
# Private and unanchored on purpose: built for ``fullmatch`` only, so callers
# go through ``is_env_key_name`` (a ``.match`` would accept a prefix).
_ENV_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")


def is_env_key_name(name: str) -> bool:
    """True when ``name`` is a whole env key in the shape above (no case folding)."""
    return _ENV_KEY_RE.fullmatch(name) is not None


class EnvValueError(ValueError):
    """A value no env-file line can store as written (#156).

    The message says what is wrong with the value and never contains it (it
    may be a secret); callers prefix the setting's name.
    """


# Characters an unquoted value may hold: every reader takes these literally.
_BARE_PUNCTUATION = "/._:-="

# Inside double quotes: the two escapes the formatter always wrote, plus the
# line breaks python-dotenv (both 1.2.2 and 1.2.3) and Docker Compose both
# decode (measured, #156). Applied one character at a time, so a backslash in
# the value can never pair up with an escape introduced here.
_DOUBLE_QUOTED_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\v": "\\v",
    "\f": "\\f",
}

# What no env-file line can carry. NUL fits no environment variable at all
# (`os.environ` refuses it, and python-dotenv stops loading the file there,
# so every later key is lost at boot). The other six are line breaks to
# `str.splitlines`, which this module's merge reads with, as do the repo's
# one-line readers (`service_install._read_env_port`, finalize's
# `_read_env_value_from_file`, `server_browser._read_env_value`): to them a
# value holding one continues onto a line of its own, read as a binding.
# python-dotenv and Compose read all six literally inside double quotes and
# need no escape (measured, #156 review), so the constraint is ours, not a
# parser's: lifting the refusal means teaching those readers first.
_REFUSED_CHARACTERS = {
    "\x00": "NUL",
    "\x1c": "FILE SEPARATOR",
    "\x1d": "GROUP SEPARATOR",
    "\x1e": "RECORD SEPARATOR",
    "\x85": "NEXT LINE",
    "\u2028": "LINE SEPARATOR",
    "\u2029": "PARAGRAPH SEPARATOR",
}


def _is_bare(text: str, extra: str = "") -> bool:
    return all(c.isalnum() or c in _BARE_PUNCTUATION or c in extra for c in text)


def format_env_value(value: str | bool | None) -> str:
    """Render a Python value as a dotenv RHS on ONE physical line.

    ``None`` and empty become ``""``; booleans become ``true``/``false`` (the
    lowercase form the settings models parse). Otherwise the value is returned
    unquoted when every character is in a safe set (alphanumerics plus
    ``/._:-=``), so base64url values such as a Fernet key ending in ``=`` write
    cleanly and Docker ``env_file`` quote handling (which is version-fragile) is
    never exercised; anything else is double-quoted, escaping ``\\`` and ``"``
    as always plus the line breaks LF, CR, VT and FF (#156). A value without a
    line break therefore formats exactly as it did before #156.

    Raises :class:`EnvValueError` for what no form can carry: a NUL, the six
    line separators the env writer's merge and the repo's one-line readers
    split on (``str.splitlines``), and a trailing backslash on a value that
    must be quoted (python-dotenv before 1.2.3 reads ``"...\\\\"``
    as an escaped quote and swallows the lines after it). A trailing backslash
    on a value that is otherwise bare (``C:\\``) is written bare instead: every
    reader takes an unquoted backslash literally.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    if not text:
        return ""
    for char in text:
        name = _REFUSED_CHARACTERS.get(char)
        if name is None:
            continue
        if char == "\x00":
            raise EnvValueError(
                "contains a NUL character (U+0000), which no environment variable can hold"
            )
        raise EnvValueError(
            f"contains U+{ord(char):04X} ({name}), a line break the env writer "
            "and its one-line readers cannot carry"
        )
    if _is_bare(text):
        return text
    if text.endswith("\\"):
        if _is_bare(text, extra="\\"):
            return text
        raise EnvValueError(
            "ends with a trailing backslash, which python-dotenv before 1.2.3 "
            "misreads inside quotes, losing the lines after it (drop the trailing "
            "backslash: paths work without it)"
        )
    return '"' + "".join(_DOUBLE_QUOTED_ESCAPES.get(c, c) for c in text) + '"'


# python-dotenv's double-quote escape set and what each decodes to. Anything
# else after a backslash is kept as is, backslash included, exactly as it does.
_DOTENV_DOUBLE_QUOTE_ESCAPE_RE = re.compile(r"\\([\\'\"abfnrtv])")
_DOTENV_DOUBLE_QUOTE_DECODED = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}


def parse_env_value(raw: str) -> str:
    """Decode one RHS :func:`format_env_value` wrote, as python-dotenv reads it.

    Strips a surrounding pair of double quotes and decodes python-dotenv's
    double-quote escapes (``\\\\ \\' \\" \\a \\b \\f \\n \\r \\t \\v``) left to
    right in one pass, so ``\\\\n`` is a backslash and an ``n``, never a
    newline. The result is what the next reload or boot reads from the file,
    which is what ``_sync_updated_env_vars`` must export so the live value and
    the file never disagree. Unquoted values are returned unchanged.
    """
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return _DOTENV_DOUBLE_QUOTE_ESCAPE_RE.sub(
            lambda match: _DOTENV_DOUBLE_QUOTE_DECODED[match.group(1)], raw[1:-1]
        )
    return raw


def env_line_key(raw_line: str) -> str | None:
    """The key a dotenv line sets, as the merge below reads it; None for none.

    Comments, blank lines and lines without ``=`` set nothing. The line is
    split on its first ``=`` and stripped, so ``  KEY = value`` names ``KEY``
    while ``export KEY=value`` names ``export KEY``, which is not a key: that
    is the line shape the writer cannot replace or drop, and the one callers
    (#434 clear, #435 wizard) refuse to claim removed. One parse for both, so
    the check and the write cannot disagree.
    """
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


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
        key = env_line_key(raw_line)
        if key is not None:
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
    :func:`merge_env_lines` (a MISSING file is treated as empty, so every
    produced key is appended; any other read failure raises before anything is
    written, because rewriting an unreadable file from nothing would silently
    drop every line it held, the vault key and every saved credential included). Otherwise a fresh file is written from
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
        except FileNotFoundError:
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
    "env_line_key",
    "is_env_key_name",
    "EnvValueError",
    "format_env_value",
    "parse_env_value",
    "merge_env_lines",
    "write_env_file",
]
