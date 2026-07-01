"""Small, dependency-free template substitution shared across subsystems.

``safe_format`` is plain ``str.format_map`` with a dict subclass whose
``__missing__`` returns the placeholder literally (``{key}``) instead of
raising. So a template with no placeholders passes through unchanged (static
text), any ``{name}`` present in ``variables`` is substituted (dynamic text),
and an unknown ``{name}`` is left intact rather than crashing. Any other
formatting error falls back to the raw template.

This lives in its own module so both the trigger stack (``trigger_manager`` and
its API/tool layers) and the lifecycle-hooks stack interpolate identically,
with a single implementation and no drift.
"""

from __future__ import annotations

from typing import Mapping


class _DefaultDict(dict):
    """Dict that returns ``{key}`` for missing keys instead of raising."""

    def __missing__(self, key):
        return f"{{{key}}}"


def safe_format(template: str, variables: Mapping) -> str:
    """Format a template string, leaving unknown ``{placeholders}`` intact."""
    try:
        return template.format_map(_DefaultDict(variables))
    except Exception:
        return template
