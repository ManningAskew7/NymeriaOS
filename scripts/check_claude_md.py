#!/usr/bin/env python3
"""Gate for the per-directory agent guide family (CLAUDE.md/AGENTS.md).

Three checks, all cheap and offline:

1. Path liveness: every backtick-quoted repo path a guide names must exist.
   A stale pointer actively misdirects agents, which is worse than no
   pointer. Tokens resolve against a small ladder of base dirs (repo root,
   the guide's own dir, Nymeria/, Nymeria/docs/, Nymeria/nymeria/, the
   plans dir, and the guide's src/lib for the client guides), so each guide
   keeps its local path conventions. The `shipped/NN` shorthand is resolved
   against the shipped/ index files. Bare filenames (no slash), globs,
   placeholders, URLs, and absolute or home paths are not checked.
2. Line budgets: each guide stays under its budget so density cannot
   silently regrow after a trim (always-loaded context reduces adherence;
   the 2026-08-02 split exists to keep per-session cost bounded). Raise a
   budget deliberately, in a reviewed commit, or trim the guide.
3. Twin integrity: every CLAUDE.md is byte-identical to the AGENTS.md
   beside it. The operator PostToolUse hook normally maintains this; this
   check is the machine-independent backstop.

Run from the repo root: python3 scripts/check_claude_md.py
Exit 0 when clean, 1 with findings listed.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Per-file line budgets. AGENTS.md twins are byte-identical, so only the
# CLAUDE.md side is budgeted.
BUDGETS = {
    "CLAUDE.md": 250,
    "Nymeria/CLAUDE.md": 250,
    "nymeria-desktop/CLAUDE.md": 150,
    "nymeria-mobile/CLAUDE.md": 100,
}
DEFAULT_BUDGET = 250

# Path-shaped tokens that are runtime artifacts, gitignored trees, or
# host-local locations a fresh clone will not have.
SKIP_EXACT = {
    "shipped/NN",       # the shorthand placeholder itself
    "temp/latest/",     # CLIProxy operator deployment dir (host-local)
    "nymeria-browser/", # separate gitignored checkout
    "Nymeria/build/",   # gitignored build artifact
    "origin/main",      # git ref, not a path
}
DOMAIN_TLDS = (".com", ".org", ".io", ".ai", ".dev")
SKIP_PREFIXES = (
    "data/",            # runtime data dir contents
    "Nymeria/data/",
)

INLINE_CODE = re.compile(r"`([^`\n]+)`")
SHIPPED_SHORTHAND = re.compile(r"^(?:private/plans/)?shipped/(\d{2})$")
REJECT_CHARS = set("*{}<>|[]()$\"' ")


def guide_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "CLAUDE.md", "AGENTS.md", "*/CLAUDE.md", "*/AGENTS.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / p for p in sorted(set(out))]


def candidate(token: str) -> str | None:
    """Return the checkable path part of an inline-code token, or None."""
    token = token.split("::")[0]
    if not token or "/" not in token:
        return None
    if any(c in REJECT_CHARS for c in token):
        return None
    if token.startswith(("http", "/", "~", "-", "glob:", "#", ".")):
        return None
    if token in SKIP_EXACT or token.startswith(SKIP_PREFIXES):
        return None
    if token.split("/")[0].endswith(DOMAIN_TLDS):
        return None  # a URL without its scheme, not a repo path
    return token


def resolves(token: str, guide_dir: Path) -> bool:
    m = SHIPPED_SHORTHAND.match(token)
    if m:
        shipped = REPO_ROOT / "Nymeria/docs/private/plans/shipped"
        return any(shipped.glob(f"{m.group(1)}-*.md"))
    bases = [
        REPO_ROOT,
        guide_dir,
        REPO_ROOT / "Nymeria",
        REPO_ROOT / "Nymeria/docs",
        REPO_ROOT / "Nymeria/nymeria",
        REPO_ROOT / "Nymeria/docs/private/plans",
        guide_dir / "src/lib",
    ]
    want_dir = token.endswith("/")
    for base in bases:
        p = base / token
        if want_dir and p.is_dir():
            return True
        if not want_dir and p.exists():
            return True
    return False


def main() -> int:
    findings: list[str] = []
    guides = [g for g in guide_files() if g.name == "CLAUDE.md"]

    for guide in guides:
        rel = guide.relative_to(REPO_ROOT).as_posix()
        text = guide.read_text(encoding="utf-8")

        budget = BUDGETS.get(rel, DEFAULT_BUDGET)
        lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
        if lines > budget:
            findings.append(f"{rel}: {lines} lines exceeds its budget of {budget}")

        twin = guide.with_name("AGENTS.md")
        if twin.exists() and guide.read_bytes() != twin.read_bytes():
            findings.append(f"{rel}: not byte-identical to its AGENTS.md twin")

        seen: set[str] = set()
        for tok in INLINE_CODE.findall(text):
            path_tok = candidate(tok)
            if path_tok is None or path_tok in seen:
                continue
            seen.add(path_tok)
            if resolves(path_tok, guide.parent):
                continue
            # `pkg/module.symbol` spelling: retry as `pkg/module.py`.
            variant = re.sub(r"\.\w+$", ".py", path_tok)
            if variant != path_tok and resolves(variant, guide.parent):
                continue
            findings.append(f"{rel}: dead path `{path_tok}`")

    if findings:
        print(f"check_claude_md: {len(findings)} finding(s)")
        for f in findings:
            print(f"  - {f}")
        return 1
    print(f"check_claude_md: OK ({len(guides)} guides checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
