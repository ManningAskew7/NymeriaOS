"""Shared helpers for deriving safe, unique callable-thread names.

A thread marked ``callable=true`` becomes an agent tool, so its ``callable_name``
must match the tool-name grammar (``^[a-zA-Z0-9_-]{1,64}$``) and be unique among
the user's tools and other callables. Both the branch path (``thread_branch.py``)
and the import path (``thread_share.py``) need to sanitize a desired name and
deduplicate it against an unavailable set; this module is the single source of
that contract so the two paths cannot silently drift.

Callers supply their own fallback string, digit prefix, and length budget, and
raise their own typed error when ``dedupe_callable_name`` exhausts its candidates
(it returns ``None`` rather than raising, to keep the error type caller-owned).
"""

from __future__ import annotations

import re
from typing import Optional, Set

# Tool-name grammar shared by every callable-thread name.
CALLABLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def safe_callable_base(
    value: str,
    *,
    fallback: str = "Branch",
    digit_prefix: str = "Branch",
    max_len: int = 64,
) -> str:
    """Sanitize an arbitrary string into a callable-name base.

    Non-alphanumeric characters (other than ``_``/``-``) collapse to single
    underscores, surrounding separators are stripped, an empty result falls back
    to ``fallback``, a leading digit is prefixed with ``digit_prefix``, and the
    result is truncated to ``max_len``.

    Args:
        value: The desired raw name.
        fallback: Name used when sanitization yields an empty string.
        digit_prefix: Prefix added (with ``_``) when the name starts with a digit.
        max_len: Maximum length of the returned base.
    """
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in value.strip())
    safe = "_".join(part for part in safe.split("_") if part)
    safe = safe.strip("_-")
    if not safe:
        safe = fallback
    if safe[0].isdigit():
        safe = f"{digit_prefix}_{safe}"
    return safe[:max_len] or fallback


def dedupe_callable_name(base: str, unavailable: Set[str]) -> Optional[str]:
    """Return a unique callable name derived from ``base``.

    Tries the bare ``base`` (capped at 64 chars) first, then ``base_2`` .. ``base_999``,
    returning the first candidate that matches :data:`CALLABLE_NAME_RE` and is not in
    ``unavailable``. Returns ``None`` when every candidate is taken so the caller can
    raise its own domain-specific error.
    """
    candidate = base[:64]
    if CALLABLE_NAME_RE.match(candidate) and candidate not in unavailable:
        return candidate
    for i in range(2, 1000):
        suffix = f"_{i}"
        candidate = f"{base[:64 - len(suffix)]}{suffix}"
        if CALLABLE_NAME_RE.match(candidate) and candidate not in unavailable:
            return candidate
    return None
