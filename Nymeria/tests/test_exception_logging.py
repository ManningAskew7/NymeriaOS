"""CLEAN-004 regression: no silent exception swallowing.

Every ``except …: pass`` block in the backend must either replace ``pass``
with a logger call or carry an inline ``# …`` comment justifying the silent
pass.  This test statically asserts that rule so new bare-pass blocks cannot
be introduced.
"""

from __future__ import annotations

import re
from pathlib import Path

NYMERIA_ROOT = Path(__file__).resolve().parent.parent / "nymeria"

_PASS_RE = re.compile(r"^\s*pass\s*$")


def _find_bare_except_pass_blocks() -> list[str]:
    """Return ``file:line`` strings for every except handler whose body is a bare ``pass``."""
    violations: list[str] = []
    for py_file in sorted(NYMERIA_ROOT.rglob("*.py")):
        lines = py_file.read_text().splitlines(keepends=True)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("except") or not stripped.endswith(":"):
                continue
            next_idx = i + 1
            if next_idx >= len(lines):
                continue
            next_line = lines[next_idx]
            if _PASS_RE.match(next_line) and "#" not in next_line:
                rel = py_file.relative_to(NYMERIA_ROOT.parent)
                violations.append(f"{rel}:{next_idx + 1}")
    return violations


def test_no_bare_except_pass():
    """Every except-pass block must have a logger call or a justification comment."""
    violations = _find_bare_except_pass_blocks()
    assert violations == [], (
        "Bare `except …: pass` blocks found without logging or comment:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_broad_except_uses_logging_not_pass():
    """Broad ``except Exception`` handlers must use logger, not pass (even with a comment)."""
    violations: list[str] = []
    for py_file in sorted(NYMERIA_ROOT.rglob("*.py")):
        lines = py_file.read_text().splitlines(keepends=True)
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped in ("except Exception:", "except Exception as e:"):
                next_idx = i + 1
                if next_idx >= len(lines):
                    continue
                next_stripped = lines[next_idx].strip()
                if next_stripped.startswith("pass"):
                    rel = py_file.relative_to(NYMERIA_ROOT.parent)
                    violations.append(f"{rel}:{next_idx + 1}")
    assert violations == [], (
        "Broad `except Exception: pass` blocks found (must use logger.X instead):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_mcp_sources_has_logger():
    """mcp_sources.py must have a module-level logger (added in CLEAN-004)."""
    src = (NYMERIA_ROOT / "core" / "mcp_sources.py").read_text()
    assert "logger = logging.getLogger(" in src
