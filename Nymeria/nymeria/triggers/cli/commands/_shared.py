"""Shared leaf helpers for the CLI command modules.

These small, dependency-light value-coercion helpers were re-implemented
byte-identically across several command modules (account, skills, mcp,
triggers, todos, tools). Centralizing them keeps a fix to the list/mapping
coercion rules in one place instead of in up to six copies.

This module is the canonical home that finding F2 (relocating the larger
`system.py` helper toolkit) is meant to grow into; for now it holds only the
genuinely-identical F3 helpers. The other helpers named in F3 (`_csv`,
`_aligned_rows`) are deliberately left in their own modules: their bodies and
signatures diverge across copies, so merging them would change output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def mapping_sequence(value: Any) -> list[Mapping[str, Any]]:
    """Return the mapping items of a non-str/bytes sequence, else an empty list."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def string_list(value: Any) -> list[str]:
    """Return the truthy stringified items of a non-str/bytes sequence."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item)]
