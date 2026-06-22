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
nothing from the rest of the package, so any storage module can depend on it
without a cycle.
"""

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
